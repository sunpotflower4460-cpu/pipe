import json
import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import quote_plus
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import BASE_DIR, BASE_PUBLIC_URL, WORKSPACE_ROOT
from app.index import build_index
from app.ingest import ingest, ingest_repo
from app.tokens import delete_token, list_tokens, revoke_token, update_token_metadata

router = APIRouter()
CHANGES_EXAMPLE_LOOKBACK_DAYS = 1
TOKEN_DISPLAY_PREFIX_LENGTH = 8
DEFAULT_VIEW_FROM = 1
DEFAULT_VIEW_TO = 600
MAX_VIEW_LINES = 800
DEFAULT_FOLDER_CANDIDATES: list[tuple[str, str]] = [
    ("source", "Source"),
    ("docs", "Docs"),
    ("config", "Config"),
    ("tests", "Tests"),
    ("hidden", "Hidden"),
    ("share", "AIに見せる"),
]
DEFAULT_SHARE_EXCLUDED_PATHS = ["build/", "node_modules/", "hidden files"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
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


def _display_time(raw: object) -> str:
    parsed = _parse_dt(raw)
    if parsed is None:
        return "-"
    return parsed.isoformat(timespec="seconds")


def _status(record: dict[str, object]) -> str:
    if record.get("revoked") is True:
        return "revoked"
    expires_at = _parse_dt(record.get("expires_at"))
    if expires_at is not None and expires_at <= _utc_now():
        return "expired"
    return "active"


def _workspace_path(record: dict[str, object]) -> Path | None:
    raw_path = record.get("workspace_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = Path(raw_path)
    candidate = (raw if raw.is_absolute() else (BASE_DIR / raw)).resolve()
    workspace_root = WORKSPACE_ROOT.resolve()
    if candidate != workspace_root and workspace_root not in candidate.parents:
        return None
    return candidate


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


def _load_index(workspace: Path) -> dict[str, object]:
    index_path = workspace / "index.json"
    if not index_path.is_file():
        return {}
    try:
        loaded = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_index(workspace: Path, data: dict[str, object]) -> None:
    index_path = workspace / "index.json"
    index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _hidden_paths(record: dict[str, object]) -> set[str]:
    raw_paths = record.get("hidden_paths")
    if not isinstance(raw_paths, list):
        return set()
    hidden: set[str] = set()
    for item in raw_paths:
        if not isinstance(item, str):
            continue
        normalized = _normalize_requested_path(item.strip())
        if normalized is None:
            continue
        hidden.add(normalized.as_posix())
    return hidden


def _set_hidden_paths(token: str, hidden_paths: set[str]) -> None:
    update_token_metadata(token, hidden_paths=sorted(hidden_paths))


def _slugify_identifier(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return normalized or "folder"


def _normalize_paths(raw_paths: object) -> list[str]:
    if not isinstance(raw_paths, list):
        return []
    normalized_paths: list[str] = []
    seen: set[str] = set()
    for item in raw_paths:
        if not isinstance(item, str):
            continue
        normalized = _normalize_requested_path(item.strip())
        if normalized is None:
            continue
        path = normalized.as_posix()
        if path in seen:
            continue
        seen.add(path)
        normalized_paths.append(path)
    return normalized_paths


def _normalize_excluded_paths(raw_paths: object) -> list[str]:
    if not isinstance(raw_paths, list):
        return list(DEFAULT_SHARE_EXCLUDED_PATHS)
    excluded: list[str] = []
    seen: set[str] = set()
    for item in raw_paths:
        if not isinstance(item, str):
            continue
        candidate = item.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        excluded.append(candidate)
    return excluded or list(DEFAULT_SHARE_EXCLUDED_PATHS)


def _default_folder_entries() -> list[dict[str, Any]]:
    return [{"id": folder_id, "name": name, "paths": [], "visible": True} for folder_id, name in DEFAULT_FOLDER_CANDIDATES]


def _folder_entries(record: dict[str, object]) -> list[dict[str, Any]]:
    raw_folders = record.get("folders")
    folders: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_folders, list):
        for item in raw_folders:
            if not isinstance(item, dict):
                continue
            folder_id = item.get("id")
            name = item.get("name")
            if not isinstance(folder_id, str) or not folder_id.strip():
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            normalized_id = _slugify_identifier(folder_id)
            if normalized_id in seen:
                continue
            seen.add(normalized_id)
            folders.append(
                {
                    "id": normalized_id,
                    "name": name.strip(),
                    "paths": _normalize_paths(item.get("paths")),
                    "visible": item.get("visible") is not False,
                }
            )

    for default_id, default_name in DEFAULT_FOLDER_CANDIDATES:
        if default_id in seen:
            continue
        folders.append({"id": default_id, "name": default_name, "paths": [], "visible": True})
        seen.add(default_id)
    folders.sort(key=lambda item: str(item["name"]).lower())
    return folders


def _share_entries(record: dict[str, object]) -> list[dict[str, Any]]:
    raw_shares = record.get("shares")
    shares: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not isinstance(raw_shares, list):
        return shares
    for item in raw_shares:
        if not isinstance(item, dict):
            continue
        share_id = item.get("id")
        share_name = item.get("name")
        if not isinstance(share_id, str) or not share_id.strip():
            continue
        if not isinstance(share_name, str) or not share_name.strip():
            continue
        normalized_id = _slugify_identifier(share_id)
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        shares.append(
            {
                "id": normalized_id,
                "name": share_name.strip(),
                "paths": _normalize_paths(item.get("paths")),
                "excluded_paths": _normalize_excluded_paths(item.get("excluded_paths")),
            }
        )
    return shares


def _save_folder_entries(token: str, folders: list[dict[str, Any]]) -> None:
    update_token_metadata(token, folders=folders)


def _save_share_entries(token: str, shares: list[dict[str, Any]]) -> None:
    update_token_metadata(token, shares=shares)


def _folder_suggestions(path: str) -> set[str]:
    lower = path.lower()
    folder_ids: set[str] = set()
    if lower.startswith(("source/", "src/", "lib/", "app/")):
        folder_ids.add("source")
    if lower.startswith(("docs/", "doc/")) or lower.endswith((".md", ".rst", ".txt")):
        folder_ids.add("docs")
    if lower.startswith(("test/", "tests/", "__tests__/")) or "/test" in lower:
        folder_ids.add("tests")
    if lower.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".cmake")) or lower in {
        "cmakelists.txt",
        "package.json",
        "pyproject.toml",
    }:
        folder_ids.add("config")
    return folder_ids


def _folder_path_map(visible_paths: list[str], hidden_paths: list[str], folders: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = {str(folder["id"]): set() for folder in folders}
    for path in visible_paths:
        for suggested in _folder_suggestions(path):
            mapping.setdefault(suggested, set()).add(path)
    for folder in folders:
        folder_id = str(folder["id"])
        for path in _normalize_paths(folder.get("paths")):
            mapping.setdefault(folder_id, set()).add(path)
    if "hidden" in mapping:
        mapping["hidden"].update(hidden_paths)
    if "share" in mapping:
        for path in visible_paths:
            if "source" in _folder_suggestions(path) or path.lower().endswith((".md", ".txt")):
                mapping["share"].add(path)
    return {folder_id: sorted(paths) for folder_id, paths in mapping.items()}


def _apply_hidden_flags(workspace: Path, hidden_paths: set[str]) -> None:
    data = _load_index(workspace)
    files = data.get("files")
    if not isinstance(files, list):
        return

    for item in files:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        item["readable"] = path not in hidden_paths

    _save_index(workspace, data)


def _refresh_index_with_hidden(workspace: Path, hidden_paths: set[str]) -> None:
    build_index(workspace)
    _apply_hidden_flags(workspace, hidden_paths)


def _stats(record: dict[str, object]) -> tuple[int, int, int]:
    workspace = _workspace_path(record)
    if workspace is None:
        return 0, 0, 0
    data = _load_index(workspace)
    files = data.get("files", [])
    hidden_count = 0
    if isinstance(files, list):
        for item in files:
            if isinstance(item, dict) and item.get("readable") is False:
                hidden_count += 1

    total_files = data.get("total_files", 0)
    total_lines = data.get("total_lines", 0)
    if not isinstance(total_files, int):
        total_files = 0
    if not isinstance(total_lines, int):
        total_lines = 0
    return total_files, total_lines, hidden_count


def _public_base(request: Request) -> str:
    if BASE_PUBLIC_URL:
        return BASE_PUBLIC_URL
    return str(request.base_url).rstrip("/")


def _token_from_response(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("TOKEN="):
            token = line.split("=", 1)[1].strip()
            if token:
                return token
    return None


def _parse_positive_int(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    candidate = raw.strip()
    if not candidate:
        return default
    try:
        parsed = int(candidate, 10)
    except ValueError:
        return default
    if parsed < 1:
        return default
    return parsed


def _admin_url(
    token: str | None = None,
    path: str | None = None,
    from_line: int | None = None,
    to_line: int | None = None,
    folder: str | None = None,
) -> str:
    params: list[str] = []
    if token:
        params.append(f"token={quote_plus(token)}")
    if path:
        params.append(f"path={quote_plus(path)}")
    if from_line is not None:
        params.append(f"from={from_line}")
    if to_line is not None:
        params.append(f"to={to_line}")
    if folder:
        params.append(f"folder={quote_plus(folder)}")
    if not params:
        return "/admin"
    return "/admin?" + "&".join(params)


def _render_admin_page(request: Request, error_message: str | None = None, info_message: str | None = None) -> HTMLResponse:
    base_url = _public_base(request)
    changes_since_timestamp = (_utc_now() - timedelta(days=CHANGES_EXAMPLE_LOOKBACK_DAYS)).isoformat()

    selected_token = request.query_params.get("token")
    selected_path = request.query_params.get("path")
    selected_folder = request.query_params.get("folder")
    view_from = _parse_positive_int(request.query_params.get("from"), DEFAULT_VIEW_FROM)
    view_to = _parse_positive_int(request.query_params.get("to"), DEFAULT_VIEW_TO)
    if view_to < view_from:
        view_to = view_from

    records = sorted(
        list_tokens().items(),
        key=lambda item: str(item[1].get("created_at", "")),
        reverse=True,
    )
    if not selected_token and records:
        selected_token = records[0][0]

    rows: list[str] = []
    selected_record: dict[str, object] | None = None
    selected_workspace: Path | None = None

    for token, record in records:
        total_files, total_lines, hidden_count = _stats(record)
        state = _status(record)
        source_type = record.get("source_type")
        if not isinstance(source_type, str) or not source_type:
            source_type = "unknown"
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            name = token[:TOKEN_DISPLAY_PREFIX_LENGTH]

        if selected_token == token:
            selected_record = record
            selected_workspace = _workspace_path(record)

        rows.append(
            "<li>"
            f"<strong>{escape(name)}</strong> "
            f"<span>({escape(source_type)} / {escape(state)})</span><br>"
            f"<small>files: {total_files} / lines: {total_lines} / hidden: {hidden_count} / created: {_display_time(record.get('created_at'))}</small><br>"
            f"<a href='{escape(_admin_url(token=token))}'>このメモを開く</a>"
            "</li>"
        )

    notebook_block = ""
    if selected_token and selected_record and selected_workspace and selected_workspace.is_dir():
        name = selected_record.get("name")
        if not isinstance(name, str) or not name.strip():
            name = selected_token[:TOKEN_DISPLAY_PREFIX_LENGTH]
        hidden = _hidden_paths(selected_record)
        _apply_hidden_flags(selected_workspace, hidden)
        index_data = _load_index(selected_workspace)
        files = index_data.get("files", [])
        visible_files: list[str] = []
        hidden_files: list[str] = []

        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict):
                    continue
                path = item.get("path")
                if not isinstance(path, str):
                    continue
                if item.get("readable") is False:
                    hidden_files.append(path)
                else:
                    visible_files.append(path)

        visible_files.sort()
        hidden_files.sort()
        folders = _folder_entries(selected_record)
        shares = _share_entries(selected_record)
        folder_paths = _folder_path_map(visible_files, hidden_files, folders)
        folder_lookup = {str(folder["id"]): folder for folder in folders}
        if selected_folder not in folder_lookup:
            selected_folder = "all"
        if selected_folder == "all":
            listed_files = visible_files
        else:
            listed_files = folder_paths.get(selected_folder, [])
        selected_file_html = "<p>ファイルをタップするとコード本文を表示します。</p>"
        selected_file_url = ""
        selected_file_path_normalized = ""

        if selected_path:
            normalized = _normalize_requested_path(selected_path)
            if normalized is None:
                selected_file_html = "<p style='color:#b00020;'>無効なファイルパスです。</p>"
            else:
                selected_file_path_normalized = normalized.as_posix()
                target = _resolve_safe_file_path(selected_workspace, normalized)
                if target is None or not target.is_file():
                    selected_file_html = "<p style='color:#b00020;'>ファイルが見つかりません。</p>"
                else:
                    try:
                        raw_lines = target.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeDecodeError):
                        selected_file_html = "<p style='color:#b00020;'>テキストとして読み込めませんでした。</p>"
                    else:
                        if view_from > len(raw_lines):
                            selected_file_html = (
                                f"<h4>{escape(selected_file_path_normalized)}</h4>"
                                f"<p style='color:#b00020;'>指定した行範囲（from={view_from}）はファイル行数（{len(raw_lines)}）を超えています。</p>"
                            )
                        else:
                            actual_to = min(view_to, view_from + MAX_VIEW_LINES - 1, len(raw_lines))
                            selected = raw_lines[view_from - 1 : actual_to]
                            line_number_width = len(str(actual_to if selected else view_from))
                            rendered = "\n".join(
                                f"{number:>{line_number_width}}| {escape(line)}"
                                for number, line in enumerate(selected, start=view_from)
                            )
                            next_link = ""
                            if len(raw_lines) > actual_to:
                                next_from = actual_to + 1
                                next_to = next_from + (DEFAULT_VIEW_TO - DEFAULT_VIEW_FROM)
                                next_link = (
                                    f"<p><a href='{escape(_admin_url(token=selected_token, path=selected_file_path_normalized, from_line=next_from, to_line=next_to))}'>"
                                    f"続きを表示 ({next_from}-{next_to})</a></p>"
                                )
                            selected_file_html = (
                                f"<h4>{escape(selected_file_path_normalized)}</h4>"
                                "<div style='display:flex;gap:8px;flex-wrap:wrap;'>"
                                f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/hide'>"
                                f"<input type='hidden' name='path' value='{escape(selected_file_path_normalized)}'>"
                                "<button type='submit'>隠す</button></form>"
                                f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/files/delete' onsubmit='return confirm(\"このファイルを削除します。元に戻せません。\")'>"
                                f"<input type='hidden' name='path' value='{escape(selected_file_path_normalized)}'>"
                                "<button type='submit'>削除</button></form>"
                                f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/folders/assign' style='display:flex;gap:4px;align-items:center;'>"
                                f"<input type='hidden' name='path' value='{escape(selected_file_path_normalized)}'>"
                                "<label><span style='display:none;'>Folder</span>"
                                "<select name='folder_id'>"
                                + "".join(
                                    f"<option value='{escape(str(folder['id']))}'>{escape(str(folder['name']))}</option>"
                                    for folder in folders
                                    if str(folder["id"]) not in {"hidden"}
                                )
                                + "</select></label>"
                                "<button type='submit'>フォルダに追加</button></form>"
                                "</div>"
                                f"<p><small>表示行: {view_from}-{actual_to} / 全{len(raw_lines)}行</small></p>"
                                f"<pre style='white-space:pre;overflow:auto;border:1px solid #ddd;padding:8px;'>{rendered}</pre>"
                                f"{next_link}"
                            )
                            selected_file_url = (
                                f"{base_url}/t/{selected_token}/file?path={quote_plus(selected_file_path_normalized)}&from=1&to=600"
                            )

        folder_nav_items = [
            f"<li><a href='{escape(_admin_url(token=selected_token, folder='all'))}'>{'✓ ' if selected_folder == 'all' else ''}すべて</a></li>"
        ]
        for folder in folders:
            folder_id = str(folder["id"])
            visible_marker = "✓ " if selected_folder == folder_id else ""
            toggle_label = "非表示" if folder.get("visible") is not False else "表示"
            if folder.get("visible") is False and selected_folder != folder_id:
                continue
            folder_nav_items.append(
                "<li>"
                f"<a href='{escape(_admin_url(token=selected_token, folder=folder_id))}'>{visible_marker}{escape(str(folder['name']))}</a> "
                f"<small>({len(folder_paths.get(folder_id, []))})</small> "
                f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/folders/toggle-visibility' style='display:inline;'>"
                f"<input type='hidden' name='folder_id' value='{escape(folder_id)}'>"
                f"<button type='submit'>{toggle_label}</button></form>"
                f"<button type='button' onclick=\"copyText('{escape(base_url)}/t/{escape(selected_token)}/share/folder-{escape(folder_id)}/index')\">このフォルダをAIにシェア</button>"
                "</li>"
            )

        visible_list = "".join(
            "<li>"
            f"<label><input type='checkbox' form='share-form' name='paths' value='{escape(path)}'> {escape(path)}</label> "
            f"<a href='{escape(_admin_url(token=selected_token, path=path, from_line=1, to_line=DEFAULT_VIEW_TO, folder=selected_folder if selected_folder != 'all' else None))}'>表示</a>"
            "</li>"
            for path in listed_files
        )
        hidden_list = "".join(
            "<li>"
            f"{escape(path)} "
            f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/unhide' style='display:inline;'>"
            f"<input type='hidden' name='path' value='{escape(path)}'>"
            "<button type='submit'>戻す</button></form>"
            "</li>"
            for path in hidden_files
        )

        index_url = f"{base_url}/t/{selected_token}/index"
        file_url = selected_file_url or f"{base_url}/t/{selected_token}/file?path=README.md&from=1&to=600"
        symbol_url = f"{base_url}/t/{selected_token}/symbol?path=src/main.py&name=main"
        changes_url = f"{base_url}/t/{selected_token}/changes?since={quote_plus(changes_since_timestamp)}"
        ai_template = (
            "以下のCode Memo URLからコードを確認してください。\n"
            "まずindexを読んで全体像を把握し、必要なファイルはfile URLで分割して読んでください。\n\n"
            f"{index_url}"
        )
        share_items = "".join(
            "<li>"
            f"<strong>{escape(str(share['name']))}</strong> "
            f"<button type='button' onclick=\"copyText('{escape(base_url)}/t/{escape(selected_token)}/share/{escape(str(share['id']))}/index')\">URLをコピー</button>"
            f"<br><small>含める: {', '.join(escape(path) for path in share['paths']) or '-'} / "
            f"除外: {', '.join(escape(path) for path in share['excluded_paths']) or '-'}</small>"
            "</li>"
            for share in shares
        )
        folder_options = "".join(
            f"<option value='{escape(str(folder['id']))}'>{escape(str(folder['name']))}</option>"
            for folder in folders
            if str(folder["id"]) not in {"hidden"}
        )
        default_excluded_paths_text = "\n".join(DEFAULT_SHARE_EXCLUDED_PATHS)

        notebook_block = (
            f"<h2>{escape(name)}</h2>"
            f"<div>token: <code>{escape(selected_token)}</code></div>"
            f"<div>files: {len(visible_files)}（hidden: {len(hidden_files)}）</div>"
            f"<div>lines: {index_data.get('total_lines', 0)}</div>"
            "<div style='display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;'>"
            "<form method='post' action='/admin/pipes/create' enctype='multipart/form-data' style='display:flex;gap:8px;align-items:center;'>"
            "<input type='hidden' name='source_type' value='zip'>"
            "<input type='hidden' name='name' value='Code Memo'>"
            "<label style='display:inline-flex;align-items:center;gap:6px;'>"
            "<span style='display:none;'>ZIP</span>"
            "<input type='file' name='file' accept='.zip,application/zip' required>"
            "</label>"
            "<button type='submit' style='min-height:44px;padding:0 14px;'>ファイルを追加</button>"
            "</form>"
            "<a href='#share' style='display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 14px;border:1px solid #666;text-decoration:none;'>シェア</a>"
            "</div>"
            "<h3>フォルダ</h3>"
            f"<ul>{''.join(folder_nav_items)}</ul>"
            "<div style='display:flex;gap:8px;flex-wrap:wrap;'>"
            f"<form method='post' action='/admin/pipes/{quote_plus(selected_token)}/folders/create' style='display:flex;gap:6px;'>"
            "<input type='text' name='name' placeholder='新しいフォルダ名' required>"
            "<button type='submit'>フォルダ作成</button></form>"
            "</div>"
            "<h3>ファイル一覧</h3>"
            f"<ul>{visible_list if visible_list else '<li>表示対象のファイルはありません。</li>'}</ul>"
            f"<form id='share-form' method='post' action='/admin/pipes/{quote_plus(selected_token)}/shares/create' style='display:grid;gap:8px;margin:8px 0;'>"
            "<div>選択した複数ファイルだけをシェア</div>"
            "<input type='text' name='name' value='AIに見せるセット' placeholder='セット名' required>"
            f"<label>除外するパス（改行区切り）<textarea name='excluded_paths' rows='3'>{escape(default_excluded_paths_text)}</textarea></label>"
            f"<label>同時に追加するフォルダ<select name='folder_id'>{folder_options}</select></label>"
            "<button type='submit'>AIに見せるセットを作成</button>"
            "</form>"
            "<h3>隠したファイル</h3>"
            f"<ul>{hidden_list if hidden_list else '<li>まだありません。</li>'}</ul>"
            "<h3>ファイル表示</h3>"
            f"{selected_file_html}"
            "<h3 id='share'>シェア</h3>"
            "<div style='display:grid;gap:8px;'>"
            "<div>AIに貼るURL</div>"
            f"<input id='index-url' type='text' readonly value='{escape(index_url)}'>"
            "<button type='button' onclick=\"copyById('index-url')\">index URLをコピー</button>"
            "<div>AIに貼る文章</div>"
            f"<textarea id='ai-template' readonly rows='5'>{escape(ai_template)}</textarea>"
            "<button type='button' onclick=\"copyById('ai-template')\">テンプレートをコピー</button>"
            "<div>選択中のファイルだけ見せる</div>"
            f"<input id='file-url' type='text' readonly value='{escape(file_url)}'>"
            "<button type='button' onclick=\"copyById('file-url')\">file URLをコピー</button>"
            "<div>フォルダ共有URL（選択中）</div>"
            f"<input id='folder-url' type='text' readonly value='{escape(base_url)}/t/{escape(selected_token)}/share/folder-{escape(selected_folder or 'all')}/index'>"
            "<button type='button' onclick=\"copyById('folder-url')\">フォルダURLをコピー</button>"
            "</div>"
            "<h4>AIに見せるセット</h4>"
            f"<ul>{share_items if share_items else '<li>まだありません。</li>'}</ul>"
            "<details style='margin-top:12px;'><summary>上級者向けURL（既存機能）</summary>"
            f"<div>symbol URL 例: <input type='text' readonly value='{escape(symbol_url)}' size='96'></div>"
            f"<div>changes URL 例: <input type='text' readonly value='{escape(changes_url)}' size='96'></div>"
            "</details>"
        )

    error_block = f"<p style='color:#b00020;'>{escape(error_message)}</p>" if error_message else ""
    info_block = f"<p style='color:#0b6e4f;'>{escape(info_message)}</p>" if info_message else ""

    body = (
        "<!doctype html>"
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Code Memo</title>"
        "<style>body{font-family:sans-serif;line-height:1.5;padding:12px;max-width:980px;margin:0 auto;}"
        "button,input,textarea{font-size:16px;} ul{padding-left:18px;} code{word-break:break-all;}</style>"
        "<script>function copyById(id){var el=document.getElementById(id);if(!el){return;}"
        "var text=(el.value!==undefined)?el.value:el.textContent;copyText(text,el);}"
        "function copyText(text,el){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text);}"
        "else if(el){el.focus();if(el.select){el.select();}document.execCommand('copy');}}</script>"
        "</head><body>"
        "<h1>Code Memo</h1>"
        "<p>ZIPを追加すると、コードをアプリ内で読めます。</p>"
        "<div style='display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;'>"
        "<form method='post' action='/admin/pipes/create' enctype='multipart/form-data' style='display:flex;gap:8px;align-items:center;'>"
        "<input type='hidden' name='source_type' value='zip'>"
        "<input type='text' name='name' placeholder='メモ名（任意）' value='Code Memo' style='max-width:180px;'>"
        "<input type='file' name='file' accept='.zip,application/zip' required>"
        "<button type='submit' style='min-height:44px;padding:0 14px;'>ファイルを追加</button>"
        "</form>"
        "<a href='#share' style='display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 14px;border:1px solid #666;text-decoration:none;'>シェア</a>"
        "</div>"
        f"{error_block}{info_block}"
        f"{notebook_block if notebook_block else '<p>まだメモがありません。ZIPを追加してください。</p>'}"
        "<h2>保存済みメモ</h2>"
        f"<ul>{''.join(rows) if rows else '<li>まだメモがありません。</li>'}</ul>"
        "</body></html>"
    )
    return HTMLResponse(body)


@router.get("/", response_class=HTMLResponse)
@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    return _render_admin_page(request)


@router.post("/admin/pipes/create")
async def create_pipe(
    request: Request,
    name: str = Form(...),
    source_type: str = Form(...),
    repository_url: str | None = Form(default=None),
    access_token: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
):
    stripped_name = name.strip()
    if not stripped_name:
        return _render_admin_page(request, "メモ名を入力してください。")

    if source_type == "repo":
        if repository_url is None or not repository_url.strip():
            return _render_admin_page(request, "repository_url を入力してください。")
        response = await ingest_repo(repository_url=repository_url, access_token=access_token)
    elif source_type == "zip":
        if file is None:
            return _render_admin_page(request, "ZIPファイルを選択してください。")
        response = await ingest(file=file)
    else:
        return _render_admin_page(request, "source_type は repo か zip を選択してください。")

    if response.status_code != 200:
        return _render_admin_page(request, response.body.decode("utf-8"))

    token = _token_from_response(response.body.decode("utf-8"))
    if token is not None:
        update_token_metadata(
            token,
            name=stripped_name,
            source_type=source_type,
            repository_url=repository_url if source_type == "repo" else None,
            hidden_paths=[],
            folders=_default_folder_entries(),
            shares=[],
        )
        return RedirectResponse(url=_admin_url(token=token), status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/pipes/{token}/hide")
async def hide_file(request: Request, token: str, path: str = Form(...)) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)

    workspace = _workspace_path(record)
    normalized = _normalize_requested_path(path.strip())
    if workspace is None or normalized is None:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)

    normalized_path = normalized.as_posix()
    hidden = _hidden_paths(record)
    hidden.add(normalized_path)
    _set_hidden_paths(token, hidden)
    _apply_hidden_flags(workspace, hidden)
    return RedirectResponse(url=_admin_url(token=token), status_code=303)


@router.post("/admin/pipes/{token}/unhide")
async def unhide_file(request: Request, token: str, path: str = Form(...)) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)

    workspace = _workspace_path(record)
    normalized = _normalize_requested_path(path.strip())
    if workspace is None or normalized is None:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)

    normalized_path = normalized.as_posix()
    hidden = _hidden_paths(record)
    hidden.discard(normalized_path)
    _set_hidden_paths(token, hidden)
    _apply_hidden_flags(workspace, hidden)
    return RedirectResponse(url=_admin_url(token=token, path=normalized_path), status_code=303)


@router.post("/admin/pipes/{token}/folders/create")
async def create_folder(request: Request, token: str, name: str = Form(...)) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)
    folder_name = name.strip()
    if not folder_name:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)
    folders = _folder_entries(record)
    folder_id = _slugify_identifier(folder_name)
    existing_ids = {str(item["id"]) for item in folders}
    if folder_id in existing_ids:
        counter = 2
        while f"{folder_id}-{counter}" in existing_ids:
            counter += 1
        folder_id = f"{folder_id}-{counter}"
    folders.append({"id": folder_id, "name": folder_name, "paths": [], "visible": True})
    _save_folder_entries(token, folders)
    return RedirectResponse(url=_admin_url(token=token, folder=folder_id), status_code=303)


@router.post("/admin/pipes/{token}/folders/toggle-visibility")
async def toggle_folder_visibility(request: Request, token: str, folder_id: str = Form(...)) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)
    normalized_folder_id = _slugify_identifier(folder_id)
    folders = _folder_entries(record)
    for folder in folders:
        if str(folder["id"]) != normalized_folder_id:
            continue
        folder["visible"] = folder.get("visible") is False
        break
    _save_folder_entries(token, folders)
    return RedirectResponse(url=_admin_url(token=token, folder=normalized_folder_id), status_code=303)


@router.post("/admin/pipes/{token}/folders/assign")
async def assign_files_to_folder(
    request: Request,
    token: str,
    folder_id: str = Form(...),
    path: str | None = Form(default=None),
) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)
    form = await request.form()
    selected_paths = list(form.getlist("paths"))
    if path:
        selected_paths.append(path)
    normalized_paths = _normalize_paths(selected_paths)
    if not normalized_paths:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)

    normalized_folder_id = _slugify_identifier(folder_id)
    folders = _folder_entries(record)
    for folder in folders:
        if str(folder["id"]) != normalized_folder_id:
            continue
        merged_paths = sorted(set(_normalize_paths(folder.get("paths"))).union(normalized_paths))
        folder["paths"] = merged_paths
        break
    _save_folder_entries(token, folders)
    return RedirectResponse(url=_admin_url(token=token, folder=normalized_folder_id), status_code=303)


@router.post("/admin/pipes/{token}/shares/create")
async def create_share_set(
    request: Request,
    token: str,
    name: str = Form(...),
    folder_id: str | None = Form(default=None),
    excluded_paths: str | None = Form(default=None),
) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)
    form = await request.form()
    selected_paths = _normalize_paths(list(form.getlist("paths")))
    if not selected_paths:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)

    share_name = name.strip() or "AIに見せるセット"
    share_id = _slugify_identifier(share_name)
    shares = _share_entries(record)
    existing_ids = {str(item["id"]) for item in shares}
    if share_id in existing_ids:
        counter = 2
        while f"{share_id}-{counter}" in existing_ids:
            counter += 1
        share_id = f"{share_id}-{counter}"

    excluded_list = _normalize_excluded_paths((excluded_paths or "").splitlines())
    shares.append({"id": share_id, "name": share_name, "paths": selected_paths, "excluded_paths": excluded_list})
    _save_share_entries(token, shares)

    if folder_id:
        normalized_folder_id = _slugify_identifier(folder_id)
        folders = _folder_entries(record)
        for folder in folders:
            if str(folder["id"]) != normalized_folder_id:
                continue
            folder["paths"] = sorted(set(_normalize_paths(folder.get("paths"))).union(selected_paths))
            break
        _save_folder_entries(token, folders)

    return RedirectResponse(url=_admin_url(token=token, folder=folder_id), status_code=303)


@router.post("/admin/pipes/{token}/files/delete")
async def delete_file(request: Request, token: str, path: str = Form(...)) -> RedirectResponse:
    record = list_tokens().get(token)
    if not record:
        return RedirectResponse(url="/admin", status_code=303)

    workspace = _workspace_path(record)
    normalized = _normalize_requested_path(path.strip())
    if workspace is None or normalized is None:
        return RedirectResponse(url=_admin_url(token=token), status_code=303)

    target = _resolve_safe_file_path(workspace, normalized)
    if target is not None and target.is_file():
        try:
            target.unlink()
        except OSError:
            return RedirectResponse(url=_admin_url(token=token, path=normalized.as_posix()), status_code=303)

        parent = target.parent
        workspace_resolved = workspace.resolve()
        while parent != workspace_resolved and workspace_resolved in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

        hidden = _hidden_paths(record)
        deleted_path = normalized.as_posix()
        hidden.discard(deleted_path)
        _set_hidden_paths(token, hidden)
        folders = _folder_entries(record)
        for folder in folders:
            folder["paths"] = [item for item in _normalize_paths(folder.get("paths")) if item != deleted_path]
        _save_folder_entries(token, folders)
        shares = _share_entries(record)
        for share in shares:
            share["paths"] = [item for item in _normalize_paths(share.get("paths")) if item != deleted_path]
        _save_share_entries(token, shares)
        _refresh_index_with_hidden(workspace, hidden)

    return RedirectResponse(url=_admin_url(token=token), status_code=303)


@router.post("/admin/pipes/{token}/revoke")
async def revoke_pipe(token: str) -> RedirectResponse:
    revoke_token(token)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/pipes/{token}/delete")
async def delete_pipe(token: str) -> RedirectResponse:
    delete_token(token)
    return RedirectResponse(url="/admin", status_code=303)
