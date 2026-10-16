"""9.1 的新接口验收；核心实现完成前，这些契约测试预计失败。

运行：uv run python -m unittest discover -s tests -p test_query_outcomes.py -v
只模拟 MCP 边界，查询与 SQLite 写入使用真实代码。
"""

import asyncio
import sqlite3
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp.types import CallToolResult, TextContent
from mcp.shared.exceptions import MCPError

from diff_x_hunter import github_mcp, storage
from diff_x_hunter.migrate import migrate_release_types, migrate_query_outcomes


class QueryOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / "repository.db"
        self.db_path = db_path
        # 沿用现有测试约定：只读取原库 schema，数据全部写入临时库。
        with sqlite3.connect(f"{storage.DB_PATH.as_uri()}?mode=ro", uri=True) as source:
            schemas = [
                source.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ?", (table,),
                ).fetchone()[0]
                for table in ("releases", "tool_calls")
            ]
        with sqlite3.connect(db_path) as conn:
            for schema in schemas:
                conn.execute(schema)
        patcher = patch.object(storage, "DB_PATH", db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        migrate_release_types(Path(temp.name) / "backup.db")
        migrate_query_outcomes(Path(temp.name) / "query-backup.db")
        self.release = {
            "id": 123, "draft": False, "prerelease": False,
            "tag_name": "v1.0.0", "body": "已保存的说明",
            "html_url": "https://example.com/releases/v1.0.0",
            "published_at": "2026-10-15T00:00:00Z",
        }

    def page(self, releases: list[dict]) -> CallToolResult:
        return CallToolResult(content=[], structured_content={"result": releases})

    async def test_found_returns_release_and_saves_real_data(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(
            return_value=self.page([self.release]),
        )):
            result = await github_mcp.query_release("example", "demo")
        self.assertEqual(result.outcome, "found")
        self.assertEqual(result.release, self.release)
        self.assertIsNone(result.reason_code)
        releases = storage.load_releases("example/demo")
        self.assertEqual([row[1] for row in releases], [123])
        self.assertEqual(storage.load_tool_calls()[-1][7], releases[0][0])

    async def test_empty_and_only_drafts_return_empty_without_fake_release(self) -> None:
        for releases in ([], [self.release | {"draft": True, "published_at": None}]):
            with self.subTest(releases=releases):
                with patch.object(github_mcp, "list_releases", new=AsyncMock(
                    return_value=self.page(releases),
                )):
                    result = await github_mcp.query_release("example", "demo")
                self.assertEqual(result.outcome, "empty")
                self.assertIsNone(result.release)
                self.assertIsNone(result.reason_code)
                self.assertEqual(storage.load_releases("example/demo"), [])
                self.assertEqual(storage.load_tool_calls()[-1][4], "success")

    async def test_timeout_returns_failed_with_reason_and_error_record(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(
            side_effect=TimeoutError("controlled timeout"),
        )) as query:
            result = await github_mcp.query_release("example", "demo")
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "timeout")
        self.assertIsNone(result.release)
        self.assertEqual(storage.load_releases("example/demo"), [])
        self.assertEqual(storage.load_tool_calls()[-1][4], "error")
        query.assert_awaited_once_with("example", "demo", 1)

    async def test_deadline_cancels_unresponsive_call_and_records_timeout(self) -> None:
        cancelled = asyncio.Event()

        async def never_returns(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(github_mcp, "QUERY_TIMEOUT_SECONDS", 0.02), patch.object(
            github_mcp, "list_releases", new=AsyncMock(side_effect=never_returns),
        ) as query:
            # 外层只防止测试挂住；若触发它，测试报错而不是冒充业务超时成功。
            result = await asyncio.wait_for(
                github_mcp.query_release("example", "demo"), timeout=1,
            )
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "timeout")
        self.assertIsNone(result.release)
        self.assertTrue(cancelled.is_set())
        query.assert_awaited_once_with("example", "demo", 1)
        self.assertEqual(storage.load_releases("example/demo"), [])
        calls = storage.load_tool_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][4], "error")
        self.assertIn("TimeoutError", calls[0][6])

    async def test_deadline_is_shared_across_pages(self) -> None:
        async def delayed_page(owner, repo, page):
            # 每页均小于总预算，但两页合计超过预算。
            await asyncio.sleep(0.08)
            return self.page([self.release] * github_mcp.PAGE_SIZE if page == 1 else [])

        with patch.object(github_mcp, "QUERY_TIMEOUT_SECONDS", 0.12), patch.object(
            github_mcp, "list_releases", new=AsyncMock(side_effect=delayed_page),
        ):
            result = await asyncio.wait_for(
                github_mcp.query_release("example", "demo"), timeout=1,
            )
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "timeout")
        self.assertIsNone(result.release)
        self.assertEqual(storage.load_releases("example/demo"), [])
        calls = storage.load_tool_calls()
        self.assertEqual(calls[-1][4], "error")
        self.assertTrue(all(call[7] is None for call in calls))

    async def test_unclassified_tool_error_keeps_unknown_reason(self) -> None:
        response = CallToolResult(
            content=[TextContent(type="text", text="诊断编号 404；原因未提供")],
            is_error=True,
        )
        with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=response)):
            result = await github_mcp.query_release("example", "demo")
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "unknown")
        self.assertIsNone(result.release)
        self.assertEqual(storage.load_releases("example/demo"), [])
        self.assertEqual(storage.load_tool_calls()[-1][4], "error")

    async def test_invalid_response_returns_failure_not_empty(self) -> None:
        responses = (
            CallToolResult(content=[TextContent(type="text", text="invalid JSON")]),
            CallToolResult(content=[], structured_content={"unexpected": []}),
        )
        for response in responses:
            with self.subTest(response=response):
                with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=response)):
                    result = await github_mcp.query_release("example", "demo")
                self.assertEqual(result.outcome, "failed")
                self.assertEqual(result.reason_code, "invalid_response")
                self.assertIsNone(result.release)
                self.assertEqual(storage.load_releases("example/demo"), [])
                self.assertEqual(storage.load_tool_calls()[-1][4], "error")

    async def test_second_page_failure_preserves_old_data_without_saving_candidate(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(
            return_value=self.page([self.release]),
        )):
            await github_mcp.query_release("example", "demo")
        before = storage.load_releases("example/demo")
        candidate = self.release | {"id": 999, "published_at": "2026-10-16T00:00:00Z"}
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=[
            self.page([candidate] * github_mcp.PAGE_SIZE), TimeoutError("page two"),
        ])) as query:
            result = await github_mcp.query_release("example", "demo")
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "timeout")
        self.assertIsNone(result.release)
        self.assertEqual(storage.load_releases("example/demo"), before)
        calls = storage.load_tool_calls()
        self.assertEqual([row[4] for row in calls], ["success", "success", "error"])
        self.assertTrue(all(row[7] is None for row in calls[1:]))
        self.assertEqual(query.await_count, 2)

    async def test_empty_result_does_not_delete_previously_saved_release(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=[
            self.page([self.release]), self.page([]),
        ])):
            await github_mcp.query_release("example", "demo")
            before = storage.load_releases("example/demo")
            result = await github_mcp.query_release("example", "demo")
        self.assertEqual(result.outcome, "empty")
        self.assertIsNone(result.release)
        self.assertEqual(storage.load_releases("example/demo"), before)
        self.assertIsNone(storage.load_tool_calls()[-1][7])

    async def test_storage_failure_is_not_converted_to_external_query_failure(self) -> None:
        for error in (
            sqlite3.OperationalError("controlled write failure"),
            ValueError("controlled save validation failure"),
            RuntimeError("controlled save failure"),
        ):
            with self.subTest(error=type(error).__name__):
                with patch.object(github_mcp, "list_releases", new=AsyncMock(
                    return_value=self.page([self.release]),
                )), patch.object(github_mcp, "save_release", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        await github_mcp.query_release("example", "demo")
                self.assertIs(raised.exception, error)
                self.assertEqual(storage.load_releases("example/demo"), [])
                self.assertEqual(storage.load_tool_calls()[-1][4], "error")
                saved = storage.load_query_results("example/demo")[-1]
                self.assertEqual(saved["query_outcome"], "failed")
                self.assertEqual(saved["reason_code"], "save_failed")
                self.assertEqual(saved["failure_stage"], "save")

    async def test_access_and_rate_limit_require_matching_endpoint_evidence(self) -> None:
        prefix = "failed to list releases: GET https://api.github.com/repos/example/demo/releases?per_page=100: "
        cases = (
            (prefix + "404 Not Found []", "inaccessible", "access_unavailable"),
            (prefix + "401 Bad credentials []", "inaccessible", "access_unavailable"),
            (prefix + "403 Resource not accessible by personal access token []", "inaccessible", "access_unavailable"),
            (prefix + "403 API rate limit exceeded", "failed", "rate_limited"),
            (prefix + "429 Too Many Requests", "failed", "rate_limited"),
            (prefix + "403 Forbidden []", "failed", "unknown"),
            (prefix.replace("example/demo", "other/repo") + "404 Not Found []", "failed", "unknown"),
            ("404 Not Found", "failed", "unknown"),
        )
        for message, outcome, reason in cases:
            for transport in ("tool", "rpc"):
                with self.subTest(message=message, transport=transport):
                    mock = AsyncMock(return_value=CallToolResult(
                        content=[TextContent(type="text", text=message)], is_error=True,
                    )) if transport == "tool" else AsyncMock(side_effect=MCPError(-32603, message))
                    with patch.object(github_mcp, "list_releases", new=mock):
                        result = await github_mcp.query_release("example", "demo")
                    self.assertEqual((result.outcome, result.reason_code), (outcome, reason))
                    self.assertIsNone(result.release)
                    saved = storage.load_query_results("EXAMPLE/DEMO")[-1]
                    self.assertEqual(saved["query_outcome"], outcome)
                    self.assertEqual(saved["reason_code"], reason)
                    self.assertEqual(saved["failure_stage"], "query")
                    self.assertEqual(storage.load_releases("example/demo"), [])

    async def test_only_terminal_page_has_whole_query_outcome(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=[
            self.page([self.release] * github_mcp.PAGE_SIZE), self.page([]),
        ])):
            await github_mcp.query_release("example", "demo")
        with storage.connect_db() as conn:
            rows = conn.execute("SELECT query_outcome FROM tool_calls ORDER BY id").fetchall()
        self.assertEqual(rows, [(None,), ("found",)])
        self.assertEqual(len(storage.load_query_results("example/demo")), 1)

    async def test_success_empty_and_failure_survive_new_process(self) -> None:
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=[
            self.page([self.release]), self.page([]), TimeoutError("controlled timeout"),
        ])):
            for _ in range(3):
                await github_mcp.query_release("example", "demo")
        script = '''
import json, sys
from pathlib import Path
from diff_x_hunter import storage
storage.DB_PATH = Path(sys.argv[1])
print(json.dumps([storage.load_query_results("example/demo"), storage.load_releases("example/demo")]))
'''
        proc = subprocess.run([sys.executable, "-c", script, str(self.db_path)],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        outcomes, releases = json.loads(proc.stdout)
        self.assertEqual([row["query_outcome"] for row in outcomes], ["found", "empty", "failed"])
        self.assertEqual(outcomes[-1]["reason_code"], "timeout")
        self.assertEqual(outcomes[-1]["failure_stage"], "query")
        self.assertIsNone(outcomes[-1]["release_id"])
        self.assertEqual(len(releases), 1)
        self.assertEqual(outcomes[0]["release_id"], releases[0][0])
        self.assertEqual(storage.load_query_results("other/repo"), [])

    async def test_invalid_release_is_persisted_as_parse_failure(self) -> None:
        for bad in (self.release | {"draft": "false"}, self.release | {"tag_name": None}, {}):
            with self.subTest(release=bad):
                with patch.object(github_mcp, "list_releases", new=AsyncMock(return_value=self.page([bad]))):
                    result = await github_mcp.query_release("example", "demo")
                self.assertEqual(result.reason_code, "invalid_response")
                saved = storage.load_query_results("example/demo")[-1]
                self.assertEqual(saved["failure_stage"], "parse")
                self.assertEqual(storage.load_releases("example/demo"), [])

    async def test_persisted_error_redacts_credentials(self) -> None:
        secret = "test-configured-secret"
        message = f"failure {secret} ghp_fakecredential github_pat_fakecredential Authorization: Bearer other-secret"
        with patch.dict("os.environ", {"GITHUB_PERSONAL_ACCESS_TOKEN": secret}), patch.object(
            github_mcp, "list_releases", new=AsyncMock(side_effect=RuntimeError(message)),
        ):
            await github_mcp.query_release("example", "demo")
        error = storage.load_query_results("example/demo")[-1]["error"]
        for value in (secret, "ghp_fakecredential", "github_pat_fakecredential", "other-secret"):
            self.assertNotIn(value, error)
        self.assertIn("[REDACTED]", error)

    async def test_failed_error_record_does_not_hide_original_failure(self) -> None:
        original = TimeoutError("original timeout")
        with patch.object(github_mcp, "list_releases", new=AsyncMock(side_effect=original)), patch.object(
            github_mcp, "save_tool_call", side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            with self.assertRaises(TimeoutError) as raised:
                await github_mcp.query_release("example", "demo")
        self.assertIs(raised.exception, original)
        self.assertIn("本次失败记录未能保存", original.__notes__)
        self.assertEqual(storage.load_query_results("example/demo"), [])


if __name__ == "__main__":
    unittest.main()
