import json
import secrets
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import BASE_DIR, TOKEN_TTL_DAYS, TOKENS_FILE, WORKSPACE_ROOT


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_tokens() -> dict[str, dict[str, Any]]:
    if not TOKENS_FILE.exists():
        return {}

    try:
        data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    return data if isinstance(data, dict) else {}


def _save_tokens(tokens: dict[str, dict[str, Any]]) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = TOKENS_FILE.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(TOKENS_FILE)


def register_token(token: str, workspace_dir: Path) -> None:
    now = _utc_now()
    expires_at = now + timedelta(days=TOKEN_TTL_DAYS)
    workspace_resolved = workspace_dir.resolve()
    try:
        workspace_path = workspace_resolved.relative_to(BASE_DIR.resolve()).as_posix()
    except ValueError:
        workspace_path = workspace_resolved.as_posix()

    tokens = _load_tokens()
    tokens[token] = {
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "revoked": False,
        "workspace_path": workspace_path,
    }
    _save_tokens(tokens)


def update_token_metadata(
    token: str,
    *,
    name: str | None = None,
    source_type: str | None = None,
    repository_url: str | None = None,
) -> bool:
    tokens = _load_tokens()
    record = tokens.get(token)
    if not record:
        return False

    if name is not None:
        stripped_name = name.strip()
        if stripped_name:
            record["name"] = stripped_name
    if source_type is not None:
        record["source_type"] = source_type
    if repository_url is not None:
        record["repository_url"] = repository_url

    tokens[token] = record
    _save_tokens(tokens)
    return True


def revoke_token(token: str) -> bool:
    tokens = _load_tokens()
    record = tokens.get(token)
    if not record:
        return False
    if record.get("revoked") is True:
        return False

    _remove_workspace(record)
    record["revoked"] = True
    tokens[token] = record
    _save_tokens(tokens)
    return True


def delete_token(token: str) -> bool:
    tokens = _load_tokens()
    record = tokens.pop(token, None)
    if record is None:
        return False
    _remove_workspace(record)
    _save_tokens(tokens)
    return True


def list_tokens() -> dict[str, dict[str, Any]]:
    return _load_tokens()


def resolve_workspace_for_access(token: str) -> Path | None:
    tokens = _load_tokens()
    record = tokens.get(token)
    if not record:
        return None

    if record.get("revoked") is True:
        return None

    try:
        expires_at = datetime.fromisoformat(str(record["expires_at"]))
    except (KeyError, ValueError, TypeError):
        return None

    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at <= _utc_now():
        _remove_workspace(record)
        record["revoked"] = True
        tokens[token] = record
        _save_tokens(tokens)
        return None

    workspace = _workspace_from_record(record)
    if workspace is None or not workspace.is_dir():
        return None
    return workspace


def _workspace_from_record(record: dict[str, Any]) -> Path | None:
    raw_path = record.get("workspace_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None

    raw = Path(raw_path)
    candidate = raw.resolve() if raw.is_absolute() else (BASE_DIR / raw).resolve()
    workspace_root_resolved = WORKSPACE_ROOT.resolve()
    if candidate != workspace_root_resolved and workspace_root_resolved not in candidate.parents:
        return None
    return candidate


def _remove_workspace(record: dict[str, Any]) -> None:
    workspace = _workspace_from_record(record)
    if workspace is None:
        return
    shutil.rmtree(workspace, ignore_errors=True)
