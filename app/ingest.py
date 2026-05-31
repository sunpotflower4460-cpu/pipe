import io
import os
import re
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import PlainTextResponse

from app.config import (
    MAX_UNCOMPRESSED_BYTES,
    MAX_WORKSPACE_ALLOCATION_RETRIES,
    MAX_ZIP_BYTES,
    MAX_ZIP_ENTRIES,
    WORKSPACE_ROOT,
)
from app.index import build_index
from app.responses import error, plain_text
from app.tokens import generate_token, register_token

router = APIRouter()

_WINDOWS_DRIVE_PATTERN = re.compile(r"^[a-zA-Z]:")


def _normalized_member_path(name: str) -> PurePosixPath:
    return PurePosixPath(name.replace("\\", "/"))


def _workspace_path_for_entry(name: str, workspace_dir: Path) -> Path:
    return workspace_dir / Path(*_normalized_member_path(name).parts)


def _is_unsafe_entry_path(name: str, workspace_dir: Path) -> bool:
    normalized = name.replace("\\", "/")
    path = _normalized_member_path(name)

    if not normalized or normalized.startswith("/") or path.is_absolute():
        return True
    if _WINDOWS_DRIVE_PATTERN.match(normalized):
        return True
    if any(part in {"", ".", ".."} for part in path.parts):
        return True

    target_path = _workspace_path_for_entry(name, workspace_dir).resolve()
    workspace_realpath = workspace_dir.resolve()
    if workspace_realpath not in target_path.parents:
        return True
    return False


def _is_symlink_entry(entry: zipfile.ZipInfo) -> bool:
    mode = entry.external_attr >> 16
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _create_workspace_dir() -> tuple[str, Path]:
    for _ in range(MAX_WORKSPACE_ALLOCATION_RETRIES):
        token = generate_token()
        workspace_dir = WORKSPACE_ROOT / token
        try:
            workspace_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return token, workspace_dir
    raise RuntimeError("failed to allocate workspace directory")


def _sanitize_repository_url(repository_url: str) -> str | None:
    parsed = urlparse(repository_url.strip())
    if parsed.scheme != "https":
        return None
    if not parsed.netloc or parsed.username or parsed.password:
        return None
    if parsed.query or parsed.fragment or parsed.params:
        return None
    if not parsed.hostname:
        return None
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
        parsed.hostname,
    ):
        return None
    if not parsed.path or not re.fullmatch(r"/[A-Za-z0-9._/-]+", parsed.path):
        return None
    if any(segment in {"", ".", ".."} for segment in parsed.path.split("/")[1:]):
        return None

    netloc = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}{parsed.path}"


def _clone_repository(repository_url: str, destination: Path, access_token: str | None) -> bool:
    command = ["git", "clone", "--depth", "1", "--", repository_url, str(destination)]
    env = {
        key: value
        for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if (value := os.environ.get(key)) is not None
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if access_token:
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = "Authorization: " + "Bearer " + access_token
    try:
        result = subprocess.run(command, check=False, capture_output=True, env=env)
    except OSError:
        return False
    return result.returncode == 0


@router.post("/ingest")
async def ingest(file: UploadFile = File(...)) -> PlainTextResponse:
    filename = file.filename or ""
    if not filename.lower().endswith(".zip"):
        return error("invalid zip file type", 400)

    payload = await file.read(MAX_ZIP_BYTES + 1)
    if len(payload) > MAX_ZIP_BYTES:
        return error("zip file too large", 400)

    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return error("invalid zip file", 400)

    try:
        token, workspace_dir = _create_workspace_dir()
    except RuntimeError:
        return error("failed to allocate workspace", 500)

    def cleanup_error(message: str) -> PlainTextResponse:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        return error(message, 400)

    try:
        entries = archive.infolist()
        if not entries:
            return cleanup_error("empty zip file")
        if len(entries) > MAX_ZIP_ENTRIES:
            return cleanup_error("zip has too many entries")

        total_uncompressed = 0
        file_entries = 0

        for entry in entries:
            if _is_unsafe_entry_path(entry.filename, workspace_dir):
                return cleanup_error(f"unsafe zip entry path: {entry.filename}")
            if _is_symlink_entry(entry):
                return cleanup_error(f"unsafe zip entry type: {entry.filename}")

            if not entry.is_dir():
                file_entries += 1
                total_uncompressed += entry.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    return cleanup_error("zip content too large")

        if file_entries == 0:
            return cleanup_error("empty zip file")

        for entry in entries:
            if entry.is_dir():
                target_dir = _workspace_path_for_entry(entry.filename, workspace_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                continue

            destination = _workspace_path_for_entry(entry.filename, workspace_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as src, destination.open("wb") as dst:
                shutil.copyfileobj(src, dst)

        build_index(workspace_dir)
        register_token(token, workspace_dir)
    except Exception:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise
    finally:
        archive.close()

    body = f"TOKEN={token}\nINDEX=/t/{token}/index"
    return plain_text(body, status_code=200)


@router.post("/ingest-repo")
async def ingest_repo(
    repository_url: str = Form(...),
    access_token: str | None = Form(default=None),
) -> PlainTextResponse:
    sanitized_repository_url = _sanitize_repository_url(repository_url)
    stripped_access_token = access_token.strip() if access_token else None
    access_token = stripped_access_token if stripped_access_token else None
    if sanitized_repository_url is None:
        return error("invalid repository url", 400)

    try:
        token, workspace_dir = _create_workspace_dir()
    except RuntimeError:
        return error("failed to allocate workspace", 500)

    if not _clone_repository(sanitized_repository_url, workspace_dir, access_token):
        shutil.rmtree(workspace_dir, ignore_errors=True)
        return error("failed to clone repository", 400)

    try:
        build_index(workspace_dir)
        register_token(token, workspace_dir)
    except Exception:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise

    body = f"TOKEN={token}\nINDEX=/t/{token}/index"
    return plain_text(body, status_code=200)
