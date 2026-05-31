import io
import json
import asyncio
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.datastructures import Headers, UploadFile

import app.ingest as ingest_module
import app.serve as serve_module
import app.tokens as tokens_module


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buffer.getvalue()


class TokenRoutesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.workspace_root = self.base_dir / "workspace"
        self.tokens_file = self.base_dir / "tokens.json"

        self.original_ingest_workspace_root = ingest_module.WORKSPACE_ROOT
        self.original_tokens_base_dir = tokens_module.BASE_DIR
        self.original_tokens_workspace_root = tokens_module.WORKSPACE_ROOT
        self.original_tokens_file = tokens_module.TOKENS_FILE

        ingest_module.WORKSPACE_ROOT = self.workspace_root
        tokens_module.BASE_DIR = self.base_dir
        tokens_module.WORKSPACE_ROOT = self.workspace_root
        tokens_module.TOKENS_FILE = self.tokens_file

    def tearDown(self) -> None:
        ingest_module.WORKSPACE_ROOT = self.original_ingest_workspace_root
        tokens_module.BASE_DIR = self.original_tokens_base_dir
        tokens_module.WORKSPACE_ROOT = self.original_tokens_workspace_root
        tokens_module.TOKENS_FILE = self.original_tokens_file
        self.tmp_dir.cleanup()

    def _ingest_sample(self) -> str:
        payload = make_zip({"src/main.py": b"print('ok')\n", "README.md": b"# sample\n"})
        upload_file = UploadFile(
            file=io.BytesIO(payload),
            filename="sample.zip",
            headers=Headers({"content-type": "application/zip"}),
        )
        response = asyncio.run(ingest_module.ingest(upload_file))
        self.assertEqual(response.status_code, 200)
        token_line = response.body.decode("utf-8").splitlines()[0]
        return token_line.split("=", 1)[1]

    def test_token_access_and_content_type(self) -> None:
        token = self._ingest_sample()

        index_response = asyncio.run(serve_module.get_index(token, from_index=1, to_index=500))
        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(index_response.headers["content-type"], "text/plain; charset=utf-8")
        body = index_response.body.decode("utf-8")
        self.assertIn("# Code Relay Index", body)
        self.assertIn("Total files:", body)
        self.assertIn("Total lines:", body)
        self.assertIn("Total size:", body)
        self.assertIn("README.md | 1 lines | 1 KB", body)
        self.assertIn("src/main.py | 1 lines | 1 KB", body)

        file_response = asyncio.run(serve_module.get_file(token, path="src/main.py", from_line=1, to_line=600))
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("1| print('ok')", file_response.body.decode("utf-8"))

    def test_index_pagination(self) -> None:
        token = self._ingest_sample()

        response = asyncio.run(serve_module.get_index(token, from_index=1, to_index=1))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("README.md", body)
        self.assertNotIn("src/main.py", body)
        self.assertIn("--- 続きは from=2&to=", body)

    def test_index_not_found_returns_404(self) -> None:
        token = self._ingest_sample()
        workspace = self.workspace_root / token
        (workspace / "index.json").unlink()

        response = asyncio.run(serve_module.get_index(token))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("ERROR: index not found", response.body.decode("utf-8"))

    def test_invalid_token_returns_plain_text_403(self) -> None:
        response = asyncio.run(serve_module.get_index("not-a-real-token"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("ERROR: invalid or expired token", response.body.decode("utf-8"))

    def test_revoke_makes_workspace_unreachable(self) -> None:
        token = self._ingest_sample()
        workspace = self.workspace_root / token
        self.assertTrue(workspace.exists())

        revoke_response = asyncio.run(serve_module.revoke(token))
        self.assertEqual(revoke_response.status_code, 200)
        self.assertEqual(revoke_response.body.decode("utf-8"), "revoked")
        self.assertFalse(workspace.exists())

        index_response = asyncio.run(serve_module.get_index(token))
        self.assertEqual(index_response.status_code, 403)

    def test_expired_token_returns_403_and_removes_workspace(self) -> None:
        token = self._ingest_sample()
        workspace = self.workspace_root / token

        data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
        data[token]["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        data[token]["revoked"] = False
        self.tokens_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        response = asyncio.run(serve_module.get_index(token))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
