import io
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, UploadFile
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
