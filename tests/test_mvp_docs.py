import unittest
from pathlib import Path


class MvpDocsTestCase(unittest.TestCase):
    def test_mvp_doc_covers_manual_acceptance_flow(self) -> None:
        doc_path = Path(__file__).resolve().parent.parent / "docs" / "mvp-test.md"
        self.assertTrue(doc_path.is_file())

        content = doc_path.read_text(encoding="utf-8")

        required_snippets = [
            "初期運用は「ローカル起動 + 一時トンネル（ngrok等）」に固定する",
            "Post-MVP",
            "uvicorn app.main:app --reload --port 8000",
            "curl -i http://localhost:8000/health",
            'curl -i -X POST http://localhost:8000/ingest \\',
            "/t/${TOKEN}/index",
            "/t/${TOKEN}/file?path=README.md&from=1&to=200",
            "/t/${TOKEN}/file?path=large.cpp&from=1&to=1200",
            "ngrok http 8000",
            "ngrok-skip-browser-warning",
            '/revoke?token=${TOKEN}',
            "/t/not-a-real-token/index",
            "403",
        ]

        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, content)

    def test_how_to_use_doc_and_readme_link(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        how_to_path = repo_root / "docs" / "how-to-use.md"
        self.assertTrue(how_to_path.is_file())

        how_to_content = how_to_path.read_text(encoding="utf-8")
        required_snippets = [
            "Code Relayは、ClaudeやChatGPTにコードを読んでもらうための中継アプリです。",
            "まず管理画面のURLを開きます。",
            "まずは `index URL` を渡します。",
            "/file?path=...&from=...&to=...",
            "{INDEX_URL}",
            "停止 / revoke",
        ]
        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, how_to_content)

        readme_content = (repo_root / "README.md").read_text(encoding="utf-8")
        self.assertIn("[docs/how-to-use.md](docs/how-to-use.md)", readme_content)


if __name__ == "__main__":
    unittest.main()
