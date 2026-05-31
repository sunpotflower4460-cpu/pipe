import json
from pathlib import Path


def build_index(workspace_dir: Path) -> None:
    files: list[dict[str, int | str]] = []

    for path in sorted(workspace_dir.rglob("*")):
        if not path.is_file():
            continue

        relative = path.relative_to(workspace_dir).as_posix()
        if relative == "index.json":
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        files.append(
            {
                "path": relative,
                "lines": len(content.splitlines()),
                "bytes": path.stat().st_size,
            }
        )

    index_path = workspace_dir / "index.json"
    index_path.write_text(
        json.dumps({"files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
