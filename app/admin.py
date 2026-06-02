import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import BASE_DIR, BASE_PUBLIC_URL, WORKSPACE_ROOT
from app.ingest import ingest, ingest_repo
from app.tokens import delete_token, list_tokens, revoke_token, update_token_metadata

router = APIRouter()
CHANGES_EXAMPLE_LOOKBACK_DAYS = 1
TOKEN_DISPLAY_PREFIX_LENGTH = 8


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


def _stats(record: dict[str, object]) -> tuple[int, int]:
    workspace = _workspace_path(record)
    if workspace is None:
        return 0, 0
    index_path = workspace / "index.json"
    if not index_path.is_file():
        return 0, 0
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0
    total_files = data.get("total_files", 0)
    total_lines = data.get("total_lines", 0)
    if not isinstance(total_files, int):
        total_files = 0
    if not isinstance(total_lines, int):
        total_lines = 0
    return total_files, total_lines


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


def _render_admin_page(request: Request, error_message: str | None = None) -> HTMLResponse:
    base_url = _public_base(request)
    changes_since_timestamp = (_utc_now() - timedelta(days=CHANGES_EXAMPLE_LOOKBACK_DAYS)).isoformat()

    rows: list[str] = []
    records = sorted(
        list_tokens().items(),
        key=lambda item: str(item[1].get("created_at", "")),
        reverse=True,
    )
    for token, record in records:
        total_files, total_lines = _stats(record)
        state = _status(record)
        source_type = record.get("source_type")
        if not isinstance(source_type, str) or not source_type:
            source_type = "unknown"
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            name = token[:TOKEN_DISPLAY_PREFIX_LENGTH]
        repository_url = record.get("repository_url")
        repository_label = (
            f"<div>GitHubリポジトリURL: {escape(repository_url)}</div>"
            if isinstance(repository_url, str) and repository_url
            else ""
        )
        encoded_token = quote_plus(token)
        index_url = f"{base_url}/t/{token}/index"
        file_url = f"{base_url}/t/{token}/file?path=README.md&from=1&to=200"
        symbol_url = f"{base_url}/t/{token}/symbol?path=src/main.py&name=main"
        changes_url = f"{base_url}/t/{token}/changes?since={quote_plus(changes_since_timestamp)}"
        claude_template = (
            "以下のCode Relay URLからコード全体を確認してください。\n"
            "まず /index を読んで構成を把握し、必要なファイルは "
            "/file?path=...&from=...&to=... で分割して読んでください。\n\n"
            f"{index_url}"
        )
        revoke_disabled = "disabled" if state != "active" else ""
        rows.append(
            "<li>"
            f"<h3>{escape(name)}</h3>"
            f"<div>取り込み方法: {escape(source_type)}</div>"
            f"{repository_label}"
            f"<div>作成日時: {_display_time(record.get('created_at'))}</div>"
            f"<div>有効期限: {_display_time(record.get('expires_at'))}</div>"
            f"<div>状態: {escape(state)}</div>"
            f"<div>files: {total_files}</div>"
            f"<div>total_lines: {total_lines}</div>"
            "<div>次にやること: まず index URL をAIへ渡し、必要に応じて file/symbol/changes URL を使い、最後に revoke で止めます。</div>"
            f"<div>index URL（最初にAIへ渡すURL。コード全体の地図を見せます）: <input type='text' readonly value='{escape(index_url)}' size='96'></div>"
            f"<div>file URL 例（特定のファイルを行番号付きで読ませるURL）: <input type='text' readonly value='{escape(file_url)}' size='96'></div>"
            f"<div>symbol URL 例（関数やクラスだけを読ませたい時のURL）: <input type='text' readonly value='{escape(symbol_url)}' size='96'></div>"
            f"<div>changes URL 例（前回から変わったファイルだけを見せるURL）: <input type='text' readonly value='{escape(changes_url)}' size='96'></div>"
            "<div>Claudeに貼る文章テンプレート:</div>"
            f"<div><textarea readonly rows='5' cols='96'>{escape(claude_template)}</textarea></div>"
            "<div style='margin-top:8px;'>"
            f"<form method='post' action='/admin/pipes/{encoded_token}/revoke' style='display:inline;'>"
            f"<button type='submit' {revoke_disabled}>revoke</button>"
            "</form> "
            f"<form method='post' action='/admin/pipes/{encoded_token}/delete' style='display:inline;'>"
            "<button type='submit'>delete</button>"
            "</form>"
            "</div>"
            "<div><small>revoke: このパイプを止めます。止めるとAIはこのURLからコードを読めなくなります。</small></div>"
            "</li>"
        )

    error_block = f"<p style='color:#b00020;'>{escape(error_message)}</p>" if error_message else ""
    body = (
        "<!doctype html>"
        "<html><head><meta charset='utf-8'><title>Code Relay Admin</title></head><body>"
        "<h1>Code Relay 管理画面</h1>"
        "<p>Code Relayは、ClaudeやChatGPTにコードを読んでもらうための中継アプリです。"
        "ZIPまたはGitHubリポジトリURLを登録すると、AIに渡せる専用URLが作られます。</p>"
        "<p>最初に開く場所はこの管理画面です。新しいパイプを作って、index URLをClaudeへ渡すところから始めます。</p>"
        f"{error_block}"
        "<h2>新しいパイプを作る</h2>"
        "<form method='post' action='/admin/pipes/create' enctype='multipart/form-data'>"
        "<div><label>パイプ名: <input type='text' name='name' required></label></div>"
        "<div><label><input type='radio' name='source_type' value='repo' checked> リポジトリURL</label>"
        "<label><input type='radio' name='source_type' value='zip'> ZIPアップロード</label></div>"
        "<div><label>GitHubリポジトリURL: <input type='url' name='repository_url' placeholder='https://github.com/owner/repo.git'></label></div>"
        "<div><label>GitHub read-only token（任意）: <input type='password' name='access_token'></label>"
        "<small> この入力は保存・表示・ログ出力しません。</small></div>"
        "<div><label>コードZIPファイル: <input type='file' name='file' accept='.zip,application/zip'></label></div>"
        "<div><button type='submit'>新しいパイプを作る</button></div>"
        "</form>"
        "<h2>保存済みパイプ一覧</h2>"
        f"<ul>{''.join(rows) if rows else '<li>まだパイプがありません。</li>'}</ul>"
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
        return _render_admin_page(request, "パイプ名を入力してください。")

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
        )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/pipes/{token}/revoke")
async def revoke_pipe(token: str) -> RedirectResponse:
    revoke_token(token)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/pipes/{token}/delete")
async def delete_pipe(token: str) -> RedirectResponse:
    delete_token(token)
    return RedirectResponse(url="/admin", status_code=303)
