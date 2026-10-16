# SQLite 存储：关注配置、版本资料和查询记录。
import sqlite3
from pathlib import Path
import json


DB_PATH = Path(__file__).resolve().parent / "owner_repo" / "repository.db"

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # 每次连接都启用外键约束。
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def save_config(repository: str, focus: str) -> None:
    # 写入仓库及关注点，提交后关闭连接。
    conn = connect_db()
    try:
        conn.execute(
            "INSERT INTO watch_configs (repository, focus) VALUES (?, ?)",
            (repository, focus),
        )
        conn.commit()
    finally:
        conn.close()

def load_configs() -> list[tuple[str, str]]:
    # 返回 (仓库, 关注点) 列表。
    conn = connect_db()
    try:
        return conn.execute(
            "SELECT repository, focus FROM watch_configs"
        ).fetchall()
    finally:
        conn.close()


def save_release(
        repository: str,
        github_release_id: int,
        tag_name: str,
        body: str,
        html_url: str,
        published_at: str,
        prerelease: bool = False,
) -> int:
    # 仓库名统一小写，避免大小写造成重复。
    repository = repository.lower()
    conn = connect_db()
    try:
        # 同一 GitHub 版本已存在时，仅更新预发布标记。
        conn.execute(
            "INSERT INTO releases "
            "(repository, github_release_id, tag_name, body, html_url, published_at, prerelease) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repository, github_release_id) DO UPDATE SET "
            "prerelease = excluded.prerelease",
            (repository, github_release_id, tag_name, body, html_url, published_at, prerelease),
        )
        # 返回本地版本 ID，供调用记录关联。
        row = conn.execute(
            "SELECT id FROM releases WHERE repository = ? AND github_release_id = ?",
            (repository, github_release_id),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def load_releases(repository: str) -> list[tuple[int, int, str, str, str, str, int]]:
    # 按仓库读取版本，元组字段顺序与 SELECT 一致。
    repository = repository.lower()
    conn = connect_db()
    try:
        return conn.execute(
            "SELECT id, github_release_id, tag_name, body, html_url, published_at, prerelease FROM releases WHERE repository = ?",
            (repository,),
        ).fetchall()
    finally:
        conn.close()


def save_tool_call(
    tool_name: str,
    arguments: dict[str, str | int],
    called_at: str,
    status: str,
    result_summary: str | None = None,
    error: str | None = None,
    release_id: int | None = None,
    *,
    query_outcome: str | None = None,
    reason_code: str | None = None,
    failure_stage: str | None = None,
) -> int:
    # 参数存为 JSON，保留中文；整次查询结论可为空。
    arguments_json = json.dumps(arguments, ensure_ascii=False)
    conn = connect_db()
    try:
        cursor = conn.execute(
            "INSERT INTO tool_calls (tool_name, arguments_json, called_at, status, "
            "result_summary, error, release_id, query_outcome, reason_code, failure_stage) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tool_name, arguments_json, called_at, status, result_summary, error,
             release_id, query_outcome, reason_code, failure_stage),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()




def load_tool_calls() -> list[tuple]:
    # 按写入顺序读取调用记录。
    conn = connect_db()
    try:
        return conn.execute(
            "SELECT id, tool_name, arguments_json, called_at, status, "
            "result_summary, error, release_id "
            "FROM tool_calls ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def load_query_results(repository: str) -> list[dict]:
    """Read persisted whole-query outcomes, oldest first; omit legacy/partial calls."""
    # 从调用参数匹配仓库，仅返回完整查询结论。
    conn = connect_db()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, called_at, query_outcome, reason_code, failure_stage, "
            "error, release_id FROM tool_calls WHERE query_outcome IS NOT NULL "
            "AND lower(json_extract(arguments_json, '$.owner') || '/' || "
            "json_extract(arguments_json, '$.repo')) = ? ORDER BY id",
            (repository.lower(),),
        ).fetchall()
        # 转成字典，供页面按字段名读取。
        return [dict(row) for row in rows]
    finally:
        conn.close()
