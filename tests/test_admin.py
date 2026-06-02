import asyncio
import io
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

import app.admin as admin_module
import app.ingest as ingest_module
import app.main as main_module
import app.tokens as tokens_module


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    return buffer.getvalue()


class AdminRoutesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.workspace_root = self.base_dir / "workspace"
        self.tokens_file = self.base_dir / "tokens.json"

        self.original_ingest_workspace_root = ingest_module.WORKSPACE_ROOT
        self.original_tokens_base_dir = tokens_module.BASE_DIR
        self.original_tokens_workspace_root = tokens_module.WORKSPACE_ROOT
        self.original_tokens_file = tokens_module.TOKENS_FILE
        self.original_admin_base_dir = admin_module.BASE_DIR
        self.original_admin_base_public_url = admin_module.BASE_PUBLIC_URL
        self.original_admin_workspace_root = admin_module.WORKSPACE_ROOT

        ingest_module.WORKSPACE_ROOT = self.workspace_root
        tokens_module.BASE_DIR = self.base_dir
        tokens_module.WORKSPACE_ROOT = self.workspace_root
        tokens_module.TOKENS_FILE = self.tokens_file
        admin_module.BASE_DIR = self.base_dir
        admin_module.BASE_PUBLIC_URL = ""
        admin_module.WORKSPACE_ROOT = self.workspace_root

    def tearDown(self) -> None:
        ingest_module.WORKSPACE_ROOT = self.original_ingest_workspace_root
        tokens_module.BASE_DIR = self.original_tokens_base_dir
        tokens_module.WORKSPACE_ROOT = self.original_tokens_workspace_root
        tokens_module.TOKENS_FILE = self.original_tokens_file
        admin_module.BASE_DIR = self.original_admin_base_dir
        admin_module.BASE_PUBLIC_URL = self.original_admin_base_public_url
        admin_module.WORKSPACE_ROOT = self.original_admin_workspace_root
        self.tmp_dir.cleanup()

    def _request(self, path: str = "/admin", method: str = "GET", query: str = "") -> Request:
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": query.encode("utf-8"),
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": main_module.app,
        }
        return Request(scope)

    def test_admin_zip_create_and_listing(self) -> None:
        response = asyncio.run(admin_module.admin_page(self._request()))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        response_text = response.body.decode("utf-8")
        self.assertIn("Code Memo", response_text)
        self.assertIn("ZIPを追加すると、コードをアプリ内で読めます。", response_text)
        self.assertIn("ファイルを追加", response_text)
        self.assertIn("シェア", response_text)

        payload = make_zip({"README.md": b"# sample\n", "src/main.py": b"print('ok')\n"})
        upload_file = UploadFile(
            file=io.BytesIO(payload),
            filename="sample.zip",
            headers=Headers({"content-type": "application/zip"}),
        )
        created = asyncio.run(
            admin_module.create_pipe(
                request=self._request(method="POST"),
                name="My Zip Pipe",
                source_type="zip",
                repository_url=None,
                access_token=None,
                file=upload_file,
            )
        )
        self.assertEqual(created.status_code, 303)

        listed = asyncio.run(admin_module.admin_page(self._request()))
        listed_text = listed.body.decode("utf-8")
        self.assertIn("My Zip Pipe", listed_text)
        self.assertIn("ファイル一覧", listed_text)
        self.assertIn("隠したファイル", listed_text)
        self.assertIn("index URLをコピー", listed_text)
        self.assertIn("テンプレートをコピー", listed_text)
        self.assertIn("file URLをコピー", listed_text)
        self.assertIn("index URL", listed_text)
        self.assertIn("file?path=README.md&amp;from=1&amp;to=600", listed_text)
        self.assertIn("symbol URL \u4f8b", listed_text)
        self.assertIn("changes URL \u4f8b", listed_text)
        self.assertIn("AIに貼る文章", listed_text)

        data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        token, record = next(iter(data.items()))
        self.assertEqual(record["name"], "My Zip Pipe")
        self.assertEqual(record["source_type"], "zip")
        self.assertIn(f"/t/{token}/index", listed_text)
        self.assertEqual(record["hidden_paths"], [])

    def test_admin_repo_create_does_not_store_access_token_and_supports_revoke_delete(self) -> None:
        access_token = "read-only-secret-token"

        def fake_clone(command: list[str], check: bool, capture_output: bool, env: dict[str, str]):
            self.assertFalse(check)
            self.assertTrue(capture_output)
            self.assertNotIn(access_token, " ".join(command))
            expected_auth_header = f"Authorization: {'B' + 'earer '}{access_token}"
            self.assertEqual(env["GIT_CONFIG_VALUE_0"], expected_auth_header)
            destination = Path(command[6])
            (destination / "README.md").write_text("# sample\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

        with mock.patch("app.ingest.subprocess.run", side_effect=fake_clone):
            created = asyncio.run(
                admin_module.create_pipe(
                    request=self._request(method="POST"),
                    name="My Repo Pipe",
                    source_type="repo",
                    repository_url="https://github.com/example/repo.git",
                    access_token=access_token,
                    file=None,
                )
            )

        self.assertEqual(created.status_code, 303)
        listed = asyncio.run(admin_module.admin_page(self._request()))
        self.assertIn("My Repo Pipe", listed.body.decode("utf-8"))

        tokens_text = self.tokens_file.read_text(encoding="utf-8")
        self.assertNotIn(access_token, tokens_text)
        data = json.loads(tokens_text)
        token, record = next(iter(data.items()))
        self.assertEqual(record["name"], "My Repo Pipe")
        self.assertEqual(record["source_type"], "repo")
        self.assertEqual(record["repository_url"], "https://github.com/example/repo.git")

        revoked = asyncio.run(admin_module.revoke_pipe(token))
        self.assertEqual(revoked.status_code, 303)
        revoked_data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
        self.assertTrue(revoked_data[token]["revoked"])

        deleted = asyncio.run(admin_module.delete_pipe(token))
        self.assertEqual(deleted.status_code, 303)
        deleted_data = json.loads(self.tokens_file.read_text(encoding="utf-8"))
        self.assertNotIn(token, deleted_data)

    def test_admin_can_view_hide_unhide_and_delete_file(self) -> None:
        payload = make_zip({"README.md": b"# sample\nline2\n", "src/main.py": b"print('ok')\n"})
        upload_file = UploadFile(
            file=io.BytesIO(payload),
            filename="sample.zip",
            headers=Headers({"content-type": "application/zip"}),
        )
        created = asyncio.run(
            admin_module.create_pipe(
                request=self._request(method="POST"),
                name="Memo",
                source_type="zip",
                repository_url=None,
                access_token=None,
                file=upload_file,
            )
        )
        self.assertEqual(created.status_code, 303)

        token = created.headers["location"].split("token=", 1)[1]
        viewed = asyncio.run(admin_module.admin_page(self._request(query=f"token={token}&path=README.md")))
        viewed_text = viewed.body.decode("utf-8")
        self.assertIn("README.md", viewed_text)
        self.assertIn("1| # sample", viewed_text)
        self.assertIn("2| line2", viewed_text)
        self.assertIn(f"/t/{token}/file?path=README.md&amp;from=1&amp;to=600", viewed_text)

        hidden = asyncio.run(
            admin_module.hide_file(
                request=self._request(method="POST"),
                token=token,
                path="README.md",
            )
        )
        self.assertEqual(hidden.status_code, 303)
        hidden_page = asyncio.run(admin_module.admin_page(self._request(query=f"token={token}")))
        hidden_text = hidden_page.body.decode("utf-8")
        self.assertNotIn("token=" + token + "&amp;path=README.md", hidden_text)
        self.assertIn("README.md", hidden_text)

        index_path = self.workspace_root / token / "index.json"
        index_data = json.loads(index_path.read_text(encoding="utf-8"))
        readme_entry = next(item for item in index_data["files"] if item["path"] == "README.md")
        self.assertFalse(readme_entry["readable"])

        unhidden = asyncio.run(
            admin_module.unhide_file(
                request=self._request(method="POST"),
                token=token,
                path="README.md",
            )
        )
        self.assertEqual(unhidden.status_code, 303)
        unhidden_page = asyncio.run(admin_module.admin_page(self._request(query=f"token={token}")))
        self.assertIn("token=" + token + "&amp;path=README.md", unhidden_page.body.decode("utf-8"))

        deleted = asyncio.run(
            admin_module.delete_file(
                request=self._request(method="POST"),
                token=token,
                path="README.md",
            )
        )
        self.assertEqual(deleted.status_code, 303)
        self.assertFalse((self.workspace_root / token / "README.md").exists())
        deleted_index_data = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertFalse(any(item["path"] == "README.md" for item in deleted_index_data["files"]))


if __name__ == "__main__":
    unittest.main()
