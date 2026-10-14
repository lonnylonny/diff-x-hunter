import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from diff_x_hunter.web import app


class WatchConfigWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "repository.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE watch_configs "
                "(repository TEXT NOT NULL, focus TEXT NOT NULL)"
            )
        patcher = patch("diff_x_hunter.storage.DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_save_redirect_and_reload(self) -> None:
        self.assertIn("还没有关注配置", self.client.get("/").text)
        response = self.client.post(
            "/watch-configs",
            data={"repository": " astral-sh/uv ", "focus": "依赖解析变化"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        for _ in range(2):
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.text.count("<td>astral-sh/uv</td>"), 1)
            self.assertIn("<td>依赖解析变化</td>", page.text)

    def test_invalid_repository_does_not_save_and_preserves_input(self) -> None:
        for value in ["", "invalid", "owner/", "owner/repo/extra"]:
            with self.subTest(repository=value):
                response = self.client.post(
                    "/watch-configs",
                    data={"repository": value, "focus": "保留这段关注点"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn('role="alert"', response.text)
                self.assertIn("保留这段关注点", response.text)
                self.assertIn("还没有关注配置", self.client.get("/").text)

    def test_saved_text_is_escaped(self) -> None:
        response = self.client.post(
            "/watch-configs",
            data={"repository": "astral-sh/uv", "focus": "<script>alert(1)</script>"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", response.text)
        self.assertNotIn("<script>", response.text)

    def test_optional_focus(self) -> None:
        response = self.client.post(
            "/watch-configs", data={"repository": "astral-sh/uv"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("<td>astral-sh/uv</td><td></td>", response.text)

    def test_config_survives_application_process_restart(self) -> None:
        script = """
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from diff_x_hunter import storage
from diff_x_hunter.web import app
storage.DB_PATH = Path(sys.argv[1])
with TestClient(app) as client:
    if sys.argv[2] == 'save':
        response = client.post('/watch-configs', data={
            'repository': 'astral-sh/uv', 'focus': '重启后保留'
        })
    else:
        response = client.get('/')
    assert response.status_code == 200
    assert '<td>astral-sh/uv</td>' in response.text
    assert '<td>重启后保留</td>' in response.text
"""
        for action in ["save", "read"]:
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.db_path), action],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
