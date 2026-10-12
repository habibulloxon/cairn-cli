"""What runs inside Claude Code: the hook handler and the status line.

Both must be fast and must never print errors into a session; failures go to the log.
"""

import json
import os
import subprocess
import time
from contextlib import closing
from pathlib import Path

from cairn import config, index, live, paths, sessions, store, summarize
from cairn.util import ago, clip, epoch, tilde

# Hook event -> the live status it sets. Stop and SessionEnd also re-index and summarize.
HOOK_STATUS = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "PermissionRequest": "waiting",
    "PostToolUse": "running",
    "Stop": "idle",
    "SessionEnd": "closed",
}
DEFAULT_BRANCHES = {"main", "master", "dev", "develop", "development", "staging", "trunk"}


def handle(payload):
    """Handle one hook event. Returns what to print on stdout (JSON for Claude Code), or None."""
    if os.environ.get("CAIRN_INTERNAL"):
        return None  # our own summary calls
    event, sid = payload.get("hook_event_name", ""), payload.get("session_id")
    output = None
    with closing(store.connect()) as conn:
        if sid and event in HOOK_STATUS:
            live.set_status(conn, sid, HOOK_STATUS[event])
        if event == "SessionStart":
            message = nudge(conn, payload)
            if message:
                output = json.dumps({"systemMessage": message})  # shown to you, not added to Claude's context
    path = payload.get("transcript_path")
    if path and event in ("Stop", "SessionEnd"):
        spawn("_update", path, event)  # re-index and summarize without making the session wait
    return output


def spawn(*args):
    """Run `cairn <args>` detached from this process."""
    subprocess.Popen(
        [*paths.self_argv(), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def update(path, event):
    """After a turn: re-index the transcript, and refresh the summary if it's behind."""
    path = Path(path)
    with closing(store.connect()) as conn:
        index.ingest_file(conn, path, force=True)
        conn.commit()
        row = sessions.get(conn, path.stem)
        if summarize.needs_summary(row, final=event == "SessionEnd"):
            summarize.summarize(conn, path.stem)


def nudge(conn, payload):
    """At a fresh session start, point at existing sessions for the same branch or recent work."""
    cfg = config.load()
    if not cfg["nudge"] or payload.get("source") not in (None, "startup"):
        return None
    cwd = payload.get("cwd") or os.getcwd()
    root, branch = sessions.current_scope(cwd), sessions.current_branch(cwd)
    sid = payload.get("session_id")
    rows = [r for r in sessions.query(conn, root=root) if r["id"] != sid and index.resumable(r)]
    feature = branch if branch and branch not in DEFAULT_BRANCHES else None
    same = [r for r in rows if sessions.on_branch(r, feature)]
    cutoff = time.time() - cfg["nudge_hours"] * 3600
    picks = same[:2] or [r for r in rows if epoch(r["last_active"]) >= cutoff][:2]
    if not picks:
        return None

    open_now = live.statuses(conn)
    where = f"branch {feature}" if same else f"{tilde(root)} (last {cfg['nudge_hours']}h)"
    lines = [f"cairn: you already have {'a session' if len(picks) == 1 else 'sessions'} for {where}:"]
    for r in picks:
        note = "  [open in another terminal]" if r["id"] in open_now else ""
        lines.append(f"  • {clip(sessions.display_name(r), 60)} ({ago(r['last_active'])}){note}")
        if r["summary"]:
            lines.append(f"    {clip(r['summary'], 140)}")
        lines.append(f"    resume: cairn {r['id'][:8]}")
    return "\n".join(lines)


def statusline(raw, wrap=None):
    """The status line: the wrapped command's output, then this session's cairn line."""
    lines = []
    if wrap:
        try:
            out = subprocess.run(wrap, shell=True, input=raw, capture_output=True, text=True, timeout=10).stdout
            if out.strip():
                lines.append(out.rstrip("\n"))
        except subprocess.TimeoutExpired:
            pass
    try:
        sid = json.loads(raw).get("session_id")
    except (ValueError, AttributeError):
        sid = None
    if sid:
        with closing(store.connect()) as conn:
            row = sessions.get(conn, sid)
        name = clip(sessions.display_name(row), 50) if row and row["turns"] else "new session"
        ticket = sessions.ticket_of(row) if row else None
        pin = "★ " if row and row["pinned"] else ""
        lines.append(f"\x1b[2m▲ {pin}{name}{f' · {ticket}' if ticket else ''} · {sid[:8]}\x1b[0m")
    return "\n".join(lines)
