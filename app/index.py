import json
import os
from datetime import datetime, timezone
import hashlib
from pathlib import Path, PurePosixPath

EXCLUDED_DIRECTORIES = {
    ".git",
    "node_modules",
    "build",
    "dist",
    ".next",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

EXCLUDED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
}

EXCLUDED_FILE_EXTENSIONS = {
    ".pem",
    ".key",
    ".p12",
    ".crt",
    ".cer",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".wav",
    ".mp3",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp4",
    ".mov",
    ".webm",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
}

TEXT_SNIFF_BYTES = 4096


def _normalized_index_path(path: Path, workspace_dir: Path) -> str | None:
    relative = path.relative_to(workspace_dir).as_posix()
    normalized = PurePosixPath(relative)
    if normalized.is_absolute() or any(part in {".", ".."} for part in normalized.parts):
        return None
    return normalized.as_posix()


def _is_excluded_file(path: Path) -> bool:
    name = path.name.lower()
    if name in EXCLUDED_FILE_NAMES:
        return True
    return path.suffix.lower() in EXCLUDED_FILE_EXTENSIONS


def build_index(workspace_dir: Path) -> None:
    files: list[dict[str, int | str | bool]] = []
    errors: list[dict[str, str]] = []
    total_lines = 0
    total_bytes = 0
    generated_at = datetime.now(timezone.utc).isoformat()
    index_path = workspace_dir / "index.json"
    previous_files: dict[str, dict[str, int | str | bool]] = {}
    previous_history: list[dict[str, int | str]] = []

    if index_path.is_file():
        try:
            previous_index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, AttributeError):
            previous_index = {}
        for item in previous_index.get("files", []):
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str):
                previous_files[path] = item
        raw_history = previous_index.get("change_history", [])
        if isinstance(raw_history, list):
            previous_history = [item for item in raw_history if isinstance(item, dict)]

    for root, dirs, file_names in os.walk(workspace_dir):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRECTORIES]
        root_path = Path(root)
        for file_name in sorted(file_names):
            path = root_path / file_name
            normalized_path = _normalized_index_path(path, workspace_dir)
            if normalized_path is None or normalized_path == "index.json":
                continue
            if _is_excluded_file(path):
                continue

            try:
                payload = path.read_bytes()
            except OSError:
                errors.append({"path": normalized_path, "reason": "read_failed"})
                continue

            if b"\x00" in memoryview(payload)[:TEXT_SNIFF_BYTES]:
                continue

            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                errors.append({"path": normalized_path, "reason": "utf8_decode_failed"})
                continue

            line_count = len(text.splitlines())
            byte_count = len(payload)
            total_lines += line_count
            total_bytes += byte_count
            try:
                updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
            except OSError:
                updated_at = generated_at
            files.append(
                {
                    "path": normalized_path,
                    "lines": line_count,
                    "bytes": byte_count,
                    "hash": hashlib.sha256(payload).hexdigest(),
                    "updated_at": updated_at,
                    "readable": True,
                }
            )

    files.sort(key=lambda item: str(item["path"]))
    errors.sort(key=lambda item: item["path"])
    current_files = {
        str(item["path"]): item
        for item in files
        if isinstance(item.get("path"), str)
    }
    changes: list[dict[str, int | str]] = []

    for file_path, current in current_files.items():
        previous = previous_files.get(file_path)
        if previous is None:
            change = "added"
        else:
            previous_hash = previous.get("hash")
            current_hash = current.get("hash")
            change = (
                "modified"
                if previous_hash != current_hash
                or previous.get("lines") != current.get("lines")
                or previous.get("bytes") != current.get("bytes")
                else None
            )
        if change is None:
            continue
        lines = current.get("lines")
        bytes_count = current.get("bytes")
        updated_at = current.get("updated_at")
        file_hash = current.get("hash")
        if not isinstance(lines, int) or not isinstance(bytes_count, int):
            continue
        if not isinstance(updated_at, str):
            updated_at = generated_at
        changes.append(
            {
                "path": file_path,
                "change": change,
                "lines": lines,
                "bytes": bytes_count,
                "hash": file_hash if isinstance(file_hash, str) else "",
                "updated_at": updated_at,
                "changed_at": generated_at,
            }
        )

    for file_path, previous in previous_files.items():
        if file_path in current_files:
            continue
        lines = previous.get("lines")
        bytes_count = previous.get("bytes")
        updated_at = previous.get("updated_at")
        file_hash = previous.get("hash")
        if not isinstance(lines, int) or not isinstance(bytes_count, int):
            continue
        if not isinstance(updated_at, str):
            updated_at = generated_at
        changes.append(
            {
                "path": file_path,
                "change": "deleted",
                "lines": lines,
                "bytes": bytes_count,
                "hash": file_hash if isinstance(file_hash, str) else "",
                "updated_at": updated_at,
                "changed_at": generated_at,
            }
        )

    changes.sort(key=lambda item: str(item["path"]))
    index_data = {
        "files": files,
        "errors": errors,
        "total_files": len(files),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "generated_at": generated_at,
        "change_history": previous_history + changes,
    }
    index_path.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
