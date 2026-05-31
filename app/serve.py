import json
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from app.config import DEFAULT_FILE_FROM, DEFAULT_FILE_TO, MAX_FILE_RESPONSE_LINES, MAX_INDEX_RESPONSE_FILES
from app.responses import error, plain_text
from app.tokens import resolve_workspace_for_access, revoke_token

router = APIRouter()


def _resolve_workspace(token: str) -> Path | None:
    return resolve_workspace_for_access(token)


def _resolve_safe_file_path(workspace: Path, requested_path: str) -> Path | None:
    normalized = PurePosixPath(requested_path.replace("\\", "/"))
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
        return None

    candidate = (workspace / Path(*normalized.parts)).resolve()
    workspace_resolved = workspace.resolve()
    if candidate != workspace_resolved and workspace_resolved not in candidate.parents:
        return None
    return candidate


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


@router.get("/t/{token}/file")
async def get_file(
    token: str,
    path: str = Query(...),
    from_line: int = Query(DEFAULT_FILE_FROM, alias="from"),
    to_line: int = Query(DEFAULT_FILE_TO, alias="to"),
) -> PlainTextResponse:
    workspace = _resolve_workspace(token)
    if workspace is None:
        return error("invalid or expired token", 403)

    if from_line < 1 or to_line < 1 or to_line < from_line:
        return error("invalid range", 400)

    target_path = _resolve_safe_file_path(workspace, path)
    if target_path is None or not target_path.is_file():
        return error("file not found", 404)

    try:
        raw_lines = target_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return error("binary file", 400)

    actual_to = min(to_line, from_line + MAX_FILE_RESPONSE_LINES - 1, len(raw_lines))
    selected_lines = raw_lines[from_line - 1 : actual_to]
    rendered = [f"{number}| {line}" for number, line in enumerate(selected_lines, start=from_line)]

    if len(raw_lines) > actual_to:
        next_from = actual_to + 1
        next_to = next_from + MAX_FILE_RESPONSE_LINES - 1
        rendered.append(f"--- 続きは from={next_from}&to={next_to} で取得 ---")

    return plain_text("\n".join(rendered))
