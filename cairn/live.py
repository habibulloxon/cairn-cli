"""Which sessions are open right now, from hook events plus a liveness check on the claude PID."""

import os
import subprocess
import time

STATUS_GLYPH = {"running": "▶", "waiting": "!", "idle": "●"}


def claude_pid():
    """PID of the claude process running this hook."""
    if os.environ.get("CLAUDE_PID", "").isdigit():  # set by Claude Code 2.1.214+
        return int(os.environ["CLAUDE_PID"])
    pid = os.getppid()  # older versions: walk up the process tree to it
    for _ in range(6):
        out = _ps(pid, "ppid=,comm=")
        if not out:
            return None
        ppid, comm = out.split(None, 1)
        if os.path.basename(comm.strip()) == "claude":
            return pid
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def _ps(pid, fields):
    try:
        return subprocess.run(["ps", "-o", fields, "-p", str(pid)],
                              capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def is_claude(pid):
    """True if pid is a running claude process (PIDs get reused, so check the name too)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists, owned by someone else
    return os.path.basename(_ps(pid, "comm=")) == "claude"


def set_status(conn, session_id, status):
    row = conn.execute("SELECT status, pid FROM live WHERE session_id=?", (session_id,)).fetchone()
    if row and row["status"] == status and (row["pid"] or status == "closed"):
        return  # PostToolUse fires constantly; skip no-op writes
    pid = row["pid"] if row else None
    if status != "closed" and not is_claude(pid):
        pid = claude_pid()
    conn.execute(
        "INSERT INTO live VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
        "status=excluded.status, pid=excluded.pid, updated_at=excluded.updated_at",
        (session_id, status, pid, time.time()),
    )
    conn.commit()


def statuses(conn):
    """{session_id: status} for sessions whose claude process is still running.

    Checked fresh on every call: the dashboard server is long-lived, and sessions
    that exit without a SessionEnd (closed terminal tab) must stop showing as open.
    """
    rows = conn.execute("SELECT session_id, status, pid FROM live WHERE status != 'closed'").fetchall()
    alive = {pid: is_claude(pid) for pid in {r["pid"] for r in rows}}
    return {r["session_id"]: r["status"] for r in rows if alive[r["pid"]]}
