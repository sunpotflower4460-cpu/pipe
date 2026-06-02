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
import app.index as index_module
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

    def test_file_accepts_equal_from_and_to(self) -> None:
        source = "".join(f"line {number}\n" for number in range(1, 11)).encode("utf-8")
        token = self._ingest_sample({"src/range.py": source})

        response = asyncio.run(serve_module.get_file(token, path="src/range.py", from_line="5", to_line="5"))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("# src/range.py lines 5-5", body)
        self.assertIn("5| line 5", body)
        self.assertNotIn("6| line 6", body)

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

        valid_numeric_strings = asyncio.run(serve_module.get_file(token, path="README.md", from_line="1", to_line="10"))
        self.assertEqual(valid_numeric_strings.status_code, 200)
        self.assertEqual(valid_numeric_strings.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("1| # sample", valid_numeric_strings.body.decode("utf-8"))
        self.assertIn("# README.md lines 1-1", valid_numeric_strings.body.decode("utf-8"))

    def test_symbol_extracts_function_block(self) -> None:
        source = (
            "#include <cstdint>\n"
            "void helper();\n"
            "\n"
            "void PluginProcessor::processBlock(int value)\n"
            "{\n"
            "  if (value) {\n"
            "    value -= 1;\n"
            "  }\n"
            "}\n"
            "void after() {}\n"
        ).encode("utf-8")
        token = self._ingest_sample({"Source/PluginProcessor.cpp": source})

        response = asyncio.run(
            serve_module.get_symbol(token, path="Source/PluginProcessor.cpp", name="processBlock")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        body = response.body.decode("utf-8")
        self.assertIn("# Source/PluginProcessor.cpp symbol: processBlock lines 4-9", body)
        self.assertIn("4| void PluginProcessor::processBlock(int value)", body)
        self.assertIn("9| }", body)
        self.assertNotIn("10| void after() {}", body)

    def test_symbol_plain_text_errors_and_token_validation(self) -> None:
        token = self._ingest_sample({"src/sample.cpp": b"void run() {}\n"})

        invalid_token = asyncio.run(serve_module.get_symbol("not-a-real-token", path="src/sample.cpp", name="run"))
        self.assertEqual(invalid_token.status_code, 403)
        self.assertEqual(invalid_token.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(invalid_token.body.decode("utf-8"), "ERROR: invalid or expired token")

        missing_path = asyncio.run(serve_module.get_symbol(token, path=None, name="run"))
        self.assertEqual(missing_path.status_code, 400)
        self.assertEqual(missing_path.body.decode("utf-8"), "ERROR: missing path")

        missing_name = asyncio.run(serve_module.get_symbol(token, path="src/sample.cpp", name=None))
        self.assertEqual(missing_name.status_code, 400)
        self.assertEqual(missing_name.body.decode("utf-8"), "ERROR: missing name")

        unsafe_path = asyncio.run(serve_module.get_symbol(token, path="../tokens.json", name="run"))
        self.assertEqual(unsafe_path.status_code, 400)
        self.assertEqual(unsafe_path.body.decode("utf-8"), "ERROR: unsafe path")

        not_found = asyncio.run(serve_module.get_symbol(token, path="src/sample.cpp", name="missingSymbol"))
        self.assertEqual(not_found.status_code, 404)
        self.assertEqual(not_found.body.decode("utf-8"), "ERROR: symbol not found: missingSymbol")

        workspace = self.workspace_root / token
        (workspace / "secret.cpp").write_text("void hidden() {}", encoding="utf-8")
        not_indexed = asyncio.run(serve_module.get_symbol(token, path="secret.cpp", name="hidden"))
        self.assertEqual(not_indexed.status_code, 404)
        self.assertEqual(not_indexed.body.decode("utf-8"), "ERROR: file is not indexed or not readable")

    def test_symbol_extracts_class_block(self) -> None:
        source = (
            "class Processor {\n"
            "public:\n"
            "  void run() {\n"
            "    int value = 1;\n"
            "  }\n"
            "};\n"
            "\n"
            "void other() {}\n"
        ).encode("utf-8")
        token = self._ingest_sample({"src/plugin.hpp": source})

        response = asyncio.run(serve_module.get_symbol(token, path="src/plugin.hpp", name="Processor"))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("# src/plugin.hpp symbol: Processor lines 1-6", body)
        self.assertIn("1| class Processor {", body)
        self.assertIn("6| };", body)
        self.assertNotIn("8| void other() {}", body)

    def test_index_pagination(self) -> None:
        token = self._ingest_sample()

        response = asyncio.run(serve_module.get_index(token, from_index=1, to_index=1))
        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn("README.md", body)
        self.assertNotIn("src/main.py", body)
        self.assertIn("--- 続きは from=2&to=", body)

    def test_share_index_and_file_scope(self) -> None:
        token = self._ingest_sample(
            {
                "README.md": b"# sample\n",
                "Source/PluginProcessor.cpp": b"void process() {}\n",
                "build/cache.txt": b"temp\n",
            }
        )
        tokens_module.update_token_metadata(
            token,
            folders=[
                {
                    "id": "source",
                    "name": "Source",
                    "paths": ["Source/PluginProcessor.cpp"],
                    "visible": True,
                }
            ],
            shares=[
                {
                    "id": "review-main",
                    "name": "AIに見せるセット",
                    "paths": ["README.md", "Source/PluginProcessor.cpp"],
                    "excluded_paths": ["build/", "hidden files"],
                }
            ],
        )

        response = asyncio.run(serve_module.get_share_index(token, "review-main", from_index=1, to_index=50))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        body = response.body.decode("utf-8")
        self.assertIn("# Code Relay Share Index: AIに見せるセット", body)
        self.assertIn("README.md | 1 lines | 1 KB", body)
        self.assertIn("Source/PluginProcessor.cpp | 1 lines | 1 KB", body)
        self.assertNotIn("build/cache.txt", body)

        file_response = asyncio.run(
            serve_module.get_share_file(token, "review-main", path="Source/PluginProcessor.cpp", from_line=1, to_line=10)
        )
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(file_response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("1| void process() {}", file_response.body.decode("utf-8"))

        out_of_scope = asyncio.run(
            serve_module.get_share_file(token, "review-main", path="build/cache.txt", from_line=1, to_line=10)
        )
        self.assertEqual(out_of_scope.status_code, 404)
        self.assertEqual(out_of_scope.body.decode("utf-8"), "ERROR: file is outside share scope")

    def test_share_respects_hidden_files_and_folder_share_url(self) -> None:
        token = self._ingest_sample(
            {
                "README.md": b"# sample\n",
                "Source/main.cpp": b"int main(){}\n",
            }
        )
        tokens_module.update_token_metadata(
            token,
            folders=[{"id": "source", "name": "Source", "paths": ["Source/main.cpp"], "visible": True}],
            shares=[
                {
                    "id": "ai-set",
                    "name": "AIに見せるセット",
                    "paths": ["README.md", "Source/main.cpp"],
                    "excluded_paths": ["hidden files"],
                }
            ],
        )

        workspace = self.workspace_root / token
        index = json.loads((workspace / "index.json").read_text(encoding="utf-8"))
        for item in index["files"]:
            if item["path"] == "README.md":
                item["readable"] = False
        (workspace / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

        share_response = asyncio.run(serve_module.get_share_index(token, "ai-set", from_index=1, to_index=50))
        self.assertEqual(share_response.status_code, 200)
        body = share_response.body.decode("utf-8")
        self.assertIn("Source/main.cpp", body)
        self.assertNotIn("README.md", body)

        folder_response = asyncio.run(serve_module.get_share_index(token, "folder-source", from_index=1, to_index=50))
        self.assertEqual(folder_response.status_code, 200)
        self.assertIn("Source/main.cpp", folder_response.body.decode("utf-8"))

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

    def test_changes_reports_added_modified_deleted(self) -> None:
        token = self._ingest_sample(
            {
                "src/main.py": b"print('ok')\n",
                "README.md": b"# sample\n",
                "docs/keep.txt": b"keep\n",
            }
        )
        workspace = self.workspace_root / token
        first_index = json.loads((workspace / "index.json").read_text(encoding="utf-8"))
        since = first_index["generated_at"]

        (workspace / "src/main.py").write_text("print('changed')\n", encoding="utf-8")
        (workspace / "README.md").unlink()
        (workspace / "docs/new.txt").write_text("new\n", encoding="utf-8")
        (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
        index_module.build_index(workspace)

        response = asyncio.run(serve_module.get_changes(token, since=since))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        body = response.body.decode("utf-8")
        self.assertIn("# Changed files since", body)
        self.assertIn("src/main.py | modified | 1 lines", body)
        self.assertIn("README.md | deleted | 1 lines", body)
        self.assertIn("docs/new.txt | added | 1 lines", body)
        self.assertNotIn(".env", body)
        self.assertIn("Total changed files: 3", body)

    def test_changes_no_changes_and_invalid_token(self) -> None:
        token = self._ingest_sample()
        since = datetime.now(timezone.utc).isoformat()

        no_changes = asyncio.run(serve_module.get_changes(token, since=since))
        self.assertEqual(no_changes.status_code, 200)
        self.assertEqual(no_changes.headers["content-type"], "text/plain; charset=utf-8")
        self.assertIn("No changes.", no_changes.body.decode("utf-8"))

        invalid_token = asyncio.run(serve_module.get_changes("not-a-real-token", since=since))
        self.assertEqual(invalid_token.status_code, 403)
        self.assertEqual(invalid_token.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(invalid_token.body.decode("utf-8"), "ERROR: invalid or expired token")

        invalid_since = asyncio.run(serve_module.get_changes(token, since="not-iso8601"))
        self.assertEqual(invalid_since.status_code, 400)
        self.assertEqual(invalid_since.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(invalid_since.body.decode("utf-8"), "ERROR: invalid since")

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
