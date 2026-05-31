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

    def _ingest_sample(self, entries: dict[str, bytes] | None = None) -> str:
        payload = make_zip(entries or {"src/main.py": b"print('ok')\n", "README.md": b"# sample\n"})
        upload_file = UploadFile(
            file=io.BytesIO(payload),
            filename="sample.zip",
            headers=Headers({"content-type": "application/zip"}),
        )
        response = asyncio.run(ingest_module.ingest(upload_file))
        self.assertEqual(response.status_code, 200)
        token_line = response.body.decode("utf-8").splitlines()[0]
        return token_line.split("=", 1)[1]

    def _as_plain_lines(self, response_body: bytes) -> list[str]:
        return response_body.decode("utf-8").splitlines()

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
        file_body = file_response.body.decode("utf-8")
        self.assertIn("# src/main.py lines 1-1", file_body)
        self.assertIn("1| print('ok')", file_body)

    def test_file_defaults_and_continuation_hint(self) -> None:
        line_count = 700
        long_file = "".join(f"line {number}\n" for number in range(1, line_count + 1)).encode("utf-8")
        token = self._ingest_sample({"src/large.py": long_file})

        response = asyncio.run(serve_module.get_file(token, path="src/large.py", from_line=None, to_line=None))
        self.assertEqual(response.status_code, 200)
        lines = self._as_plain_lines(response.body)
        self.assertEqual(lines[0], "# src/large.py lines 1-600")
        self.assertEqual(lines[2], "  1| line 1")
        self.assertEqual(lines[601], "600| line 600")
        self.assertEqual(lines[602], "--- 続きは from=601&to=1200 で取得 ---")

    def test_file_hard_cap_is_800_lines(self) -> None:
        line_count = 2000
        long_file = "".join(f"line {number}\n" for number in range(1, line_count + 1)).encode("utf-8")
        token = self._ingest_sample({"src/large.py": long_file})

        response = asyncio.run(serve_module.get_file(token, path="src/large.py", from_line=801, to_line=5000))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("# src/large.py lines 801-1600", body)
        self.assertIn(" 801| line 801", body)
        self.assertIn("1600| line 1600", body)
        self.assertIn("--- 続きは from=1601&to=2200 で取得 ---", body)

    def test_file_requires_indexed_path(self) -> None:
        token = self._ingest_sample()
        workspace = self.workspace_root / token
        (workspace / "secret.txt").write_text("secret", encoding="utf-8")

        response = asyncio.run(serve_module.get_file(token, path="secret.txt", from_line=1, to_line=10))
        self.assertEqual(response.status_code, 404)
        self.assertIn("ERROR: file is not indexed or not readable", response.body.decode("utf-8"))

    def test_file_rejects_unsafe_path(self) -> None:
        token = self._ingest_sample()

        response = asyncio.run(serve_module.get_file(token, path="../tokens.json", from_line=1, to_line=20))
        self.assertEqual(response.status_code, 400)
        self.assertIn("ERROR: unsafe path", response.body.decode("utf-8"))

    def test_file_plain_text_errors_for_missing_path_and_invalid_ranges(self) -> None:
        token = self._ingest_sample()

        missing_path = asyncio.run(serve_module.get_file(token, path=None))
        self.assertEqual(missing_path.status_code, 400)
        self.assertEqual(missing_path.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(missing_path.body.decode("utf-8"), "ERROR: missing path")

        invalid_range = asyncio.run(serve_module.get_file(token, path="README.md", from_line="200", to_line="100"))
        self.assertEqual(invalid_range.status_code, 400)
        self.assertEqual(invalid_range.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(invalid_range.body.decode("utf-8"), "ERROR: invalid line range")

        invalid_type = asyncio.run(serve_module.get_file(token, path="README.md", from_line="abc", to_line="def"))
        self.assertEqual(invalid_type.status_code, 400)
        self.assertEqual(invalid_type.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(invalid_type.body.decode("utf-8"), "ERROR: invalid line range")

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
