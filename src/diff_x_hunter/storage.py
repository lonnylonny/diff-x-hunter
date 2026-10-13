import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "owner_repo" / "repository.db"

def connect_db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)

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
