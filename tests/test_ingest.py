import io
import asyncio
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import urlunsplit

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

    def test_ingest_repo_success_with_token_header_and_git_excluded_from_index(self) -> None:
        access_token = "readonly-token"

        def fake_clone(command: list[str], check: bool, capture_output: bool, env: dict[str, str]):
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertEqual(command[:5], ["git", "clone", "--depth", "1", "--"])
            self.assertNotIn(access_token, " ".join(command))
            self.assertEqual(command[5], "https://github.com/example/example.git")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: " + "Bearer " + access_token)

            destination = Path(command[6])
            (destination / ".git").mkdir(parents=True, exist_ok=True)
            (destination / ".git" / "config").write_text("secret", encoding="utf-8")
            (destination / "src").mkdir(parents=True, exist_ok=True)
            (destination / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (destination / "README.md").write_text("# sample\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with mock.patch("app.ingest.subprocess.run", side_effect=fake_clone):
            response = asyncio.run(
                ingest_module.ingest_repo(
                    repository_url="https://github.com/example/example.git",
                    access_token=access_token,
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        token = response.body.decode("utf-8").splitlines()[0].split("=", 1)[1]
        workspace = ingest_module.WORKSPACE_ROOT / token
        index_path = workspace / "index.json"
        self.assertTrue(index_path.exists())
        index_text = index_path.read_text(encoding="utf-8")
        index = json.loads(index_text)
        indexed_paths = [item["path"] for item in index["files"]]
        self.assertEqual(indexed_paths, ["README.md", "src/main.py"])
        self.assertNotIn(".git/config", indexed_paths)
        self.assertNotIn(access_token, index_text)

    def test_ingest_repo_rejects_unsafe_repository_url(self) -> None:
        file_scheme = asyncio.run(
            ingest_module.ingest_repo(repository_url="file:///tmp/repo.git", access_token=None)
        )
        self.assertEqual(file_scheme.status_code, 400)
        self.assertEqual(file_scheme.body.decode("utf-8"), "ERROR: invalid repository url")

        credential_url = urlunsplit(("https", "user:pass@github.com", "/example/example.git", "", ""))
        embedded_credentials = asyncio.run(
            ingest_module.ingest_repo(
                repository_url=credential_url,
                access_token=None,
            )
        )
        self.assertEqual(embedded_credentials.status_code, 400)
        self.assertEqual(embedded_credentials.body.decode("utf-8"), "ERROR: invalid repository url")

    def test_ingest_repo_blank_access_token_omits_auth_header(self) -> None:
        def fake_clone(command: list[str], check: bool, capture_output: bool, env: dict[str, str]):
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertEqual(command[:5], ["git", "clone", "--depth", "1", "--"])
            self.assertNotIn("GIT_CONFIG_COUNT", env)
            self.assertNotIn("GIT_CONFIG_KEY_0", env)
            self.assertNotIn("GIT_CONFIG_VALUE_0", env)

            destination = Path(command[6])
            (destination / "README.md").write_text("# sample\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with mock.patch.dict("app.ingest.os.environ", {}, clear=True):
            with mock.patch("app.ingest.subprocess.run", side_effect=fake_clone):
                response = asyncio.run(
                    ingest_module.ingest_repo(
                        repository_url="https://github.com/example/example.git",
                        access_token="   ",
                    )
                )
        self.assertEqual(response.status_code, 200)

    def test_ingest_repo_clone_failure_returns_generic_error_and_cleans_workspace(self) -> None:
        with mock.patch(
            "app.ingest.subprocess.run",
            return_value=subprocess.CompletedProcess(["git"], 1, stdout=b"", stderr=b"fatal: auth failed"),
        ):
            response = asyncio.run(
                ingest_module.ingest_repo(
                    repository_url="https://github.com/example/private.git",
                    access_token="readonly-token",
                )
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(response.body.decode("utf-8"), "ERROR: failed to clone repository")
        self.assertFalse(any(ingest_module.WORKSPACE_ROOT.glob("*")))


if __name__ == "__main__":
    unittest.main()
