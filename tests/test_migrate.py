import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from diff_x_hunter import storage
from diff_x_hunter.migrate import migrate_release_types, migrate_query_outcomes


class ReleaseMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "old.db"
        self.backup_path = Path(self.temp_dir.name) / "backup.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript('''
                CREATE TABLE releases (
                    id INTEGER PRIMARY KEY, repository TEXT, github_release_id INTEGER,
                    tag_name TEXT, body TEXT, html_url TEXT, published_at TEXT,
                    UNIQUE(repository, github_release_id)
                );
                CREATE TABLE watch_configs (repository TEXT, focus TEXT);
                CREATE TABLE tool_calls (
                    id INTEGER PRIMARY KEY, tool_name TEXT, arguments_json TEXT,
                    called_at TEXT, status TEXT, result_summary TEXT, error TEXT,
                    release_id INTEGER REFERENCES releases(id)
                );
                INSERT INTO watch_configs VALUES ('example/demo', '已有关注');
                INSERT INTO releases VALUES (
                    7, 'example/demo', 123, 'v1', '旧说明', 'https://example.com', '2026-10-15T00:00:00Z'
                );
                INSERT INTO tool_calls VALUES (
                    9, 'get_latest_release', '{"owner":"example","repo":"demo"}',
                    '2026-10-15T01:00:00Z', 'success', '取得 v1', NULL, 7
                );
            ''')
        patcher = patch.object(storage, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backup_migration_preserves_data_links_and_is_idempotent(self) -> None:
        configs = storage.load_configs()
        calls = storage.load_tool_calls()
        with sqlite3.connect(self.db_path) as conn:
            old_release = conn.execute("SELECT * FROM releases").fetchone()
        self.assertTrue(migrate_release_types(self.backup_path))
        self.assertEqual(storage.load_releases("example/demo"), [(old_release[0],) + old_release[2:] + (0,)])
        self.assertEqual(storage.load_configs(), configs)
        self.assertEqual(storage.load_tool_calls(), calls)
        with sqlite3.connect(self.backup_path) as backup:
            self.assertEqual(backup.execute("SELECT * FROM releases").fetchall(), [old_release])
            self.assertEqual(backup.execute("SELECT * FROM tool_calls").fetchall(), calls)
            self.assertNotIn("prerelease", {r[1] for r in backup.execute("PRAGMA table_info(releases)")})
        self.assertFalse(migrate_release_types(self.backup_path))
        with storage.connect_db() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_existing_backup_prevents_migration_and_is_not_overwritten(self) -> None:
        self.backup_path.write_text("keep")
        with self.assertRaises(FileExistsError):
            migrate_release_types(self.backup_path)
        self.assertEqual(self.backup_path.read_text(), "keep")
        with sqlite3.connect(self.db_path) as conn:
            self.assertNotIn("prerelease", {r[1] for r in conn.execute("PRAGMA table_info(releases)")})

    def test_query_migration_preserves_legacy_records_without_inventing_outcome(self) -> None:
        old_calls = storage.load_tool_calls()
        with storage.connect_db() as conn:
            old_releases = conn.execute("SELECT * FROM releases").fetchall()
        self.assertTrue(migrate_query_outcomes(self.backup_path))
        self.assertEqual(storage.load_tool_calls(), old_calls)
        self.assertEqual(storage.load_query_results("example/demo"), [])
        with storage.connect_db() as conn:
            self.assertEqual(conn.execute("SELECT * FROM releases").fetchall(), old_releases)
            self.assertEqual(conn.execute("SELECT query_outcome, reason_code, failure_stage FROM tool_calls").fetchall(), [(None, None, None)])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        with sqlite3.connect(self.backup_path) as backup:
            self.assertEqual(backup.execute("SELECT * FROM tool_calls").fetchall(), old_calls)
            self.assertNotIn("query_outcome", {r[1] for r in backup.execute("PRAGMA table_info(tool_calls)")})
        self.assertFalse(migrate_query_outcomes(self.backup_path))

    def test_query_migration_refuses_to_overwrite_backup(self) -> None:
        self.backup_path.write_text("keep")
        with self.assertRaises(FileExistsError):
            migrate_query_outcomes(self.backup_path)
        self.assertEqual(self.backup_path.read_text(), "keep")
        with storage.connect_db() as conn:
            self.assertNotIn("query_outcome", {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")})

    def test_query_migration_rolls_back_all_columns_on_failure(self) -> None:
        conn = storage.connect_db()
        alters = 0

        def authorize(action, *args):
            nonlocal alters
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alters += 1
                if alters == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        with patch("diff_x_hunter.migrate.connect_db", return_value=conn):
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_query_outcomes(self.backup_path)
        with storage.connect_db() as check:
            self.assertNotIn("query_outcome", {r[1] for r in check.execute("PRAGMA table_info(tool_calls)")})
        self.assertTrue(self.backup_path.exists())
