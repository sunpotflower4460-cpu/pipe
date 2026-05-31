import unittest
from pathlib import Path


class MvpDocsTestCase(unittest.TestCase):
    def test_mvp_doc_covers_manual_acceptance_flow(self) -> None:
        doc_path = Path(__file__).resolve().parent.parent / "docs" / "mvp-test.md"
        self.assertTrue(doc_path.is_file())

        content = doc_path.read_text(encoding="utf-8")

        required_snippets = [
            "uvicorn app.main:app --reload --port 8000",
            "curl -i http://localhost:8000/health",
            'curl -i -X POST http://localhost:8000/ingest \\',
            "/t/${TOKEN}/index",
            "/t/${TOKEN}/file?path=README.md&from=1&to=200",
            "/t/${TOKEN}/file?path=large.cpp&from=1&to=1200",
            "ngrok http 8000",
            "ngrok-skip-browser-warning",
            '/revoke?token=${TOKEN}',
            "403",
        ]

        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, content)


if __name__ == "__main__":
    unittest.main()
