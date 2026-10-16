import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from mcp.types import CallToolResult, TextContent

from diff_x_hunter.migrate import migrate_release_types, migrate_query_outcomes

from diff_x_hunter import storage
from diff_x_hunter.github_mcp import QueryResult
from diff_x_hunter.storage import load_configs, load_releases, load_tool_calls
from diff_x_hunter.web import app


class WatchConfigWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "repository.db"
        with sqlite3.connect(f"{storage.DB_PATH.as_uri()}?mode=ro", uri=True) as conn:
            schemas = [
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                for name in ("watch_configs", "releases", "tool_calls")
            ]
        with sqlite3.connect(self.db_path) as conn:
            for schema in schemas:
                conn.execute(schema)
        patcher = patch("diff_x_hunter.storage.DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        migrate_release_types(Path(self.temp_dir.name) / "backup.db")
        migrate_query_outcomes(Path(self.temp_dir.name) / "query-backup.db")
        self.release = {
            "id": 123,
            "draft": False,
            "prerelease": False,
            "tag_name": "v1.0.0",
            "body": "固定测试发布说明",
            "html_url": "https://github.com/astral-sh/uv/releases/tag/v1.0.0",
            "published_at": "2026-10-15T00:00:00Z",
        }
        self.result = CallToolResult(content=[], structured_content={"result": [self.release]})
        query_patcher = patch(
            "diff_x_hunter.github_mcp.list_releases",
            new_callable=AsyncMock,
            return_value=self.result,
        )
        self.mcp_query = query_patcher.start()
        self.addCleanup(query_patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_save_redirect_and_reload(self) -> None:
        self.assertIn("还没有关注配置", self.client.get("/").text)

        async def check_config_saved(owner: str, repo: str, page: int) -> CallToolResult:
            self.assertEqual(load_configs(), [("astral-sh/uv", "依赖解析变化")])
            return self.result

        self.mcp_query.side_effect = check_config_saved
        response = self.client.post(
            "/watch-configs",
            data={"repository": " astral-sh/uv ", "focus": "依赖解析变化"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/")
        success_page = self.client.get(response.headers["location"])
        self.assertIn('<p role="status">配置及查询结果已保存</p>', success_page.text)
        releases = load_releases("astral-sh/uv")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][1:], (
            123, "v1.0.0", self.release["body"],
            self.release["html_url"], self.release["published_at"], 0,
        ))
        calls = load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "list_releases")
        self.assertEqual(json.loads(calls[0][2]), {"owner": "astral-sh", "repo": "uv", "page": 1, "perPage": 100})
        self.assertTrue(calls[0][3])
        self.assertEqual(calls[0][4:], (
            "success", f"第 1 页：取得 1 条 Release；查询完成，选定 v1.0.0（正式版），发布说明 {len(self.release['body'])} 字符",
            None, releases[0][0],
        ))
        for _ in range(2):
            page = self.client.get("/")
            self.assertEqual(page.status_code, 200)
            config_section = page.text.split("<h2>已保存的版本资料</h2>")[0]
            self.assertEqual(config_section.count("<td>astral-sh/uv</td>"), 1)
            self.assertIn("<td>依赖解析变化</td>", page.text)
            self.assertIn(self.release["body"], page.text)
            self.assertIn(f'href="{self.release["html_url"]}"', page.text)
            self.assertIn(f'href="#release-{releases[0][0]}">v1.0.0</a>', page.text)
            self.assertIn(calls[0][3], page.text)
            self.assertIn(calls[0][5], page.text)
            self.assertIn("配置及查询结果已保存", page.text)
        self.mcp_query.assert_awaited_once_with("astral-sh", "uv", 1)

    def test_url_flags_cannot_fabricate_success(self) -> None:
        page = self.client.get("/")
        self.assertIn('<button type="submit">保存并查询</button>', page.text)
        self.assertNotIn("配置及查询结果已保存", page.text)
        for url in ("/?success=true", "/?success=true&empty=true"):
            self.assertNotIn("配置及查询结果已保存", self.client.get(url).text)
            self.assertNotIn("配置已保存，暂无已发布版本", self.client.get(url).text)
        self.mcp_query.assert_not_awaited()

    def test_invalid_repository_does_not_save_and_preserves_input(self) -> None:
        for value in ["", "invalid", "owner/", "owner/repo/extra"]:
            with self.subTest(repository=value):
                response = self.client.post(
                    "/watch-configs",
                    data={"repository": value, "focus": "保留这段关注点"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn('<p role="alert">', response.text)
                self.assertIn("保留这段关注点", response.text)
                self.assertIn("还没有关注配置", self.client.get("/").text)
        self.mcp_query.assert_not_awaited()
        self.assertEqual(load_configs(), [])
        self.assertEqual(load_releases("astral-sh/uv"), [])
        self.assertEqual(load_tool_calls(), [])

    def test_config_save_failure_does_not_query(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_config BEFORE INSERT ON watch_configs "
                "BEGIN SELECT RAISE(ABORT, 'private config failure'); END"
            )
        response = self.client.post(
            "/watch-configs",
            data={"repository": "astral-sh/uv", "focus": "保留关注点"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("配置保存失败，请稍后重试", response.text)
        self.assertIn('value="astral-sh/uv"', response.text)
        self.assertIn("保留关注点", response.text)
        self.assertNotIn("private config failure", response.text)
        self.assertNotIn("配置及查询结果已保存", response.text)
        self.mcp_query.assert_not_awaited()
        self.assertEqual(load_configs(), [])
        self.assertEqual(load_tool_calls(), [])

    def test_unreadable_database_still_shows_save_error_and_input(self) -> None:
        # A directory cannot be opened as a SQLite database.
        with patch.object(storage, "DB_PATH", Path(self.temp_dir.name)):
            response = self.client.post(
                "/watch-configs",
                data={"repository": "astral-sh/uv", "focus": "<待保留>"},
            )
            page = self.client.get("/?success=true")
        self.assertEqual(response.status_code, 500)
        self.assertIn("配置保存失败，请稍后重试", response.text)
        self.assertIn("暂时无法读取关注配置", response.text)
        self.assertIn('value="astral-sh/uv"', response.text)
        self.assertIn("&lt;待保留&gt;", response.text)
        self.assertNotIn("还没有关注配置", response.text)
        self.assertIn("关注配置读取失败", page.text)
        self.assertNotIn("配置及查询结果已保存", page.text)
        self.mcp_query.assert_not_awaited()

    def test_query_failure_preserves_saved_config(self) -> None:
        self.mcp_query.side_effect = TimeoutError("private timeout detail")
        response = self.client.post(
            "/watch-configs",
            data={"repository": "astral-sh/uv", "focus": "依赖解析变化"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("配置已保存，但本次查询超时，无法确认当前版本", response.text)
        self.assertNotIn("private timeout detail", response.text)
        self.assertNotIn("配置及查询结果已保存", response.text)
        self.mcp_query.assert_awaited_once_with("astral-sh", "uv", 1)
        self.assertEqual(load_configs(), [("astral-sh/uv", "依赖解析变化")])
        self.assertEqual(load_releases("astral-sh/uv"), [])
        calls = load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4:], (
            "error", None, "TimeoutError: private timeout detail", None,
        ))

    def test_failed_query_outcomes_do_not_redirect_to_success(self) -> None:
        # 只验证网页消费结果对象；真实 MCP 到存储的链路由其他测试覆盖。
        for outcome in ("failed", "inaccessible"):
            with self.subTest(outcome=outcome):
                with patch("diff_x_hunter.web.query_release", new=AsyncMock(
                    return_value=QueryResult(outcome, reason_code="unknown"),
                )):
                    response = self.client.post(
                        "/watch-configs", data={"repository": "example/demo"},
                        follow_redirects=False,
                    )
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("location", response.headers)
                self.assertIn('<p role="alert">', response.text)
                self.assertIn("配置已保存", response.text)
                self.assertNotIn("配置及查询结果已保存", response.text)
                self.assertNotIn("暂无已发布版本", response.text)
                self.assertEqual(load_configs()[-1], ("example/demo", ""))
                self.assertEqual(load_releases("example/demo"), [])
        self.mcp_query.assert_not_awaited()

    def test_mcp_error_result_is_recorded_and_shown_as_failure(self) -> None:
        self.mcp_query.return_value = CallToolResult(
            content=[TextContent(type="text", text="private MCP error")],
            is_error=True,
        )
        response = self.client.post("/watch-configs", data={"repository": "astral-sh/uv"})
        self.assertEqual(response.status_code, 502)
        self.assertIn('<p role="alert">', response.text)
        self.assertNotIn("private MCP error", response.text)
        self.assertEqual(load_configs(), [("astral-sh/uv", "")])
        self.assertEqual(load_releases("astral-sh/uv"), [])
        calls = load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4:], (
            "error", None, "RuntimeError: private MCP error", None,
        ))

    def test_release_save_failure_is_recorded_and_shown_as_failure(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_release BEFORE INSERT ON releases "
                "BEGIN SELECT RAISE(ABORT, 'private release failure'); END"
            )
        response = self.client.post("/watch-configs", data={"repository": "astral-sh/uv"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("配置已保存，但查询或结果保存失败", response.text)
        self.assertNotIn("private release failure", response.text)
        self.assertEqual(load_configs(), [("astral-sh/uv", "")])
        self.assertEqual(load_releases("astral-sh/uv"), [])
        calls = load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4:], (
            "error", None, "IntegrityError: private release failure", None,
        ))

    def test_call_save_failure_preserves_release_without_showing_success(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TRIGGER reject_call BEFORE INSERT ON tool_calls "
                "BEGIN SELECT RAISE(ABORT, 'private call failure'); END"
            )
        response = self.client.post("/watch-configs", data={"repository": "astral-sh/uv"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("配置已保存，但查询或结果保存失败", response.text)
        self.assertIn("本次失败记录未能保存", response.text)
        self.assertNotIn("配置及查询结果已保存", response.text)
        self.assertNotIn("private call failure", response.text)
        self.assertEqual(load_configs(), [("astral-sh/uv", "")])
        self.assertEqual(len(load_releases("astral-sh/uv")), 1)
        self.assertEqual(load_tool_calls(), [])

    def test_repeated_submissions_reuse_release_and_add_calls(self) -> None:
        for repository in ("astral-sh/uv", "Astral-Sh/UV"):
            response = self.client.post(
                "/watch-configs", data={"repository": repository},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("配置及查询结果已保存", response.text)
        self.assertEqual(len(load_configs()), 2)
        releases = load_releases("astral-sh/uv")
        self.assertEqual(len(releases), 1)
        calls = load_tool_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[4] for call in calls], ["success", "success"])
        self.assertEqual([call[7] for call in calls], [releases[0][0]] * 2)
        self.assertEqual(response.text.count(f'<article id="release-{releases[0][0]}">'), 1)
        self.assertEqual(len(re.findall(r'<tr id="call-\d+">', response.text)), 2)

    def test_empty_saved_results_do_not_trigger_query(self) -> None:
        page = self.client.get("/")
        self.assertIn("尚无保存的版本资料", page.text)
        self.assertIn("尚无调用记录", page.text)
        self.assertNotIn('<p role="alert">', page.text)
        self.mcp_query.assert_not_awaited()

    def test_saved_repositories_and_calls_are_associated_by_local_release_id(self) -> None:
        saved = []
        for repository, tag, called_at in (
            ("example/first", "v-first", "2026-10-15T01:00:00+00:00"),
            ("example/second", "v-second", "2026-10-15T02:00:00+00:00"),
        ):
            storage.save_config(repository, "")
            # The GitHub IDs are identical; association must use the local ID.
            release_id = storage.save_release(
                repository, 123, tag, f"{repository} 的说明",
                f"https://github.com/{repository}/releases/tag/{tag}",
                "2026-10-14T00:00:00Z",
            )
            owner, repo = repository.split("/")
            storage.save_tool_call(
                "list_releases", {"owner": owner, "repo": repo},
                called_at, "success", f"取得 {tag}", release_id=release_id,
            )
            saved.append((repository, tag, called_at, release_id))

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        rows = re.findall(r'<tr id="call-\d+">(.*?)</tr>', page.text, re.S)
        self.assertEqual(len(rows), 2)
        for row, (repository, tag, called_at, release_id) in zip(rows, saved):
            self.assertIn(repository, row)
            self.assertIn(called_at, row)
            self.assertIn(f'href="#release-{release_id}">{tag}</a>', row)
            self.assertIn(f"取得 {tag}", row)
            self.assertIn(f'<article id="release-{release_id}">', page.text)
            self.assertIn(f"{repository} 的说明", page.text)
        self.assertIn("GitHub 发布时间：2026-10-14T00:00:00Z", page.text)
        self.assertIn("查询时间（UTC）", page.text)
        self.mcp_query.assert_not_awaited()

    def test_failure_record_is_visible_after_reload_without_release(self) -> None:
        storage.save_config("example/failed", "")
        storage.save_tool_call(
            "list_releases", {"owner": "example", "repo": "failed"},
            "2026-10-15T03:00:00+00:00", "error",
            error="private error must not be displayed",
        )
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("尚无保存的版本资料", page.text)
        rows = re.findall(r'<tr id="call-\d+">(.*?)</tr>', page.text, re.S)
        self.assertEqual(len(rows), 1)
        self.assertIn("example/failed", rows[0])
        self.assertIn("2026-10-15T03:00:00+00:00", rows[0])
        self.assertIn("<td>失败</td>", rows[0])
        self.assertIn("无关联版本", rows[0])
        self.assertIn("查询或结果保存失败", rows[0])
        self.assertNotIn("private error", page.text)
        self.assertNotIn('<p role="alert">', page.text)
        self.mcp_query.assert_not_awaited()

    def test_all_saved_versions_are_shown_and_empty_body_has_placeholder(self) -> None:
        storage.save_config("example/demo", "")
        for github_id, tag, body in ((1, "v-old", "旧版说明"), (2, "v-new", "")):
            storage.save_release(
                "example/demo", github_id, tag, body,
                f"https://github.com/example/demo/releases/tag/{tag}",
                "2026-10-14T00:00:00Z",
            )
        page = self.client.get("/")
        self.assertIn("<h4>v-old</h4>", page.text)
        self.assertIn("<h4>v-new</h4>", page.text)
        self.assertIn("旧版说明", page.text)
        self.assertIn("暂无发布说明", page.text)
        self.assertIn("尚无调用记录", page.text)
        self.mcp_query.assert_not_awaited()

    def test_release_and_summary_are_escaped_and_unsafe_source_is_not_linked(self) -> None:
        storage.save_config("example/demo", "")
        release_id = storage.save_release(
            "example/demo", 123, "<version>", "<script>alert(1)</script>\n第二行",
            "javascript:alert(1)", "2026-10-14T00:00:00Z",
        )
        storage.save_tool_call(
            "list_releases", {"owner": "example", "repo": "demo"},
            "2026-10-15T03:00:00+00:00", "success",
            "<img src=x onerror=alert(1)>", release_id=release_id,
        )
        page = self.client.get("/")
        self.assertIn("&lt;version&gt;", page.text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;\n第二行", page.text)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page.text)
        self.assertNotIn("<script>", page.text)
        self.assertNotIn("<img", page.text)
        self.assertNotIn('href="javascript:', page.text)
        self.mcp_query.assert_not_awaited()

    def test_results_read_failure_preserves_config_and_shows_error(self) -> None:
        storage.save_config("example/demo", "已保存的关注点")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP TABLE tool_calls")
        page = self.client.get("/?success=true")
        self.assertEqual(page.status_code, 200)
        self.assertIn("<td>已保存的关注点</td>", page.text)
        self.assertIn("版本资料或调用记录读取失败", page.text)
        self.assertIn("暂时无法读取版本资料", page.text)
        self.assertIn("暂时无法读取调用记录", page.text)
        self.assertNotIn("配置及查询结果已保存", page.text)
        self.assertNotIn("尚无调用记录", page.text)
        self.mcp_query.assert_not_awaited()

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

    def test_saved_data_survives_application_process_restart(self) -> None:
        script = """
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from mcp.types import CallToolResult
from diff_x_hunter import storage
from diff_x_hunter.web import app
storage.DB_PATH = Path(sys.argv[1])
result = CallToolResult(content=[], structured_content={"result": [json.loads(sys.argv[3])]})
with patch('diff_x_hunter.github_mcp.list_releases', new=AsyncMock(return_value=result)) as query, TestClient(app) as client:
    if sys.argv[2] == 'save':
        response = client.post('/watch-configs', data={
            'repository': 'astral-sh/uv', 'focus': '重启后保留'
        })
        query.assert_awaited_once_with('astral-sh', 'uv', 1)
        assert '配置及查询结果已保存' in response.text
    else:
        response = client.get('/')
        query.assert_not_awaited()
    assert response.status_code == 200
    assert '<td>astral-sh/uv</td>' in response.text
    assert '<td>重启后保留</td>' in response.text
    assert '<h4>v1.0.0</h4>' in response.text
    assert json.loads(sys.argv[3])['body'] in response.text
    assert 'href="' + json.loads(sys.argv[3])['html_url'] + '"' in response.text
    releases = storage.load_releases('astral-sh/uv')
    calls = storage.load_tool_calls()
    assert len(releases) == len(calls) == 1
    assert releases[0][1] == 123
    assert releases[0][3] == json.loads(sys.argv[3])['body']
    assert calls[0][4] == 'success'
    assert calls[0][7] == releases[0][0]
    assert calls[0][3] in response.text
    assert calls[0][5] in response.text
    assert 'href="#release-' + str(releases[0][0]) + '">v1.0.0</a>' in response.text
"""
        for action in ["save", "read"]:
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.db_path), action,
                 json.dumps(self.release, ensure_ascii=False)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_published_release_has_distinct_feedback_and_persisted_record(self) -> None:
        self.mcp_query.return_value = CallToolResult(content=[], structured_content={"result": []})
        response = self.client.post("/watch-configs", data={"repository": "example/demo"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("配置已保存，暂无已发布版本", response.text)
        self.assertNotIn('<p role="alert"', response.text)
        self.assertEqual(load_releases("example/demo"), [])
        self.assertEqual(load_tool_calls()[0][4], "success")
        page = self.client.get("/")
        self.assertIn("暂无已发布版本", page.text)
        self.assertIn("无关联版本", page.text)
        self.mcp_query.assert_awaited_once_with("example", "demo", 1)

    def test_prerelease_type_is_visible_in_saved_card_and_call_summary(self) -> None:
        self.release["prerelease"] = True
        self.mcp_query.return_value = CallToolResult(content=[], structured_content={"result": [self.release]})
        response = self.client.post("/watch-configs", data={"repository": "example/demo"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("版本类型：预发布版", response.text)
        self.assertIn("选定 v1.0.0（预发布版）", response.text)
        self.assertIn("版本类型：预发布版", self.client.get("/").text)

    def test_failure_after_success_survives_refresh_and_process_restart(self) -> None:
        self.client.post("/watch-configs", data={"repository": "example/demo"})
        before = load_releases("example/demo")
        obtained = load_tool_calls()[0][3]
        self.mcp_query.side_effect = TimeoutError("private detail")
        response = self.client.post("/watch-configs", data={"repository": "example/demo"})
        self.assertEqual(response.status_code, 502)
        calls_before = load_tool_calls()
        for page in (response, self.client.get("/"), self.client.get("/?success=true")):
            self.assertIn("本次查询超时", page.text)
            self.assertIn("以下为此前保存的资料，不代表本次查询成功", page.text)
            self.assertIn(f"资料取得时间（关联成功调用，UTC）：{obtained}", page.text)
            self.assertIn("失败环节：调用 GitHub MCP", page.text)
            self.assertIn("<h4>v1.0.0</h4>", page.text)
            self.assertNotIn("配置及查询结果已保存", page.text)
            self.assertNotIn("private detail", page.text)
        self.assertEqual(load_releases("example/demo"), before)
        self.assertEqual(load_tool_calls(), calls_before)
        script = '''
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from diff_x_hunter import storage
from diff_x_hunter.web import app
storage.DB_PATH = Path(sys.argv[1])
with patch('diff_x_hunter.github_mcp.list_releases', new=AsyncMock()) as query, TestClient(app) as client:
    page = client.get('/')
    assert '本次查询超时' in page.text
    assert '此前保存的资料' in page.text
    assert '<h4>v1.0.0</h4>' in page.text
    assert '配置及查询结果已保存' not in page.text
    query.assert_not_awaited()
'''
        process = subprocess.run([sys.executable, "-c", script, str(self.db_path)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(load_tool_calls(), calls_before)

    def test_saved_failure_reasons_are_visible_after_reload(self) -> None:
        cases = (
            ("404 Not Found []", "无法确定是仓库不存在还是权限或凭据问题"),
            ("403 API rate limit exceeded", "GitHub 限流"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.mcp_query.return_value = CallToolResult(is_error=True, content=[TextContent(
                    type="text", text="failed to list releases: GET https://api.github.com/repos/example/demo/releases: " + error,
                )])
                response = self.client.post("/watch-configs", data={"repository": "example/demo"})
                self.assertEqual(response.status_code, 502)
                self.assertIn(expected, self.client.get("/").text)
                self.assertNotIn("暂无已发布版本", response.text)
        self.assertEqual(load_releases("example/demo"), [])

    def test_empty_after_success_preserves_old_data_and_marks_it_historical(self) -> None:
        self.client.post("/watch-configs", data={"repository": "example/demo"})
        before = load_releases("example/demo")
        self.mcp_query.return_value = CallToolResult(content=[], structured_content={"result": []})
        self.client.post("/watch-configs", data={"repository": "example/demo"})
        page = self.client.get("/")
        self.assertIn("暂无已发布版本", page.text)
        self.assertIn("此前保存的资料", page.text)
        self.assertEqual(load_releases("example/demo"), before)
        self.assertNotIn("配置及查询结果已保存", page.text)

    def test_newer_incomplete_call_does_not_reuse_old_success_conclusion(self) -> None:
        self.client.post("/watch-configs", data={"repository": "example/demo"})
        storage.save_tool_call("list_releases", {"owner": "example", "repo": "demo", "page": 1},
                               "2026-10-16T03:00:00Z", "success", "第 1 页成功，尚未查完")
        page = self.client.get("/")
        self.assertIn("暂无明确的查询结论", page.text)
        self.assertIn("此前保存的资料", page.text)
        self.assertNotIn("配置及查询结果已保存", page.text)

    def test_saved_release_without_successful_call_has_unknown_acquisition_time(self) -> None:
        storage.save_config("example/demo", "")
        storage.save_release("example/demo", 123, "v1", "旧说明", "https://example.com", "2026-10-15T00:00:00Z")
        page = self.client.get("/")
        self.assertIn("资料取得时间（关联成功调用，UTC）：未知", page.text)
        self.assertIn("暂无明确的查询结论", page.text)
        self.mcp_query.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
