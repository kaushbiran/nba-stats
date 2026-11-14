"""SQLite connection handling and schema management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "nba.db"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"
RAW_DIR = PROJECT_ROOT / "data" / "raw"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with sane defaults for a read-heavy analytics DB."""
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # WAL lets readers run while a backfill writes; the rest are speed knobs
    # that are safe because we can always re-ingest from the cached raw JSON.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables, indexes and views. Safe to run repeatedly."""
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts per table, for a quick health check after ingest."""
    tables = ["players", "teams", "games", "player_games", "team_games"]
    out = {}
    for t in tables:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = 0
    return out
