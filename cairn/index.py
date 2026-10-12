"""Keeping the database in step with the transcripts on disk, plus the transcript archive."""

import json
import os
import re
import shutil
from pathlib import Path

from cairn import paths, transcripts
from cairn.util import log

FTS_VERSION = "1"  # bump to rebuild the search index (and re-link transcripts) on the next sync


def _upsert(conn, info, size, mtime):
    info = dict(info, branches=json.dumps(info["branches"]), file_size=size, file_mtime=mtime, missing=0)
    cols = list(info)
    conn.execute(
        f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'id')}",
        [info[c] for c in cols],
    )


def _index_text(conn, session_id, dialogue):
    conn.execute("DELETE FROM fts WHERE session_id=?", (session_id,))
    body = "\n".join(f"{'$ ' if who == 'Cmd' else ''}{text}" for who, text in dialogue)
    if body:
        conn.execute("INSERT INTO fts (session_id, body) VALUES (?, ?)", (session_id, body))


def ingest_file(conn, path, force=False):
    """Re-read one transcript if it changed (or force). Returns True if it was read."""
    try:
        st = path.stat()
    except OSError:
        return False
    if not force:
        row = conn.execute("SELECT file_size, file_mtime FROM sessions WHERE id=?", (path.stem,)).fetchone()
        if row and row["file_size"] == st.st_size and row["file_mtime"] == st.st_mtime:
            return False
    info, dialogue = transcripts.parse(path)
    _upsert(conn, info, st.st_size, st.st_mtime)
    _index_text(conn, info["id"], dialogue)
    archive_transcript(path)
    return True


def sync(conn):
    """Ingest every changed transcript and flag sessions whose file is gone. Returns the count read."""
    built = conn.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
    force = not built or built["value"] != FTS_VERSION
    seen, changed = set(), 0
    for path in paths.PROJECTS.glob("*/*.jsonl"):
        seen.add(path.stem)
        try:
            changed += ingest_file(conn, path, force=force)
        except Exception as e:  # one bad file shouldn't stop the sweep
            log(f"ingest {path}: {e!r}")
    for row in conn.execute("SELECT id FROM sessions WHERE missing=0").fetchall():
        if row["id"] not in seen:
            conn.execute("UPDATE sessions SET missing=1 WHERE id=?", (row["id"],))
    if force:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('fts_version', ?)", (FTS_VERSION,))
    conn.commit()
    return changed


def search(conn, text, limit=200):
    """{session_id: snippet} for sessions whose transcript matches every word, best first.

    Matches are wrapped in \\x02 … \\x03 in the snippet.
    """
    words = re.findall(r"\w+", text)
    if not words:
        return {}
    query = " ".join(f'"{w}"*' for w in words)  # quoted prefix terms: no FTS syntax from the user
    rows = conn.execute(
        "SELECT session_id, snippet(fts, 1, '\x02', '\x03', '…', 14) AS snip FROM fts "
        "WHERE fts MATCH ? ORDER BY rank LIMIT ?", (query, limit)).fetchall()
    return {r["session_id"]: r["snip"] for r in rows}


# --------------------------------------------------------------- archive

def archive_path(transcript):
    p = Path(transcript)
    return paths.ARCHIVE / p.parent.name / p.name


def archive_transcript(path):
    """Hard-link the transcript into ~/.cairn so Claude Code's cleanup can't delete it.

    A hard link costs no space while the original exists and sees its appends.
    If Claude Code ever replaces the file (new inode), the link is refreshed.
    """
    dest = archive_path(path)
    try:
        ino = path.stat().st_ino
        if dest.exists():
            if dest.stat().st_ino == ino:
                return
            dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(path, dest)
        except OSError:
            shutil.copy2(path, dest)  # a different filesystem: copy instead
    except OSError as e:
        log(f"archive {path}: {e!r}")


def resumable(row):
    return not row["missing"] or archive_path(row["transcript"]).exists()


def restore_transcript(conn, row):
    """Put a cleaned-up transcript back from the archive. Returns True if the session is resumable."""
    dst = Path(row["transcript"])
    if dst.exists():
        return True
    src = archive_path(dst)
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)  # fresh mtime, so the next cleanup pass leaves it alone
    conn.execute("UPDATE sessions SET missing=0 WHERE id=?", (row["id"],))
    conn.commit()
    return True
