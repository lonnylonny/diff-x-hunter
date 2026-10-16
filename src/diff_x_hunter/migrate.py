# 数据库迁移：先备份，再补充版本或查询结论字段。
"""Back up an existing database before adding Release or query outcome fields."""

import argparse
import sqlite3
from pathlib import Path

from .storage import connect_db


def backup_database(conn: sqlite3.Connection, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建，避免覆盖旧备份或原库。
    with backup_path.open("xb"):
        pass
    with sqlite3.connect(backup_path) as backup:
        conn.backup(backup)


def migrate_release_types(backup_path: Path) -> bool:
    # 检查字段，避免重复迁移。
    conn = connect_db()
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(releases)")}
        if "prerelease" in columns:
            return False
        if not columns:
            raise RuntimeError("releases 表不存在，无法迁移")
        backup_database(conn, backup_path)
        # 旧版本默认按正式版处理。
        with conn:
            conn.execute(
                "ALTER TABLE releases ADD COLUMN prerelease INTEGER NOT NULL "
                "DEFAULT 0 CHECK (prerelease IN (0, 1))"
            )
        return True
    finally:
        conn.close()


def main() -> None:
    # 根据开关选择迁移，备份路径由命令行传入。
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
        # 仅补缺失字段，兼容部分已迁移的库。
        missing = {key: value for key, value in additions.items() if key not in columns}
        if not missing:
            return False
        backup_database(conn, backup_path)
        with conn:
            conn.execute("BEGIN")
            # 同一事务内增列，失败时一起回滚。
            for name, definition in missing.items():
                conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {name} {definition}")
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    main()
