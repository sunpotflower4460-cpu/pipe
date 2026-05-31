import json
import os
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
    if normalized.is_absolute() or any(part in {"", ".", ".."} for part in normalized.parts):
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

            if b"\x00" in payload[:TEXT_SNIFF_BYTES]:
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
            files.append(
                {
                    "path": normalized_path,
                    "lines": line_count,
                    "bytes": byte_count,
                    "readable": True,
                }
            )

    files.sort(key=lambda item: str(item["path"]))
    errors.sort(key=lambda item: item["path"])
    index_data = {
        "files": files,
        "errors": errors,
        "total_files": len(files),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
    }
    index_path = workspace_dir / "index.json"
    index_path.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
