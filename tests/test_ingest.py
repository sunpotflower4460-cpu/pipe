import io
import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from starlette.datastructures import Headers, UploadFile

import app.ingest as ingest_module
import app.tokens as tokens_module


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buffer.getvalue()


class IngestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        tmp_base = Path(self.tmp_dir.name)
        self.original_workspace_root = ingest_module.WORKSPACE_ROOT
        self.original_tokens_base_dir = tokens_module.BASE_DIR
        self.original_tokens_workspace_root = tokens_module.WORKSPACE_ROOT
        self.original_tokens_file = tokens_module.TOKENS_FILE

        ingest_module.WORKSPACE_ROOT = tmp_base / "workspace"
        tokens_module.BASE_DIR = tmp_base
        tokens_module.WORKSPACE_ROOT = ingest_module.WORKSPACE_ROOT
        tokens_module.TOKENS_FILE = tmp_base / "tokens.json"

    def tearDown(self) -> None:
        ingest_module.WORKSPACE_ROOT = self.original_workspace_root
        tokens_module.BASE_DIR = self.original_tokens_base_dir
        tokens_module.WORKSPACE_ROOT = self.original_tokens_workspace_root
        tokens_module.TOKENS_FILE = self.original_tokens_file
        self.tmp_dir.cleanup()

    def call_ingest(self, filename: str, payload: bytes, content_type: str) -> tuple[int, str, str]:
        upload_file = UploadFile(
            file=io.BytesIO(payload),
            filename=filename,
            headers=Headers({"content-type": content_type}),
        )
        response = asyncio.run(ingest_module.ingest(upload_file))
        body = response.body.decode("utf-8")
        return response.status_code, response.headers["content-type"], body

    def test_ingest_success(self) -> None:
        payload = make_zip(
            {
                "src/main.py": b"print('ok')\n",
                "README.md": b"# sample\n",
            }
        )
        status_code, content_type, body = self.call_ingest("sample.zip", payload, "application/zip")
        self.assertEqual(status_code, 200)
        self.assertEqual(content_type, "text/plain; charset=utf-8")
        lines = body.strip().splitlines()
        self.assertEqual(len(lines), 2)
        token = lines[0].split("=", 1)[1]
        self.assertEqual(lines[1], f"INDEX=/t/{token}/index")

        workspace = ingest_module.WORKSPACE_ROOT / token
        self.assertTrue((workspace / "src/main.py").exists())
        self.assertTrue((workspace / "index.json").exists())

    def test_reject_non_zip_file(self) -> None:
        status_code, _, body = self.call_ingest("README.md", b"hello", "text/plain")
        self.assertEqual(status_code, 400)
        self.assertIn("ERROR: invalid zip file type", body)

    def test_reject_invalid_zip(self) -> None:
        status_code, _, body = self.call_ingest("broken.zip", b"not-a-zip", "application/zip")
        self.assertEqual(status_code, 400)
        self.assertIn("ERROR: invalid zip file", body)

    def test_reject_path_traversal(self) -> None:
        payload = make_zip({"../secret.txt": b"ng"})
        status_code, _, body = self.call_ingest("sample.zip", payload, "application/zip")
        self.assertEqual(status_code, 400)
        self.assertIn("ERROR: unsafe zip entry path:", body)
        self.assertFalse(any(ingest_module.WORKSPACE_ROOT.glob("*")))
        self.assertFalse((ingest_module.WORKSPACE_ROOT.parent / "secret.txt").exists())

    def test_build_index_excludes_binary_secret_and_records_utf8_errors(self) -> None:
        payload = make_zip(
            {
                "src/main.py": b"print('ok')\n",
                "README.md": b"# sample\n",
                ".env": b"SECRET=1\n",
                "font.ttf": b"font-bytes",
                "audio.wav": b"audio-bytes",
                "image.png": b"image-bytes",
                "node_modules/foo/index.js": b"console.log('x')\n",
                "legacy/unknown.txt": b"\x80\x81\x82",
            }
        )
        status_code, _, body = self.call_ingest("sample.zip", payload, "application/zip")
        self.assertEqual(status_code, 200)
        token = body.strip().splitlines()[0].split("=", 1)[1]

        index = json.loads((ingest_module.WORKSPACE_ROOT / token / "index.json").read_text(encoding="utf-8"))
        files = index["files"]
        self.assertEqual([item["path"] for item in files], ["README.md", "src/main.py"])
        self.assertEqual(index["total_files"], 2)
        self.assertEqual(index["total_lines"], 2)
        self.assertEqual(index["total_bytes"], len(b"# sample\n") + len(b"print('ok')\n"))
        self.assertTrue(all(item["readable"] is True for item in files))
        self.assertEqual(
            index["errors"],
            [{"path": "legacy/unknown.txt", "reason": "utf8_decode_failed"}],
        )

        indexed_paths = {item["path"] for item in files}
        self.assertNotIn(".env", indexed_paths)
        self.assertNotIn("font.ttf", indexed_paths)
        self.assertNotIn("audio.wav", indexed_paths)
        self.assertNotIn("image.png", indexed_paths)
        self.assertNotIn("node_modules/foo/index.js", indexed_paths)
        for path in indexed_paths.union(item["path"] for item in index["errors"]):
            self.assertFalse(path.startswith("/"))
            self.assertNotIn("..", Path(path).parts)


if __name__ == "__main__":
    unittest.main()
