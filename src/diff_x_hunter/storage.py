import sqlite3
from pathlib import Path
import json


DB_PATH = Path(__file__).resolve().parent / "owner_repo" / "repository.db"

def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def save_config(repository: str, focus: str) -> None:
    # Saves the configuration for repository.db .
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
    # Load the configurations for repository.db .
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
    repository = repository.lower()
    conn = connect_db()
    try:
        conn.execute(
            "INSERT INTO releases "
            "(repository, github_release_id, tag_name, body, html_url, published_at, prerelease) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repository, github_release_id) DO UPDATE SET "
            "prerelease = excluded.prerelease",
            (repository, github_release_id, tag_name, body, html_url, published_at, prerelease),
        )
        row = conn.execute(
            "SELECT id FROM releases WHERE repository = ? AND github_release_id = ?",
            (repository, github_release_id),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def load_releases(repository: str) -> list[tuple[int, int, str, str, str, str, int]]:
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
        return [dict(row) for row in rows]
    finally:
        conn.close()
