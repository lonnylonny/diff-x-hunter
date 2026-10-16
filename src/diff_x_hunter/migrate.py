"""Back up an existing database before adding Release or query outcome fields."""

import argparse
import sqlite3
from pathlib import Path

from .storage import connect_db


def migrate_release_types(backup_path: Path) -> bool:
    conn = connect_db()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
        if "prerelease" in columns:
            return False
        if not columns:
            raise RuntimeError("releases 表不存在，无法迁移")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        # Never overwrite an earlier backup, including the source database.
        with backup_path.open("xb"):
            pass
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        with conn:
            conn.execute(
                "ALTER TABLE releases ADD COLUMN prerelease INTEGER NOT NULL "
                "DEFAULT 0 CHECK (prerelease IN (0, 1))"
            )
        return True
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup_path", type=Path)
    parser.add_argument("--query-outcomes", action="store_true",
                        help="添加整次查询结论、原因及失败环节字段")
    args = parser.parse_args()
    migrate = migrate_query_outcomes if args.query_outcomes else migrate_release_types
    changed = migrate(args.backup_path)
    print(f"迁移完成，原库备份：{args.backup_path}" if changed else "已迁移，无需重复操作")


def migrate_query_outcomes(backup_path: Path) -> bool:
    conn = connect_db()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")}
        if not columns:
            raise RuntimeError("tool_calls 表不存在，无法迁移")
        additions = {
            "query_outcome": "TEXT CHECK (query_outcome IN ('found', 'empty', 'inaccessible', 'failed'))",
            "reason_code": "TEXT",
            "failure_stage": "TEXT CHECK (failure_stage IN ('query', 'parse', 'save'))",
        }
        missing = {key: value for key, value in additions.items() if key not in columns}
        if not missing:
            return False
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with backup_path.open("xb"):
            pass
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        with conn:
            conn.execute("BEGIN")
            for name, definition in missing.items():
                conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {name} {definition}")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    main()
