import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from app.config import DEFAULT_FILE_FROM, DEFAULT_FILE_TO, MAX_FILE_RESPONSE_LINES, MAX_INDEX_RESPONSE_FILES
from app.responses import error, plain_text
from app.tokens import list_tokens, resolve_workspace_for_access, revoke_token

router = APIRouter()


def _resolve_workspace(token: str) -> Path | None:
    return resolve_workspace_for_access(token)


def _normalize_requested_path(requested_path: str) -> PurePosixPath | None:
    normalized = PurePosixPath(requested_path.replace("\\", "/"))
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        return None
    return normalized


def _resolve_safe_file_path(workspace: Path, normalized: PurePosixPath) -> Path | None:
    candidate = (workspace / Path(*normalized.parts)).resolve()
    workspace_resolved = workspace.resolve()
    if candidate != workspace_resolved and workspace_resolved not in candidate.parents:
        return None
    return candidate


def _is_indexed_readable_file(workspace: Path, normalized_path: str) -> bool:
    index_path = workspace / "index.json"
    if not index_path.is_file():
        return False

    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        files = index_data.get("files", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return False

    for entry in files:
        if not isinstance(entry, dict):
            continue
        if entry.get("path") == normalized_path and entry.get("readable") is True:
            return True
    return False


def _slugify_identifier(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return normalized or "share"


def _indexed_readable_paths(workspace: Path) -> dict[str, dict[str, Any]]:
    index_path = workspace / "index.json"
    if not index_path.is_file():
        return {}
    try:
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        files = index_data.get("files", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    paths: dict[str, dict[str, Any]] = {}
    if not isinstance(files, list):
        return paths
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        if entry.get("readable") is not True:
            continue
        paths[path] = entry
    return paths


def _normalize_path_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        candidate = _normalize_requested_path(item.strip())
        if candidate is None:
            continue
        path = candidate.as_posix()
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def _resolve_share_paths(token: str, share_id: str, workspace: Path) -> tuple[str, set[str]] | None:
    record = list_tokens().get(token)
    if not isinstance(record, dict):
        return None

    indexed_paths = _indexed_readable_paths(workspace)
    if not indexed_paths:
        return None

    if share_id.startswith("folder-"):
        folder_id = _slugify_identifier(share_id.removeprefix("folder-"))
        folders = record.get("folders")
        if not isinstance(folders, list):
            return None
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            if _slugify_identifier(str(folder.get("id", ""))) != folder_id:
                continue
            raw_paths = _normalize_path_list(folder.get("paths"))
            scoped = {path for path in raw_paths if path in indexed_paths}
            if folder_id == "hidden":
                scoped = set()
            if folder_id == "share" and not scoped:
                scoped = {
                    path
                    for path in indexed_paths
                    if path.lower().endswith((".md", ".txt"))
                    or path.lower().startswith(("source/", "src/", "app/", "lib/"))
                }
            return str(folder.get("name", folder_id)), scoped
        return None

    shares = record.get("shares")
    if not isinstance(shares, list):
        return None
    normalized_share_id = _slugify_identifier(share_id)
    for share in shares:
        if not isinstance(share, dict):
            continue
        if _slugify_identifier(str(share.get("id", ""))) != normalized_share_id:
            continue
        raw_paths = _normalize_path_list(share.get("paths"))
        excluded_raw = share.get("excluded_paths")
        excluded_list = excluded_raw if isinstance(excluded_raw, list) else []
        excluded: set[str] = set()
        for item in excluded_list:
            if not isinstance(item, str):
                continue
            candidate = item.strip().lower()
            if candidate:
                excluded.add(candidate)

        scoped_paths = {path for path in raw_paths if path in indexed_paths}
        if "hidden files" in excluded:
            scoped_paths = {path for path in scoped_paths if _is_indexed_readable_file(workspace, path)}
        scoped_paths = {
            path
            for path in scoped_paths
            if not any(
                path.lower().startswith(excluded_path.rstrip("/"))
                for excluded_path in excluded
                if excluded_path.endswith("/")
            )
        }
        return str(share.get("name", share_id)), scoped_paths
    return None


def _parse_line_number(value: str | int | None, default_value: int) -> int | None:
    if value is None:
        parsed = default_value
    elif isinstance(value, int):
        parsed = value
    else:
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = int(candidate, 10)
        except ValueError:
            return None
    if parsed < 1:
        return None
    return parsed


def _parse_iso8601(value: str) -> datetime | None:
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _find_symbol_range(raw_lines: list[str], symbol_name: str) -> tuple[int, int] | None:
    escaped_name = re.escape(symbol_name)
    class_pattern = re.compile(rf"\b(class|struct)\s+{escaped_name}\b")
    function_pattern = re.compile(rf"\b{escaped_name}\s*\(")
    control_pattern = re.compile(r"^\s*(if|for|while|switch|return|catch)\b")
    candidate_ranges: list[tuple[int, int]] = []

    for line_index, line in enumerate(raw_lines, start=1):
        stripped = line.strip()
        is_class_like = class_pattern.search(line) is not None
        is_function_like = function_pattern.search(line) is not None and not control_pattern.search(stripped)
        if not is_class_like and not is_function_like:
            continue

        block_line = None
        for scan_index in range(line_index, len(raw_lines) + 1):
            scan_line = raw_lines[scan_index - 1]
            if "{" in scan_line:
                block_line = scan_index
                break
            if ";" in scan_line:
                break
        if block_line is None:
            continue

        brace_depth = 0
        started = False
        for end_index in range(block_line, len(raw_lines) + 1):
            for char in raw_lines[end_index - 1]:
                if char == "{":
                    brace_depth += 1
                    started = True
                elif char == "}" and started:
                    brace_depth -= 1
            if started and brace_depth == 0:
                candidate_ranges.append((line_index, end_index))
                break

    if not candidate_ranges:
        return None
    return min(candidate_ranges, key=lambda item: item[0])


@router.post("/revoke")
async def revoke(token: str = Query(...)) -> PlainTextResponse:
    if revoke_token(token):
        return plain_text("revoked")
    return error("invalid or expired token", 403)


@router.get("/t/{token}/index")
async def get_index(
    token: str,
    from_index: int = Query(1, alias="from"),
    to_index: int = Query(MAX_INDEX_RESPONSE_FILES, alias="to"),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    index_path = workspace / "index.json"
    if not index_path.is_file():
        return error("index not found. Please ingest a zip first.", 404)

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
        files = data.get("files", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return error("index not found. Please ingest a zip first.", 404)

    if from_index < 1 or to_index < 1 or to_index < from_index:
        return error("invalid range", 400)

    actual_to = min(to_index, from_index + MAX_INDEX_RESPONSE_FILES - 1, len(files))
    selected = files[from_index - 1 : actual_to]

    output_lines: list[str] = ["# Code Relay Index", ""]
    for item in selected:
        path = item.get("path")
        line_count = item.get("lines")
        size_bytes = item.get("bytes")
        if not isinstance(path, str) or not isinstance(line_count, int) or not isinstance(size_bytes, int):
            continue
        size_kb = max(1, (size_bytes + 1023) // 1024)
        output_lines.append(f"{path} | {line_count} lines | {size_kb} KB")

    if len(files) > actual_to:
        next_from = actual_to + 1
        next_to = next_from + MAX_INDEX_RESPONSE_FILES - 1
        output_lines.append(f"--- 続きは from={next_from}&to={next_to} で取得 ---")

    total_files = data.get("total_files", len(files))
    total_lines_sum = data.get("total_lines", 0)
    total_bytes = data.get("total_bytes", 0)
    total_size_kb = (total_bytes + 1023) // 1024

    output_lines.append("")
    output_lines.append("---")
    output_lines.append(f"Total files: {total_files}")
    output_lines.append(f"Total lines: {total_lines_sum}")
    output_lines.append(f"Total size: {total_size_kb} KB")

    return plain_text("\n".join(output_lines))


@router.get("/t/{token}/changes")
async def get_changes(token: str, since: str = Query(...)) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    since_dt = _parse_iso8601(since)
    if since_dt is None:
        return error("invalid since", 400)

    index_path = workspace / "index.json"
    if not index_path.is_file():
        return error("index not found. Please ingest a zip first.", 404)

    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return error("index not found. Please ingest a zip first.", 404)

    history = data.get("change_history", [])
    changes: list[dict[str, str | int]] = []
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            change = item.get("change")
            lines = item.get("lines")
            changed_at = item.get("changed_at")
            if (
                not isinstance(path, str)
                or not isinstance(change, str)
                or not isinstance(lines, int)
                or not isinstance(changed_at, str)
            ):
                continue
            changed_at_dt = _parse_iso8601(changed_at)
            if changed_at_dt is None or changed_at_dt <= since_dt:
                continue
            changes.append({"path": path, "change": change, "lines": lines, "changed_at": changed_at})

    latest_by_path: dict[str, dict[str, str | int]] = {}
    for item in sorted(changes, key=lambda entry: str(entry["changed_at"])):
        latest_by_path[str(item["path"])] = item
    selected = [latest_by_path[path] for path in sorted(latest_by_path)]

    lines = [f"# Changed files since {since}", ""]
    if not selected:
        lines.append("No changes.")
        return plain_text("\n".join(lines))

    for item in selected:
        lines.append(f"{item['path']} | {item['change']} | {item['lines']} lines")
    lines.extend(["", "---", f"Total changed files: {len(selected)}"])
    return plain_text("\n".join(lines))


@router.get("/t/{token}/share/{share_id}/index")
async def get_share_index(
    token: str,
    share_id: str,
    from_index: int = Query(1, alias="from"),
    to_index: int = Query(MAX_INDEX_RESPONSE_FILES, alias="to"),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    if from_index < 1 or to_index < 1 or to_index < from_index:
        return error("invalid range", 400)

    resolved_share = _resolve_share_paths(token, share_id, workspace)
    if resolved_share is None:
        return error("share not found", 404)

    share_name, scoped_paths = resolved_share
    indexed = _indexed_readable_paths(workspace)
    scoped_entries = [indexed[path] for path in sorted(scoped_paths) if path in indexed]
    actual_to = min(to_index, from_index + MAX_INDEX_RESPONSE_FILES - 1, len(scoped_entries))
    selected = scoped_entries[from_index - 1 : actual_to]

    lines = [f"# Code Relay Share Index: {share_name}", ""]
    for item in selected:
        path = item.get("path")
        line_count = item.get("lines")
        size_bytes = item.get("bytes")
        if not isinstance(path, str) or not isinstance(line_count, int) or not isinstance(size_bytes, int):
            continue
        size_kb = max(1, (size_bytes + 1023) // 1024)
        lines.append(f"{path} | {line_count} lines | {size_kb} KB")

    if len(scoped_entries) > actual_to:
        next_from = actual_to + 1
        next_to = next_from + MAX_INDEX_RESPONSE_FILES - 1
        lines.append(f"--- 続きは from={next_from}&to={next_to} で取得 ---")

    lines.extend(["", "---", f"Total files: {len(scoped_entries)}"])
    return plain_text("\n".join(lines))


@router.get("/t/{token}/share/{share_id}/file")
async def get_share_file(
    token: str,
    share_id: str,
    path: str | None = Query(None),
    from_line: str | int | None = Query(None, alias="from"),
    to_line: str | int | None = Query(None, alias="to"),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)
    if path is None or not path.strip():
        return error("missing path", 400)

    normalized_path = _normalize_requested_path(path.strip())
    if normalized_path is None:
        return error("unsafe path", 400)
    normalized_path_str = normalized_path.as_posix()

    resolved_share = _resolve_share_paths(token, share_id, workspace)
    if resolved_share is None:
        return error("share not found", 404)
    _, scoped_paths = resolved_share
    if normalized_path_str not in scoped_paths:
        return error("file is outside share scope", 404)

    resolved_from = _parse_line_number(from_line, DEFAULT_FILE_FROM)
    if resolved_from is None:
        return error("invalid line range", 400)
    default_to = resolved_from + (DEFAULT_FILE_TO - DEFAULT_FILE_FROM)
    resolved_to = _parse_line_number(to_line, default_to)
    if resolved_to is None or resolved_to < resolved_from:
        return error("invalid line range", 400)

    target_path = _resolve_safe_file_path(workspace, normalized_path)
    if target_path is None or not target_path.is_file() or not _is_indexed_readable_file(workspace, normalized_path_str):
        return error("file is not indexed or not readable", 404)

    try:
        raw_lines = target_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return error("file is not indexed or not readable", 404)

    actual_to = min(resolved_to, resolved_from + MAX_FILE_RESPONSE_LINES - 1, len(raw_lines))
    selected_lines = raw_lines[resolved_from - 1 : actual_to]
    display_upper_bound = actual_to if selected_lines else resolved_from
    line_number_width = len(str(display_upper_bound))
    rendered = [f"# {normalized_path_str} lines {resolved_from}-{actual_to}", ""]
    rendered.extend(
        f"{number:>{line_number_width}}| {line}" for number, line in enumerate(selected_lines, start=resolved_from)
    )
    if len(raw_lines) > actual_to:
        next_from = actual_to + 1
        next_to = next_from + (DEFAULT_FILE_TO - DEFAULT_FILE_FROM)
        rendered.append(f"--- 続きは from={next_from}&to={next_to} で取得 ---")
    return plain_text("\n".join(rendered))


@router.get("/t/{token}/file")
async def get_file(
    token: str,
    path: str | None = Query(None),
    from_line: str | int | None = Query(None, alias="from"),
    to_line: str | int | None = Query(None, alias="to"),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    if path is None or not path.strip():
        return error("missing path", 400)

    normalized_path = _normalize_requested_path(path.strip())
    if normalized_path is None:
        return error("unsafe path", 400)

    resolved_from = _parse_line_number(from_line, DEFAULT_FILE_FROM)
    if resolved_from is None:
        return error("invalid line range", 400)
    default_to = resolved_from + (DEFAULT_FILE_TO - DEFAULT_FILE_FROM)
    resolved_to = _parse_line_number(to_line, default_to)
    if resolved_to is None or resolved_to < resolved_from:
        return error("invalid line range", 400)

    target_path = _resolve_safe_file_path(workspace, normalized_path)
    if target_path is None:
        return error("unsafe path", 400)

    normalized_path_str = normalized_path.as_posix()
    if not target_path.is_file() or not _is_indexed_readable_file(workspace, normalized_path_str):
        return error("file is not indexed or not readable", 404)

    try:
        raw_lines = target_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return error("file is not indexed or not readable", 404)

    actual_to = min(resolved_to, resolved_from + MAX_FILE_RESPONSE_LINES - 1, len(raw_lines))
    selected_lines = raw_lines[resolved_from - 1 : actual_to]
    display_upper_bound = actual_to if selected_lines else resolved_from
    line_number_width = len(str(display_upper_bound))
    rendered = [f"# {normalized_path_str} lines {resolved_from}-{actual_to}", ""]
    rendered.extend(
        f"{number:>{line_number_width}}| {line}" for number, line in enumerate(selected_lines, start=resolved_from)
    )

    if len(raw_lines) > actual_to:
        next_from = actual_to + 1
        next_to = next_from + (DEFAULT_FILE_TO - DEFAULT_FILE_FROM)
        rendered.append(f"--- 続きは from={next_from}&to={next_to} で取得 ---")

    return plain_text("\n".join(rendered))


@router.get("/t/{token}/symbol")
async def get_symbol(
    token: str,
    path: str | None = Query(None),
    name: str | None = Query(None),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    if path is None or not path.strip():
        return error("missing path", 400)
    if name is None or not name.strip():
        return error("missing name", 400)

    normalized_path = _normalize_requested_path(path.strip())
    if normalized_path is None:
        return error("unsafe path", 400)

    target_path = _resolve_safe_file_path(workspace, normalized_path)
    if target_path is None:
        return error("unsafe path", 400)

    normalized_path_str = normalized_path.as_posix()
    if not target_path.is_file() or not _is_indexed_readable_file(workspace, normalized_path_str):
        return error("file is not indexed or not readable", 404)

    try:
        raw_lines = target_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return error("file is not indexed or not readable", 404)

    symbol_name = name.strip()
    symbol_range = _find_symbol_range(raw_lines, symbol_name)
    if symbol_range is None:
        return error(f"symbol not found: {symbol_name}", 404)

    start_line, end_line = symbol_range
    selected_lines = raw_lines[start_line - 1 : end_line]
    line_number_width = len(str(end_line))
    rendered = [f"# {normalized_path_str} symbol: {symbol_name} lines {start_line}-{end_line}", ""]
    rendered.extend(
        f"{number:>{line_number_width}}| {line}" for number, line in enumerate(selected_lines, start=start_line)
    )
    return plain_text("\n".join(rendered))
