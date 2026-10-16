import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.types import CallToolResult, TextContent

from diff_x_hunter.migrate import migrate_release_types, migrate_query_outcomes

from diff_x_hunter import github_mcp, storage


class ReleaseQueryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "repository.db"
        with sqlite3.connect(f"{storage.DB_PATH.as_uri()}?mode=ro", uri=True) as conn:
            schemas = [
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
                ).fetchone()[0]
                for name in ("releases", "tool_calls")
            ]
        with sqlite3.connect(self.db_path) as conn:
            for schema in schemas:
                conn.execute(schema)
        patcher = patch.object(storage, "DB_PATH", self.db_path)
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
            "html_url": "https://example.com/releases/v1.0.0",
            "published_at": "2026-10-15T00:00:00Z",
        }

    def assert_success_saved(self, body: str) -> None:
        releases = storage.load_releases("EXAMPLE/DEMO")
        self.assertEqual(releases, [(
            1, 123, "v1.0.0", body,
            self.release["html_url"], self.release["published_at"], 0,
        )])
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call[1], "list_releases")
        self.assertEqual(json.loads(call[2]), {"owner": "Example", "repo": "Demo", "page": 1, "perPage": 100})
        self.assertEqual(datetime.fromisoformat(call[3]).utcoffset(), timedelta(0))
        self.assertEqual(call[4:], (
            "success", f"第 1 页：取得 1 条 Release；查询完成，选定 v1.0.0（正式版），发布说明 {len(body)} 字符", None, releases[0][0],
        ))

    def assert_error_saved(self, message: str) -> None:
        self.assertEqual(storage.load_releases("example/demo"), [])
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "list_releases")
        self.assertEqual(json.loads(calls[0][2]), {"owner": "Example", "repo": "Demo", "page": 1, "perPage": 100})
        self.assertEqual(calls[0][4:], ("error", None, message, None))

    async def test_structured_result_is_saved_and_survives_new_process(self) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text="结构化结果优先使用")],
            structured_content={"result": [self.release]},
        )
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)) as query:
            self.assertEqual(
                await github_mcp.query_release("Example", "Demo"),
                github_mcp.QueryResult("found", release=self.release),
            )
            query.assert_awaited_once_with("Example", "Demo", 1)
        self.assert_success_saved(self.release["body"])

        script = """
import json
import sys
from pathlib import Path
from diff_x_hunter import storage
storage.DB_PATH = Path(sys.argv[1])
releases = storage.load_releases('example/demo')
calls = storage.load_tool_calls()
assert len(releases) == len(calls) == 1
assert releases[0][1] == 123
assert releases[0][3] == '固定测试发布说明'
assert calls[0][4] == 'success'
assert calls[0][7] == releases[0][0]
assert json.loads(calls[0][2]) == {'owner': 'Example', 'repo': 'Demo', 'page': 1, 'perPage': 100}
"""
        check = subprocess.run(
            [sys.executable, "-c", script, str(self.db_path)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    async def test_json_text_result_is_saved(self) -> None:
        result = CallToolResult(content=[TextContent(
            type="text", text=json.dumps([self.release], ensure_ascii=False),
        )])
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            await github_mcp.query_release("Example", "Demo")
        self.assert_success_saved(self.release["body"])

    async def test_missing_or_null_body_is_saved_as_empty_text(self) -> None:
        del self.release["body"]
        for body in ("missing", None):
            with self.subTest(body=body):
                if body is None:
                    self.release["body"] = None
                result = CallToolResult(content=[], structured_content={"result": [self.release]})
                with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
                    await github_mcp.query_release("Example", "Demo")
                self.assert_success_saved("")
                with storage.connect_db() as conn:
                    conn.execute("DELETE FROM tool_calls")
                    conn.execute("DELETE FROM releases")

    async def test_repeated_queries_reuse_release_and_preserve_first_data(self) -> None:
        result = CallToolResult(content=[], structured_content={"result": [self.release]})
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            await github_mcp.query_release("Example", "Demo")
            await github_mcp.query_release("example", "demo")

        edited = self.release | {"body": "后续编辑的说明"}
        with patch.object(github_mcp, "list_releases", new=AsyncMock(
            return_value=CallToolResult(content=[], structured_content={"result": [edited]}),
        )):
            await github_mcp.query_release("Example", "Demo")

        releases = storage.load_releases("example/demo")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][3], self.release["body"])
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual([call[4] for call in calls], ["success"] * 3)
        self.assertEqual([call[7] for call in calls], [releases[0][0]] * 3)
        self.assertEqual(calls[-1][5], f"第 1 页：取得 1 条 Release；查询完成，选定 v1.0.0（正式版），发布说明 {len(edited['body'])} 字符")

    async def test_different_github_release_id_creates_new_record(self) -> None:
        for release in (self.release, self.release | {"id": 124}):
            result = CallToolResult(content=[], structured_content={"result": [release]})
            with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
                await github_mcp.query_release("Example", "Demo")

        releases = storage.load_releases("example/demo")
        self.assertEqual({release[1] for release in releases}, {123, 124})
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[4] for call in calls], ["success", "success"])
        self.assertEqual({call[7] for call in calls}, {release[0] for release in releases})

    async def test_query_after_process_restart_reuses_release(self) -> None:
        script = """
import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch
from mcp.types import CallToolResult
from diff_x_hunter import github_mcp, storage
storage.DB_PATH = Path(sys.argv[1])
if sys.argv[3] == 'query':
    result = CallToolResult(content=[], structured_content={"result": [json.loads(sys.argv[2])]})
    with patch.object(github_mcp, 'list_releases', new=AsyncMock(return_value=result)):
        asyncio.run(github_mcp.query_release('Example', 'Demo'))
else:
    releases = storage.load_releases('example/demo')
    calls = storage.load_tool_calls()
    assert len(releases) == 1 and len(calls) == 2
    assert releases[0][1] == 123
    assert releases[0][3] == json.loads(sys.argv[2])['body']
    assert [call[4] for call in calls] == ['success', 'success']
    assert [call[7] for call in calls] == [releases[0][0], releases[0][0]]
"""
        for action in ("query", "query", "read"):
            check = subprocess.run(
                [sys.executable, "-c", script, str(self.db_path),
                 json.dumps(self.release, ensure_ascii=False), action],
                capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    async def test_tool_error_is_saved_and_returned(self) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text="固定工具错误")], is_error=True,
        )
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result.reason_code, "unknown")
        self.assert_error_saved("RuntimeError: 固定工具错误")

    async def test_query_timeout_is_saved_and_returned_as_failure(self) -> None:
        error = TimeoutError("固定测试超时")
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=error)):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result, github_mcp.QueryResult("failed", reason_code="timeout"))
        self.assert_error_saved("TimeoutError: 固定测试超时")

    async def test_invalid_json_is_saved_as_error(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="无效 JSON")])
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result.reason_code, "invalid_response")
        self.assert_error_saved("JSONDecodeError: Expecting value: line 1 column 1 (char 0)")

    async def test_only_prereleases_use_github_flag_not_tag_name(self) -> None:
        release = self.release | {"prerelease": True}
        result = CallToolResult(content=[], structured_content={"result": [release]})
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            await github_mcp.query_release("Example", "Demo")
        self.assertEqual(storage.load_releases("example/demo")[0][-1], 1)
        self.assertIn("预发布版", storage.load_tool_calls()[0][5])

    async def test_mixed_unsorted_releases_filter_drafts_and_compare_times(self) -> None:
        newest = self.release | {
            "id": 124, "prerelease": True,
            "published_at": "2026-10-15T00:30:00Z",
        }
        releases = [
            self.release | {"id": 125, "draft": True, "published_at": None},
            self.release | {"id": 126, "published_at": "2026-10-15T08:00:00+08:00"},
            newest, self.release,
        ]
        result = CallToolResult(content=[], structured_content={"result": releases})
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            self.assertEqual(
                await github_mcp.query_release("Example", "Demo"),
                github_mcp.QueryResult("found", release=newest),
            )
        self.assertEqual([r[1] for r in storage.load_releases("example/demo")], [124])

    async def test_latest_can_be_on_later_page_and_every_call_is_recorded(self) -> None:
        newest = self.release | {"id": 999, "published_at": "2026-10-16T00:00:00Z"}
        pages = [[self.release | {"id": i} for i in range(100)], [newest]]
        results = [CallToolResult(content=[], structured_content={"result": p}) for p in pages]
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=results)) as query:
            self.assertEqual(
                await github_mcp.query_release("Example", "Demo"),
                github_mcp.QueryResult("found", release=newest),
            )
        self.assertEqual(query.await_count, 2)
        calls = storage.load_tool_calls()
        self.assertEqual([json.loads(c[2])["page"] for c in calls], [1, 2])
        self.assertEqual([c[4] for c in calls], ["success", "success"])
        releases = storage.load_releases("example/demo")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][1], 999)
        self.assertIsNone(calls[0][7])
        self.assertEqual(calls[1][7], releases[0][0])

    async def test_full_page_requires_another_call_even_if_all_drafts(self) -> None:
        pages = [[self.release | {"draft": True}] * 100, [self.release] * 100, []]
        results = [CallToolResult(content=[], structured_content={"result": p}) for p in pages]
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=results)):
            await github_mcp.query_release("Example", "Demo")
        calls = storage.load_tool_calls()
        self.assertEqual([json.loads(c[2])["page"] for c in calls], [1, 2, 3])
        self.assertEqual(len(storage.load_releases("example/demo")), 1)
        self.assertIn("查询完成，选定", calls[-1][5])

    async def test_empty_or_only_drafts_is_success_without_release(self) -> None:
        for releases in ([], [self.release | {"draft": True, "published_at": None}]):
            result = CallToolResult(content=[], structured_content={"result": releases})
            with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
                self.assertEqual(
                    await github_mcp.query_release("Example", "Demo"),
                    github_mcp.QueryResult("empty"),
                )
        self.assertEqual(storage.load_releases("example/demo"), [])
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[4], "success")
            self.assertIn("暂无已发布版本", call[5])
            self.assertIsNone(call[7])

    async def test_failed_second_page_does_not_save_partial_latest(self) -> None:
        first = CallToolResult(content=[], structured_content={"result": [self.release] * 100})
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=[first, TimeoutError("page two")])):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result, github_mcp.QueryResult("failed", reason_code="timeout"))
        self.assertEqual(storage.load_releases("example/demo"), [])
        calls = storage.load_tool_calls()
        self.assertEqual([c[4] for c in calls], ["success", "error"])
        self.assertEqual(json.loads(calls[1][2])["page"], 2)
        self.assertEqual(calls[1][6], "TimeoutError: page two")
        self.assertTrue(all(c[7] is None for c in calls))

    async def test_404_remains_failure(self) -> None:
        result = CallToolResult(content=[TextContent(type="text", text="404 Not Found")], is_error=True)
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result, github_mcp.QueryResult("failed", reason_code="unknown"))
        self.assert_error_saved("RuntimeError: 404 Not Found")

    async def test_non_list_response_is_not_treated_as_empty(self) -> None:
        result = CallToolResult(content=[], structured_content={"unexpected": []})
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
            result = await github_mcp.query_release("Example", "Demo")
        self.assertEqual(result.reason_code, "invalid_response")
        self.assertEqual(storage.load_tool_calls()[0][4], "error")

    async def test_prerelease_promotion_reuses_id_and_preserves_history(self) -> None:
        for prerelease in (True, False):
            release = self.release | {"prerelease": prerelease}
            result = CallToolResult(content=[], structured_content={"result": [release]})
            with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=result)):
                await github_mcp.query_release("Example", "Demo")
        releases = storage.load_releases("example/demo")
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0][-1], 0)
        calls = storage.load_tool_calls()
        self.assertEqual([c[7] for c in calls], [releases[0][0]] * 2)
        self.assertIn("预发布版", calls[0][5])
        self.assertIn("正式版", calls[1][5])


if __name__ == "__main__":
    unittest.main()
