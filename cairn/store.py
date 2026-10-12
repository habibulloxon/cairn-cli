"""The SQLite database (~/.cairn/cairn.db): schema, migrations, connections."""

import sqlite3

from cairn import paths

SCHEMA_VERSION = 2

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS sessions (
      id                 TEXT PRIMARY KEY,
      transcript         TEXT,
      cwd                TEXT,     -- launch directory; `claude --resume` only finds the session from there
      branch             TEXT,     -- latest branch Claude Code recorded
      branches           TEXT,     -- JSON list of every recorded branch, in order first seen
      custom_title       TEXT,
      ai_title           TEXT,
      first_prompt       TEXT,
      last_prompt        TEXT,
      turns              INTEGER DEFAULT 0,
      started_at         TEXT,
      last_active        TEXT,
      pr_url             TEXT,
      summary            TEXT,
      summary_turns      INTEGER DEFAULT 0,  -- prompt count the summary covers
      summary_at         REAL,
      file_size          INTEGER,
      file_mtime         REAL,
      missing            INTEGER DEFAULT 0,  -- transcript deleted by Claude Code's cleanup
      inferred_branch    TEXT,     -- guessed by Haiku from git commands when branch is empty
      inferred_repo      TEXT,
      branch_inferred_at REAL,
      pinned             INTEGER DEFAULT 0,
      archived_at        REAL
    )""",
    "CREATE INDEX IF NOT EXISTS sessions_last_active ON sessions(last_active)",
    """CREATE TABLE IF NOT EXISTS live (
      session_id TEXT PRIMARY KEY,
      status     TEXT,     -- running | waiting | idle | closed
      pid        INTEGER,  -- the claude process, to notice sessions that died without SessionEnd
      updated_at REAL
    )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
      session_id UNINDEXED, body, tokenize="unicode61 remove_diacritics 2"
    )""",
)

# Columns that databases from before SCHEMA_VERSION 2 may lack.
ADDED_COLUMNS = (
    ("inferred_branch", "TEXT"),
    ("inferred_repo", "TEXT"),
    ("branch_inferred_at", "REAL"),
    ("pinned", "INTEGER DEFAULT 0"),
    ("archived_at", "REAL"),
)
DROPPED_COLUMNS = ("next_step",)


def connect():
    """A connection with the schema in place. Rows are sqlite3.Row (index by column name)."""
    paths.DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _migrate(conn)
    return conn


def _migrate(conn):
    # WAL lets hooks, summarizers and the dashboard read and write at the same time.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")  # one process migrates; others wait, then see the new version
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            conn.rollback()
            return
        for statement in SCHEMA:
            conn.execute(statement)
        have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        for col, typ in ADDED_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
        for col in DROPPED_COLUMNS:
            if col in have:
                conn.execute(f"ALTER TABLE sessions DROP COLUMN {col}")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
