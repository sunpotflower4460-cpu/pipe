import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import quote_plus

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


def _admin_url(token: str | None = None, path: str | None = None, from_line: int | None = None, to_line: int | None = None) -> str:
    params: list[str] = []
    if token:
        params.append(f"token={quote_plus(token)}")
    if path:
        params.append(f"path={quote_plus(path)}")
    if from_line is not None:
        params.append(f"from={from_line}")
    if to_line is not None:
        params.append(f"to={to_line}")
    if not params:
        return "/admin"
    return "/admin?" + "&".join(params)


def _render_admin_page(request: Request, error_message: str | None = None, info_message: str | None = None) -> HTMLResponse:
    base_url = _public_base(request)
    changes_since_timestamp = (_utc_now() - timedelta(days=CHANGES_EXAMPLE_LOOKBACK_DAYS)).isoformat()

    selected_token = request.query_params.get("token")
    selected_path = request.query_params.get("path")
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
                            "</div>"
                            f"<p><small>表示行: {view_from}-{actual_to} / 全{len(raw_lines)}行</small></p>"
                            f"<pre style='white-space:pre;overflow:auto;border:1px solid #ddd;padding:8px;'>{rendered}</pre>"
                            f"{next_link}"
                        )
                        selected_file_url = (
                            f"{base_url}/t/{selected_token}/file?path={quote_plus(selected_file_path_normalized)}&from=1&to=600"
                        )

        visible_list = "".join(
            f"<li><a href='{escape(_admin_url(token=selected_token, path=path, from_line=1, to_line=DEFAULT_VIEW_TO))}'>{escape(path)}</a></li>"
            for path in visible_files
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
            "<h3>ファイル一覧</h3>"
            f"<ul>{visible_list if visible_list else '<li>表示対象のファイルはありません。</li>'}</ul>"
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
            "</div>"
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
        "var text=(el.value!==undefined)?el.value:el.textContent;"
        "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text);}"
        "else{el.focus();if(el.select){el.select();}document.execCommand('copy');}}</script>"
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
        while parent != workspace_resolved:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

        hidden = _hidden_paths(record)
        hidden.discard(normalized.as_posix())
        _set_hidden_paths(token, hidden)
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
