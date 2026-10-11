#!/usr/bin/env python3
"""cairn: find the right Claude Code session and hop back into it.

Indexes the transcripts Claude Code writes to ~/.claude/projects into a local
SQLite file, keeps it fresh (and tracks which sessions are open) from hooks,
writes a short summary of each session with a background Haiku call, keeps
transcripts resumable after Claude Code's cleanup, and serves a dashboard.

Standard library only.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
SETTINGS = HOME / ".claude" / "settings.json"
DATA = Path(os.environ.get("CAIRN_HOME", HOME / ".cairn"))
DB_PATH = DATA / "cairn.db"
LOG_PATH = DATA / "cairn.log"
LOCK_DIR = DATA / "locks"
ARCHIVE = DATA / "transcripts"  # hard links to transcripts, so cleanup can't take them
CONFIG_PATH = DATA / "config.json"
ROOT = Path(__file__).resolve().parent
# Built dashboard (the cairn-ui repo, cloned into ./ui and built with `npm run build`).
UI_DIST = Path(os.environ.get("CAIRN_UI_DIST", ROOT / "ui" / "dist")).expanduser()
SELF = str(Path(__file__).resolve())

DEFAULT_PORT = 7444
INTERNAL_ENV = "CAIRN_INTERNAL"  # set for our own `claude -p` so its hooks no-op

# Hook event -> live status it sets. Stop/SessionEnd also re-index and summarize.
HOOK_STATUS = {
    "SessionStart": "idle",
    "UserPromptSubmit": "running",
    "PermissionRequest": "waiting",
    "PostToolUse": "running",
    "Stop": "idle",
    "SessionEnd": "closed",
}
DEFAULT_BRANCHES = {"main", "master", "dev", "develop", "development", "staging", "trunk"}

DEFAULT_CONFIG = {
    "ticket_url": "",      # e.g. https://linear.app/acme/issue/{ticket}
    "nudge": True,         # remind at session start when a session for this work exists
    "nudge_hours": 24,     # ...counting sessions active within this many hours (same branch: any age)
}

SUMMARY_MODEL = os.environ.get("CAIRN_MODEL", "haiku")
SUMMARIZE_EVERY_TURNS = 3  # on Stop, re-summarize after this many new prompts
SUMMARIZE_MIN_GAP = 10 * 60  # ...or after this many seconds with any new prompt
EXCERPT_CHARS = 14000

SUMMARY_SYSTEM = (
    "You write one-line summaries of Claude Code sessions for a session picker. "
    "Given an excerpt of a coding session, reply with ONLY the summary: one or two "
    "plain sentences, at most 220 characters, past tense, concrete (name the feature, "
    "bug, file or command). Say what was done and where it stands, e.g. "
    "'Added CSV export to the reports API; tests pass, frontend button still TODO.' "
    "No preamble, no quotes, no markdown."
)
BRANCH_INSTRUCTIONS = (
    "\n\nThe session's git branch wasn't recorded (it ran in a folder that holds several "
    "repos, or outside one). From the git activity below, also identify the branch the "
    "session's work was done on: the feature branch that was checked out, committed or "
    "pushed, not main/master/dev unless the work really was committed there. Use null if "
    "the evidence doesn't make it clear; never guess a name that isn't in the evidence. "
    "If the work was in a sub-repository, give its directory name as repo.\n"
    "Reply with ONLY this JSON instead: "
    '{"summary": "...", "branch": "..." or null, "repo": "..." or null}'
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id            TEXT PRIMARY KEY,
  transcript    TEXT,
  cwd           TEXT,
  branch        TEXT,
  branches      TEXT,      -- JSON list, in order first seen
  custom_title  TEXT,
  ai_title      TEXT,
  first_prompt  TEXT,
  last_prompt   TEXT,
  turns         INTEGER DEFAULT 0,
  started_at    TEXT,
  last_active   TEXT,
  pr_url        TEXT,
  summary       TEXT,
  summary_turns INTEGER DEFAULT 0,
  summary_at    REAL,
  file_size     INTEGER,
  file_mtime    REAL,
  missing       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_last_active ON sessions(last_active);
CREATE TABLE IF NOT EXISTS live (
  session_id TEXT PRIMARY KEY,
  status     TEXT,         -- running | waiting | idle | closed
  pid        INTEGER,      -- the claude process, to notice sessions that died without SessionEnd
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  session_id UNINDEXED, body, tokenize="unicode61 remove_diacritics 2"
);
"""
# Columns added after the first release; created on existing databases too.
EXTRA_COLUMNS = (
    ("inferred_branch", "TEXT"),     # guessed by Haiku from git commands when branch is empty
    ("inferred_repo", "TEXT"),
    ("branch_inferred_at", "REAL"),
    ("pinned", "INTEGER DEFAULT 0"),
    ("archived_at", "REAL"),
)
FTS_VERSION = "1"  # bump to rebuild the search index (and re-link transcripts) on next sync


# ---------------------------------------------------------------- utilities

def log(msg):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def db():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    for col, typ in EXTRA_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
    if "next_step" in have:  # dropped feature
        conn.execute("ALTER TABLE sessions DROP COLUMN next_step")
    return conn


def config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def tilde(path):
    if path and path.startswith(str(HOME)):
        return "~" + path[len(str(HOME)):]
    return path or ""


def resume_command(row):
    cmd = f"claude --resume {row['id']}"
    if row["cwd"]:
        cmd = f"cd {shlex.quote(row['cwd'])} && {cmd}"
    return cmd


def display_name(row):
    return row["custom_title"] or row["ai_title"] or clip(row["first_prompt"], 70) or "(untitled)"


def clip(text, n):
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


def ago(iso):
    if not iso:
        return ""
    s = time.time() - epoch(iso)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit} ago"
    return "just now"


def git(cwd, *args):
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------- transcript parse

PASTED_RE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
NOISE_PREFIXES = (
    "<local-command", "<command-name", "<command-message", "<system-reminder",
    "<task-notification", "Caveat:", "[Request interrupted",
)


def prompt_text(rec):
    """The human-typed text of a user record, or None for tool results / meta."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if not parts:
            return None  # tool_result-only turn
        content = "\n".join(parts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return None
    text = PASTED_RE.sub("[pasted]", text)
    return text.strip() or None


def parse_transcript(path):
    """The session row, plus its dialogue as (who, text) with who in User/Claude/Cmd."""
    info = {
        "id": path.stem, "transcript": str(path), "cwd": None, "branch": None,
        "branches": [], "custom_title": None, "ai_title": None,
        "first_prompt": None, "last_prompt": None, "turns": 0,
        "started_at": None, "last_active": None, "pr_url": None,
    }
    dialogue = []
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("type")
            if kind == "custom-title":
                info["custom_title"] = rec.get("customTitle") or info["custom_title"]
                continue
            if kind == "ai-title":
                info["ai_title"] = rec.get("aiTitle") or info["ai_title"]
                continue
            if kind == "pr-link":
                info["pr_url"] = rec.get("prUrl") or info["pr_url"]
                continue
            if kind not in ("user", "assistant") or rec.get("isSidechain"):
                continue

            # The launch directory, not the latest one: Claude Code files the
            # transcript under it, so `claude --resume` only finds it from there.
            if rec.get("cwd") and not info["cwd"]:
                info["cwd"] = rec["cwd"]
            branch = rec.get("gitBranch")
            if branch and branch != "HEAD":
                info["branch"] = branch
                if branch not in info["branches"]:
                    info["branches"].append(branch)
            ts = rec.get("timestamp")
            if ts:
                info["started_at"] = info["started_at"] or ts
                info["last_active"] = ts

            if kind == "user":
                text = prompt_text(rec)
                if text:
                    info["turns"] += 1
                    info["first_prompt"] = info["first_prompt"] or text[:2000]
                    info["last_prompt"] = text[:2000]
                    dialogue.append(("User", text))
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    dialogue.append(("Claude", c["text"].strip()))
                elif c.get("type") == "tool_use" and c.get("name") == "Bash":
                    cmd = str((c.get("input") or {}).get("command", "")).strip()
                    if cmd:
                        dialogue.append(("Cmd", cmd))
    return info, dialogue


# ------------------------------------------------------------------ ingest

def upsert(conn, info, size, mtime):
    info = dict(info, branches=json.dumps(info["branches"]), file_size=size, file_mtime=mtime, missing=0)
    cols = list(info)
    conn.execute(
        f"INSERT INTO sessions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in cols if c != 'id')}",
        [info[c] for c in cols],
    )


def index_text(conn, session_id, dialogue):
    conn.execute("DELETE FROM fts WHERE session_id=?", (session_id,))
    body = "\n".join(f"{'$ ' if who == 'Cmd' else ''}{text}" for who, text in dialogue)
    if body:
        conn.execute("INSERT INTO fts (session_id, body) VALUES (?, ?)", (session_id, body))


def archive_path(transcript):
    p = Path(transcript)
    return ARCHIVE / p.parent.name / p.name


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
            shutil.copy2(path, dest)  # different filesystem
    except OSError as e:
        log(f"archive {path}: {e!r}")


def restore_transcript(conn, row):
    """Put a cleaned-up transcript back from the archive. True if it's resumable."""
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


def resumable(row):
    return not row["missing"] or archive_path(row["transcript"]).exists()


def ingest_file(conn, path, force=False):
    try:
        st = path.stat()
    except OSError:
        return False
    if not force:
        row = conn.execute("SELECT file_size, file_mtime FROM sessions WHERE id=?", (path.stem,)).fetchone()
        if row and row["file_size"] == st.st_size and row["file_mtime"] == st.st_mtime:
            return False
    info, dialogue = parse_transcript(path)
    upsert(conn, info, st.st_size, st.st_mtime)
    index_text(conn, info["id"], dialogue)
    archive_transcript(path)
    return True


def sync(conn):
    """Ingest every changed transcript and flag sessions whose file is gone."""
    built = conn.execute("SELECT value FROM meta WHERE key='fts_version'").fetchone()
    force = not built or built["value"] != FTS_VERSION
    seen, changed = set(), 0
    for path in PROJECTS.glob("*/*.jsonl"):
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


# -------------------------------------------------------------- live status

def claude_pid():
    """PID of the claude process running this hook."""
    if os.environ.get("CLAUDE_PID", "").isdigit():  # set by Claude Code 2.1.214+
        return int(os.environ["CLAUDE_PID"])
    pid = os.getppid()  # older versions: walk up to the claude process
    for _ in range(6):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        if not out:
            return None
        ppid, comm = out.split(None, 1)
        if os.path.basename(comm.strip()) == "claude":
            return pid
        pid = int(ppid)
        if pid <= 1:
            return None
    return None


def pid_alive(pid, _cache={}):
    if not pid:
        return False
    if pid not in _cache:
        try:
            os.kill(pid, 0)
            comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=2).stdout.strip()
            _cache[pid] = os.path.basename(comm) == "claude"
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            _cache[pid] = False
    return _cache[pid]


def set_status(conn, session_id, status):
    row = conn.execute("SELECT status, pid FROM live WHERE session_id=?", (session_id,)).fetchone()
    if row and row["status"] == status and (row["pid"] or status == "closed"):
        return  # PostToolUse fires constantly; skip no-op writes
    pid = row["pid"] if row and row["pid"] else None
    if status != "closed" and (not pid or not pid_alive(pid)):
        pid = claude_pid()
    conn.execute(
        "INSERT INTO live VALUES (?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
        "status=excluded.status, pid=excluded.pid, updated_at=excluded.updated_at",
        (session_id, status, pid, time.time()),
    )
    conn.commit()


def live_statuses(conn):
    """{session_id: status} for sessions whose claude process is still running."""
    out = {}
    for r in conn.execute("SELECT * FROM live WHERE status != 'closed'"):
        if pid_alive(r["pid"]):
            out[r["session_id"]] = r["status"]
    return out


STATUS_GLYPH = {"running": "▶", "waiting": "!", "idle": "●"}


# ------------------------------------------------------------ ticket, scope

TICKET_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})-(\d{1,6})(?![0-9])")
TICKET_UPPER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,10})-(\d{1,6})(?![0-9])")
NOT_TICKETS = {"UTF", "SHA", "ISO", "HTTP", "HTTPS", "RFC", "TLS", "SSL", "AES", "MD", "GPT", "IPV",
               "WCAG", "ES", "CVE", "PEP", "TS", "STEP", "PHASE", "PART", "ITEM", "FIX", "TOP", "DAY",
               "WEEK", "SPRINT", "VERSION", "OPUS", "HAIKU", "SONNET", "CLAUDE", "TEST", "TESTS", "PR"}


def ticket_of(row):
    """A tracker ID like DEV-1157 from the branch, title or first prompt."""
    branches = json.loads(row["branches"] or "[]")
    candidates = [(row["branch"], TICKET_RE), (row["inferred_branch"], TICKET_RE),
                  (row["custom_title"], TICKET_RE)]
    candidates += [(b, TICKET_RE) for b in reversed(branches)]
    candidates += [(row["ai_title"], TICKET_UPPER_RE), (row["first_prompt"], TICKET_UPPER_RE)]
    for text, rx in candidates:
        for m in rx.finditer(text or ""):
            prefix = m.group(1).upper()
            if prefix not in NOT_TICKETS:
                return f"{prefix}-{m.group(2)}"
    return None


def in_dir(row, root, exact=False):
    cwd = row["cwd"] or ""
    if cwd == root or (not exact and cwd.startswith(root.rstrip("/") + "/")):
        return True
    # A session started in a multi-repo folder whose work was in this repo.
    return bool(row["inferred_repo"]) and os.path.join(cwd, row["inferred_repo"]) == root


def on_branch(row, branch):
    return bool(branch) and branch in (row["branch"], row["inferred_branch"])


def is_archived(row):
    # New activity after archiving brings a session back.
    return bool(row["archived_at"]) and epoch(row["last_active"]) <= row["archived_at"]


def current_scope(cwd=None):
    """The project you're standing in: the git repo root if inside one, else the cwd."""
    cwd = cwd or os.getcwd()
    return git(cwd, "rev-parse", "--show-toplevel") or cwd


def current_branch(cwd=None):
    return git(cwd or os.getcwd(), "branch", "--show-current") or None


def resolve_scope(args):
    """Directory to filter by, or None for every session (-a)."""
    if getattr(args, "all", False):
        return None
    if getattr(args, "dir", None):
        return str(Path(args.dir).expanduser().resolve())
    return current_scope()


def query_sessions(conn, text=None, include_empty=False, limit=None, root=None, exact=False,
                   archived=False, ids=None):
    rows = conn.execute("SELECT * FROM sessions ORDER BY last_active DESC").fetchall()
    out = []
    for r in rows:
        if not include_empty and not r["turns"]:
            continue
        if is_archived(r) != archived:
            continue
        if ids is not None and r["id"] not in ids:
            continue
        if root and not in_dir(r, root, exact):
            continue
        if text:
            hay = " ".join(str(r[k] or "") for k in (
                "id", "custom_title", "ai_title", "summary", "cwd", "branches",
                "inferred_branch", "inferred_repo", "first_prompt", "last_prompt")).lower()
            if not all(word in hay for word in text.lower().split()):
                continue
        out.append(r)
        if limit and len(out) >= limit:
            break
    return out


def ranked(rows, branch):
    """Pinned first, then sessions on the current branch, then by recency (input order)."""
    return sorted(rows, key=lambda r: (not r["pinned"], not on_branch(r, branch)))


def fts_query(text):
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words)


def search_ids(conn, text, limit=200):
    """{session_id: snippet} for sessions whose transcript matches, best first."""
    q = fts_query(text)
    if not q:
        return {}
    rows = conn.execute(
        "SELECT session_id, snippet(fts, 1, '\x02', '\x03', '…', 14) AS snip FROM fts "
        "WHERE fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    return {r["session_id"]: r["snip"] for r in rows}


def resolve_id(conn, prefix):
    rows = conn.execute("SELECT id FROM sessions WHERE id LIKE ?", (prefix + "%",)).fetchall()
    if len(rows) != 1:
        sys.exit(f"{'No' if not rows else 'More than one'} session matches '{prefix}'.")
    return rows[0]["id"]


# --------------------------------------------------------------- summarize

def build_excerpt(dialogue, previous):
    """First prompt plus as much of the most recent dialogue as fits."""
    talk = [(w, t) for w, t in dialogue if w != "Cmd"]
    first = next((t for w, t in talk if w == "User"), "")
    tail, used = [], 0
    for who, text in reversed(talk):
        line = f"{who}: {clip(text, 1500 if who == 'User' else 900)}"
        if used + len(line) > EXCERPT_CHARS:
            break
        tail.append(line)
        used += len(line)
    tail.reverse()
    parts = []
    if previous:
        parts.append(f"Previous summary (update it): {previous}")
    parts.append(f"Session started with: {clip(first, 1200)}")
    parts.append("Most recent part of the session:\n" + "\n\n".join(tail))
    return "\n\n".join(parts)


GIT_CMD_RE = re.compile(r"\bgit\b.*\b(checkout|switch|push|worktree|branch|merge|rebase|pull|commit)\b")
GIT_OUT_RE = re.compile(
    r"^(On branch \S+|Switched to (a new )?branch .+|Your branch .+|\[[\w./-]+ [0-9a-f]{7,}\] .*"
    r"|.*\[new branch\].*|.*\b[\w./-]+ -> [\w./-]+$|branch '.+' set up to track.*)"
)


def git_evidence(path, max_lines=50):
    """Git commands and their telling output lines, most recent last, as one text block."""
    lines = []
    with open(path, "rb") as f:
        for raw in f:
            if b"git" not in raw and b"branch" not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            content = (rec.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use" and c.get("name") == "Bash":
                    cmd = str((c.get("input") or {}).get("command", ""))
                    if GIT_CMD_RE.search(cmd):
                        lines.append("$ " + clip(cmd, 300))
                elif c.get("type") == "tool_result":
                    out = c.get("content")
                    if isinstance(out, list):
                        out = "\n".join(x.get("text", "") for x in out if isinstance(x, dict))
                    for line in str(out or "").splitlines():
                        line = line.strip()
                        if len(line) < 200 and GIT_OUT_RE.match(line):
                            lines.append("  " + line)
    seen, unique = set(), []
    for line in reversed(lines):  # keep the most recent occurrence of each line
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return "\n".join(reversed(unique[:max_lines]))


def parse_reply(text, evidence):
    """{summary, branch, repo} from Haiku's reply; branch/repo only if found in the evidence."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"summary": text.strip(), "branch": None, "repo": None}

    def field(key):
        v = data.get(key)
        return str(v).strip() if v not in (None, "", "null") else None

    out = {"summary": field("summary") or "", "branch": None, "repo": None}
    branch, repo = field("branch"), field("repo")
    if branch and evidence:
        branch = branch.removeprefix("origin/")
        # Only keep it if it's really in the transcript, so Haiku can't invent one.
        if re.search(rf"(?<![\w./-]){re.escape(branch)}(?![\w/-])", evidence):
            out["branch"] = branch
            if repo and repo.strip("/") in evidence:
                out["repo"] = repo.strip("/")
    return out


def acquire_lock(session_id):
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_DIR / f"{session_id}.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > 300:
            lock.unlink()  # stale: a summarizer died
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return lock
    except FileExistsError:
        return None


def summarize(conn, session_id, fresh=False):
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or row["missing"] or not row["turns"]:
        return None
    claude = shutil.which("claude")
    if not claude:
        log("summarize: claude not on PATH")
        return None
    lock = acquire_lock(session_id)
    if not lock:
        return None
    try:
        info, dialogue = parse_transcript(Path(row["transcript"]))
        prompt = build_excerpt(dialogue, None if fresh else row["summary"])
        system = SUMMARY_SYSTEM
        evidence = None if info["branch"] else git_evidence(Path(row["transcript"]))
        if evidence:
            system += BRANCH_INSTRUCTIONS
            prompt += "\n\nGit activity in this session (commands and output):\n" + evidence
        DATA.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                claude, "-p", "--model", SUMMARY_MODEL,
                "--no-session-persistence", "--tools", "",
                "--strict-mcp-config", "--settings", '{"disableAllHooks": true}',
                "--system-prompt", system, "--effort", "low",
            ],
            input=prompt, capture_output=True, text=True, timeout=180,
            # No extended thinking: with a high global effortLevel Haiku spent ~4000 thinking
            # tokens (30s) on a 60-word summary; without it the call takes ~4s.
            cwd=DATA, env=dict(os.environ, **{INTERNAL_ENV: "1", "MAX_THINKING_TOKENS": "0"}),
        )
        reply = parse_reply(proc.stdout.strip(), evidence)
        summary = clip(reply["summary"].strip('"'), 300)
        if proc.returncode != 0 or not summary:
            log(f"summarize {session_id}: rc={proc.returncode} {proc.stderr.strip()[:300]}")
            return None
        now = time.time()
        conn.execute(
            "UPDATE sessions SET summary=?, summary_turns=?, summary_at=? WHERE id=?",
            (summary, info["turns"], now, session_id),
        )
        if not info["branch"]:
            # Keep an earlier guess if this pass found nothing new.
            conn.execute(
                "UPDATE sessions SET inferred_branch=COALESCE(?, inferred_branch), "
                "inferred_repo=CASE WHEN ? IS NULL THEN inferred_repo ELSE ? END, "
                "branch_inferred_at=? WHERE id=?",
                (reply["branch"], reply["branch"], reply["repo"], now, session_id),
            )
        conn.commit()
        return summary
    except subprocess.TimeoutExpired:
        log(f"summarize {session_id}: timed out")
        return None
    finally:
        lock.unlink(missing_ok=True)


def needs_branch(row):
    """No recorded branch and never asked Haiku to infer one."""
    return bool(row and not row["missing"] and row["turns"] and not row["branch"] and row["branch_inferred_at"] is None)


def shown_branch(row):
    if row["branch"]:
        return row["branch"]
    if row["inferred_branch"]:
        return f"≈{row['inferred_branch']}"
    return None


def needs_summary(row, final):
    if not row or row["missing"] or not row["turns"]:
        return False
    new_turns = row["turns"] - (row["summary_turns"] or 0)
    if new_turns <= 0:
        return False
    if final or not row["summary"]:
        return True
    return new_turns >= SUMMARIZE_EVERY_TURNS or time.time() - (row["summary_at"] or 0) >= SUMMARIZE_MIN_GAP


# ------------------------------------------------------------------ hooks

def nudge(conn, payload):
    """At session start, point at an existing session for the same branch or recent work."""
    cfg = config()
    if not cfg["nudge"] or payload.get("source") not in (None, "startup"):
        return None
    cwd = payload.get("cwd") or os.getcwd()
    root, branch = current_scope(cwd), current_branch(cwd)
    sid = payload.get("session_id")
    rows = [r for r in query_sessions(conn, root=root) if r["id"] != sid and resumable(r)]
    if not rows:
        return None
    feature = branch if branch and branch not in DEFAULT_BRANCHES else None
    same = [r for r in rows if on_branch(r, feature)]
    cutoff = time.time() - cfg["nudge_hours"] * 3600
    picks = same[:2] or [r for r in rows if epoch(r["last_active"]) >= cutoff][:2]
    if not picks:
        return None
    live = live_statuses(conn)
    where = f"branch {feature}" if same else f"{tilde(root)} (last {cfg['nudge_hours']}h)"
    lines = [f"cairn: you already have {'a session' if len(picks) == 1 else 'sessions'} for {where}:"]
    for r in picks:
        open_note = "  [open in another terminal]" if r["id"] in live else ""
        lines.append(f"  • {clip(display_name(r), 60)} ({ago(r['last_active'])}){open_note}")
        if r["summary"]:
            lines.append(f"    {clip(r['summary'], 140)}")
        lines.append(f"    resume: cairn {r['id'][:8]}")
    return "\n".join(lines)


def cmd_hook(args):
    """Hook entry point: record live status, nudge on start, re-index on Stop/SessionEnd."""
    if os.environ.get(INTERNAL_ENV):
        return
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    event, sid = payload.get("hook_event_name", ""), payload.get("session_id")
    conn = db()
    if sid and event in HOOK_STATUS:
        set_status(conn, sid, HOOK_STATUS[event])
    if event == "SessionStart":
        message = nudge(conn, payload)
        if message:
            print(json.dumps({"systemMessage": message}))
    path = payload.get("transcript_path")
    if path and event in ("Stop", "SessionEnd"):
        subprocess.Popen(
            [sys.executable, SELF, "_update", path, event],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def cmd_update(args):
    path = Path(args.path)
    conn = db()
    ingest_file(conn, path, force=True)
    conn.commit()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (path.stem,)).fetchone()
    if needs_summary(row, final=args.event == "SessionEnd"):
        summarize(conn, path.stem)


def cmd_statusline(args):
    """Status line: the wrapped command's output, then this session's cairn line."""
    raw = sys.stdin.read()
    if args.wrap:
        try:
            out = subprocess.run(args.wrap, shell=True, input=raw, capture_output=True, text=True, timeout=10).stdout
            if out.strip():
                print(out.rstrip("\n"))
        except subprocess.TimeoutExpired:
            pass
    try:
        sid = json.loads(raw).get("session_id")
    except ValueError:
        return
    if not sid:
        return
    row = db().execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    name = clip(display_name(row), 50) if row and row["turns"] else "new session"
    ticket = ticket_of(row) if row else None
    pin = "★ " if row and row["pinned"] else ""
    print(f"\x1b[2m▲ {pin}{name}{f' · {ticket}' if ticket else ''} · {sid[:8]}\x1b[0m")


# ---------------------------------------------------------------- install

def hook_command():
    return f"{shlex.quote(sys.executable)} {shlex.quote(SELF)} hook"


def statusline_command(wrap=None):
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(SELF)} statusline"
    return f"{cmd} --wrap {shlex.quote(wrap)}" if wrap else cmd


def is_cairn_hook(h):
    return isinstance(h, dict) and "cairn.py" in h.get("command", "") and h.get("command", "").endswith(" hook")


def unwrap_statusline(cmd):
    """The original status line command if `cmd` is cairn's wrapper, else None."""
    if "cairn.py" not in cmd or " statusline" not in cmd:
        return None
    parts = shlex.split(cmd)
    return parts[parts.index("--wrap") + 1] if "--wrap" in parts else ""


def write_settings(settings):
    backup = SETTINGS.with_name(f"settings.json.cairn-backup-{int(time.time())}")
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, backup)
        print(f"Backup: {backup}")
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def strip_hooks(hooks):
    for event in list(hooks):
        for g in hooks[event]:
            g["hooks"] = [h for h in g.get("hooks", []) if not is_cairn_hook(h)]
        hooks[event] = [g for g in hooks[event] if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]


def cmd_install(args):
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = settings.setdefault("hooks", {})
    strip_hooks(hooks)
    for event in HOOK_STATUS:
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": hook_command(), "timeout": 5}]})
    if not args.no_statusline:
        current = (settings.get("statusLine") or {}).get("command", "")
        original = unwrap_statusline(current)
        wrap = original if original is not None else current
        settings["statusLine"] = {**(settings.get("statusLine") or {"type": "command", "padding": 0}),
                                  "type": "command", "command": statusline_command(wrap or None)}
    write_settings(settings)
    print(f"Added cairn hooks ({', '.join(HOOK_STATUS)}) to {SETTINGS}")
    if not args.no_statusline:
        print("Status line: cairn line added under your existing one")
    conn = db()
    print(f"Indexed {sync(conn)} sessions into {DB_PATH}")


def cmd_uninstall(args):
    if not SETTINGS.exists():
        return
    settings = json.loads(SETTINGS.read_text())
    strip_hooks(settings.get("hooks", {}))
    original = unwrap_statusline((settings.get("statusLine") or {}).get("command", ""))
    if original:
        settings["statusLine"]["command"] = original
    elif original == "":
        settings.pop("statusLine", None)
    write_settings(settings)
    print(f"Removed cairn hooks and status line from {SETTINGS} (data kept in {DATA})")


# -------------------------------------------------------------- terminal

GHOSTTY_SCRIPT = """
on run argv
  set theDir to item 1 of argv
  set theInput to item 2 of argv
  tell application "Ghostty"
    activate
    set cfg to new surface configuration
    set initial working directory of cfg to theDir
    set initial input of cfg to theInput & linefeed
    if (count of windows) is 0 then
      new window with configuration cfg
    else
      new tab in front window with configuration cfg
    end if
  end tell
end run
"""


def open_in_ghostty(cwd, command):
    """Open a new Ghostty tab in `cwd` and type `command` into its shell."""
    proc = subprocess.run(["osascript", "-e", GHOSTTY_SCRIPT, cwd, command],
                          capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")


def prepare_resume(conn, row):
    """Check a session can be resumed (restoring its transcript if needed). Returns cwd."""
    if not restore_transcript(conn, row):
        raise RuntimeError("Its transcript was deleted by Claude Code's cleanup before cairn archived it.")
    cwd = row["cwd"] or str(HOME)
    if not os.path.isdir(cwd):
        raise RuntimeError(f"Directory {cwd} no longer exists.")
    return cwd


# --------------------------------------------------------------- commands

def row_json(r, live=None, ticket_url=""):
    ticket = ticket_of(r)
    return {
        "id": r["id"],
        "name": display_name(r),
        "custom_title": r["custom_title"],
        "ai_title": r["ai_title"],
        "summary": r["summary"],
        "first_prompt": clip(r["first_prompt"], 300),
        "last_prompt": clip(r["last_prompt"], 300),
        "cwd": r["cwd"],
        "dir": tilde(r["cwd"]),
        "branch": r["branch"],
        "branches": json.loads(r["branches"] or "[]"),
        "inferred_branch": r["inferred_branch"],
        "inferred_repo": r["inferred_repo"],
        "ticket": ticket,
        "ticket_url": ticket_url.replace("{ticket}", ticket) if ticket and ticket_url else None,
        "turns": r["turns"],
        "started_at": r["started_at"],
        "last_active": r["last_active"],
        "pr_url": r["pr_url"],
        "missing": bool(r["missing"]),
        "resumable": resumable(r),
        "pinned": bool(r["pinned"]),
        "archived": is_archived(r),
        "status": (live or {}).get(r["id"]),
        "resume": resume_command(r),
    }


def scoped(conn, args, text=None, include_empty=False, limit=None):
    """Sessions in the current scope; falls back to all sessions if the scope has none."""
    root = resolve_scope(args)
    archived = getattr(args, "archived", False)
    ids = search_ids(conn, text) if getattr(args, "deep", False) and text else None
    text = None if ids is not None else text
    rows = query_sessions(conn, text, include_empty, None, root=root, archived=archived, ids=ids)
    if root and not rows and not getattr(args, "dir", None):
        print(f"No sessions in {tilde(root)}; showing all. (-d DIR to pick another directory)", file=sys.stderr)
        rows, root = query_sessions(conn, text, include_empty, None, archived=archived, ids=ids), None
    if ids is not None:  # transcript search: best match first
        order = {sid: i for i, sid in enumerate(ids)}
        rows.sort(key=lambda r: order[r["id"]])
    else:
        rows = ranked(rows, current_branch() if root else None)
    return (rows[:limit] if limit else rows), root, ids


def flags(r, live, branch):
    return ("★" if r["pinned"] else " ") + STATUS_GLYPH.get(live.get(r["id"]), " ") + \
           ("⎇" if on_branch(r, branch) else " ")


def cmd_sync(args):
    conn = db()
    print(f"{sync(conn)} sessions updated")


def cmd_ls(args):
    conn = db()
    sync(conn)
    rows, root, hits = scoped(conn, args, " ".join(args.query), args.empty, args.limit)
    live = live_statuses(conn)
    if args.json:
        print(json.dumps([row_json(r, live, config()["ticket_url"]) for r in rows], indent=2, ensure_ascii=False))
        return
    branch = current_branch() if root else None

    def show(r):
        print(f"{flags(r, live, branch)} {r['id'][:8]}  {ago(r['last_active']):>8}  "
              f"{clip(display_name(r), 46):<46}  {clip(shown_branch(r) or '-', 22):<22}  {tilde(r['cwd'])}")
        if hits:
            print(f"{'':14}{clip(hits[r['id']].replace(chr(2), '').replace(chr(3), ''), 120)}")
        elif r["summary"]:
            print(f"{'':14}{clip(r['summary'], 120)}")

    if args.tickets:
        groups = {}
        for r in rows:
            groups.setdefault(ticket_of(r) or "(no ticket)", []).append(r)
        for ticket, rs in groups.items():
            print(f"\n{ticket}  ({len(rs)} session{'s' * (len(rs) != 1)})")
            for r in rs:
                show(r)
    else:
        for r in rows:
            show(r)
    where = f" in {tilde(root)}" if root else ""
    print(f"\n{len(rows)} session{'s' * (len(rows) != 1)}{where} · ★ pinned  ▶ running  ! waiting  "
          f"● open  ⎇ current branch · -a all dirs", file=sys.stderr)


def pick(rows, root, hits, live):
    """Let the user choose one session (fzf if available)."""
    if len(rows) == 1:
        return rows[0]
    branch = current_branch() if root else None
    if shutil.which("fzf") and sys.stdin.isatty():
        def preview(r):
            if hits:
                return hits[r["id"]].replace("\x02", "»").replace("\x03", "«")
            return r["summary"] or clip(r["last_prompt"], 200)
        lines = [
            f"{r['id']}\t{flags(r, live, branch)} {ago(r['last_active']):>8}  {clip(display_name(r), 44):<44}  "
            f"{clip(shown_branch(r) or '-', 20):<20}  {tilde(r['cwd'])}\t{preview(r)}"
            for r in rows
        ]
        proc = subprocess.run(
            ["fzf", "--delimiter=\t", "--with-nth=2", "--no-sort", "--height=60%", "--reverse",
             f"--prompt={tilde(root) + ' ' if root else ''}session> ",
             "--preview=echo {3} | fold -s -w $FZF_PREVIEW_COLUMNS", "--preview-window=down,4,wrap",
             "--header=★ pinned  ▶ running  ! waiting  ● open  ⎇ current branch"],
            input="\n".join(lines), capture_output=True, text=True,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return next(r for r in rows if r["id"] == proc.stdout.split("\t", 1)[0])
    for i, r in enumerate(rows[:30], 1):
        print(f"{i:>2}. {flags(r, live, branch)} {ago(r['last_active']):>8}  {clip(display_name(r), 48):<48}  {tilde(r['cwd'])}")
    try:
        choice = input("number> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not choice.isdigit() or not 1 <= int(choice) <= min(30, len(rows)):
        return None
    return rows[int(choice) - 1]


def confirm_if_open(row, live):
    """Opening a session that's already running elsewhere means two processes writing one transcript."""
    status = live.get(row["id"])
    if not status:
        return True
    msg = f"'{display_name(row)}' is already open in another terminal ({status})."
    if not sys.stdin.isatty():
        print(msg + " Not opening a second copy.", file=sys.stderr)
        return False
    try:
        return input(f"{msg}\nTwo copies would both write to the same transcript. Open anyway? [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_go(args):
    """Pick a session and resume it in its own directory (or a new Ghostty tab)."""
    conn = db()
    sync(conn)
    rows, root, hits = scoped(conn, args, " ".join(args.query))
    if not rows:
        sys.exit("No matching sessions.")
    live = live_statuses(conn)
    chosen = pick(rows, root, hits, live)
    if not chosen:
        return
    if args.print:
        print(resume_command(chosen))
        return
    if not confirm_if_open(chosen, live):
        return
    try:
        cwd = prepare_resume(conn, chosen)
    except RuntimeError as e:
        sys.exit(f"Can't resume '{display_name(chosen)}': {e}")
    if chosen["missing"]:
        print("Restored the transcript from cairn's archive.", file=sys.stderr)
    if args.ghostty:
        open_in_ghostty(cwd, f"claude --resume {chosen['id']}")
        print(f"Opened '{display_name(chosen)}' in a new Ghostty tab.", file=sys.stderr)
        return
    os.chdir(cwd)
    print(f"→ {display_name(chosen)}  ({tilde(cwd)})", file=sys.stderr)
    os.execvp("claude", ["claude", "--resume", chosen["id"]])


def cmd_search(args):
    args.deep = True
    if args.list:
        args.limit, args.empty, args.json, args.tickets = 30, False, False, False
        return cmd_ls(args)
    args.print = False
    return cmd_go(args)


def cmd_mark(args):
    conn = db()
    for prefix in args.ids:
        sid = resolve_id(conn, prefix)
        if args.cmd in ("pin", "unpin"):
            conn.execute("UPDATE sessions SET pinned=? WHERE id=?", (int(args.cmd == "pin"), sid))
        else:
            conn.execute("UPDATE sessions SET archived_at=? WHERE id=?",
                         (time.time() if args.cmd == "archive" else None, sid))
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        print(f"{args.cmd}: {display_name(row)}")
    conn.commit()


def cmd_summarize(args):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = db()
    sync(conn)
    if args.ids:
        rows = [conn.execute("SELECT * FROM sessions WHERE id=?", (resolve_id(conn, i),)).fetchone() for i in args.ids]
        force = True
    else:
        rows = query_sessions(conn) + query_sessions(conn, archived=True)
        if args.dir:
            root = str(Path(args.dir).expanduser().resolve())
            rows = [r for r in rows if in_dir(r, root, args.exact)]
        force = args.force
        if not force:
            # Skip sessions whose summary already covers every prompt.
            rows = [r for r in rows if needs_summary(r, final=True) or needs_branch(r)]
        if args.missing:
            rows = [r for r in rows if not r["summary"]]
    rows = [r for r in rows if not r["missing"] and r["turns"]]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("Nothing to summarize.")
        return
    print(f"Summarizing {len(rows)} session{'s' * (len(rows) != 1)} with {SUMMARY_MODEL} ({args.jobs} at a time)…")

    def work(row):
        return row, summarize(db(), row["id"], fresh=force)  # own connection per thread

    done = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for fut in as_completed([pool.submit(work, r) for r in rows]):
            row, summary = fut.result()
            done += 1
            failed += not summary
            print(f"[{done}/{len(rows)}] {clip(display_name(row), 50)}  ({tilde(row['cwd'])})\n"
                  f"    {summary or '(failed or already running; see ~/.cairn/cairn.log)'}", flush=True)
    print(f"Done: {done - failed} summarized, {failed} failed.")


def cmd_config(args):
    cfg = config()
    if not args.key:
        print(json.dumps(cfg, indent=2))
        return
    if args.key not in DEFAULT_CONFIG:
        sys.exit(f"Unknown key {args.key}. Keys: {', '.join(DEFAULT_CONFIG)}")
    if args.value is None:
        print(cfg[args.key])
        return
    try:
        value = json.loads(args.value)  # true / 24 / "text"
    except ValueError:
        value = args.value
    stored = {}
    try:
        stored = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        pass
    stored[args.key] = value
    DATA.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(stored, indent=2) + "\n")
    print(f"{args.key} = {json.dumps(value)}")


# --------------------------------------------------------------- dashboard

def make_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_static(self, path):
            import mimetypes
            index = UI_DIST / "index.html"
            if not index.exists():
                return self.send(503, "Dashboard not built. Run: cd ui && npm install && npm run build",
                                 "text/plain; charset=utf-8")
            target = (UI_DIST / path.lstrip("/")).resolve()
            if path in ("", "/") or not target.is_file() or UI_DIST.resolve() not in target.parents:
                target = index  # the app handles its own routes
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
                ctype += "; charset=utf-8"
            self.send(200, target.read_bytes(), ctype)

        def send_json(self, obj, code=200):
            self.send(code, json.dumps(obj, ensure_ascii=False))

        def local_only(self):
            # Refuse other sites (Origin) and DNS-rebinding (Host) so only the dashboard can drive this.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            origin = self.headers.get("Origin")
            ok = host in ("127.0.0.1", "localhost") and (
                not origin or urlparse(origin).hostname in ("127.0.0.1", "localhost"))
            if not ok:
                self.send_json({"error": "forbidden"}, 403)
            return ok

        def do_GET(self):
            if not self.local_only():
                return
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if not url.path.startswith("/api/"):
                return self.send_static(url.path)
            if url.path == "/api/sessions":
                conn = db()
                sync(conn)
                live = live_statuses(conn)
                cfg = config()
                rows = query_sessions(conn) + query_sessions(conn, archived=True)
                return self.send_json({"sessions": [row_json(r, live, cfg["ticket_url"]) for r in rows]})
            if url.path == "/api/search":
                hits = search_ids(db(), (q.get("q") or [""])[0])
                return self.send_json({"hits": [{"id": k, "snippet": v} for k, v in hits.items()]})
            self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            if not self.local_only():
                return
            url = urlparse(self.path)
            m = re.fullmatch(r"/api/(summarize|open|pin|archive)/([0-9a-f-]{36})", url.path)
            if not m:
                return self.send_json({"error": "not found"}, 404)
            action, sid = m.groups()
            on = parse_qs(url.query).get("on") == ["1"]
            conn = db()
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                return self.send_json({"error": "no such session"}, 404)
            if action == "summarize":
                return self.send_json({"summary": summarize(conn, sid)})
            if action == "pin":
                conn.execute("UPDATE sessions SET pinned=? WHERE id=?", (int(on), sid))
            elif action == "archive":
                conn.execute("UPDATE sessions SET archived_at=? WHERE id=?", (time.time() if on else None, sid))
            elif action == "open":
                try:
                    open_in_ghostty(prepare_resume(conn, row), f"claude --resume {sid}")
                except Exception as e:
                    return self.send_json({"ok": False, "error": str(e)})
            conn.commit()
            self.send_json({"ok": True})

    return Handler


def server_running(port):
    import urllib.request
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/search?q=", timeout=1)
        return True
    except Exception:
        return False


def cmd_serve(args):
    from http.server import ThreadingHTTPServer
    import webbrowser

    url = f"http://127.0.0.1:{args.port}"
    sync(db())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler())
    print(f"cairn dashboard on {url}  (Ctrl-C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_ui(args):
    """Open the dashboard, starting the server in the background if needed."""
    import webbrowser

    url = f"http://127.0.0.1:{args.port}"
    root = resolve_scope(args)
    params = {"dir": root, "branch": current_branch()} if root else {}
    page = f"{url}/?{urlencode({k: v for k, v in params.items() if v})}" if params else url
    if not server_running(args.port):
        DATA.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [sys.executable, SELF, "serve", "--port", str(args.port)],
            stdin=subprocess.DEVNULL, stdout=open(LOG_PATH, "a"), stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        for _ in range(50):
            if server_running(args.port):
                break
            time.sleep(0.1)
    webbrowser.open(page)
    print(page)


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(prog="cairn", description="Find and resume Claude Code sessions.")
    sub = p.add_subparsers(dest="cmd")

    def scope_args(s):
        s.add_argument("-a", "--all", action="store_true", help="sessions from every directory, not just this project")
        s.add_argument("-d", "--dir", help="sessions from this directory (and below) instead of the current project")
        s.add_argument("--archived", action="store_true", help="show archived sessions instead")

    s = sub.add_parser("go", help="pick a session and resume it in its directory (default)")
    s.add_argument("query", nargs="*")
    s.add_argument("-p", "--print", action="store_true", help="print the resume command instead of running it")
    s.add_argument("-g", "--ghostty", action="store_true", help="open it in a new Ghostty tab")
    s.add_argument("-s", "--deep", action="store_true", help="match the query against full transcripts")
    scope_args(s)
    s.set_defaults(fn=cmd_go)

    s = sub.add_parser("ls", help="list sessions: pinned, current branch, then newest")
    s.add_argument("query", nargs="*")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.add_argument("-t", "--tickets", action="store_true", help="group by ticket (DEV-123)")
    s.add_argument("-s", "--deep", action="store_true", help="match the query against full transcripts")
    s.add_argument("--empty", action="store_true", help="include sessions with no prompts")
    s.add_argument("--json", action="store_true")
    scope_args(s)
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("search", help="full-text search of transcripts, then pick one to resume")
    s.add_argument("query", nargs="+")
    s.add_argument("-l", "--list", action="store_true", help="just list matches with snippets")
    s.add_argument("-g", "--ghostty", action="store_true", help="open the pick in a new Ghostty tab")
    scope_args(s)
    s.set_defaults(fn=cmd_search)

    for name, hlp in (("pin", "pin sessions to the top"), ("unpin", "unpin sessions"),
                      ("archive", "hide finished sessions (they come back on new activity)"),
                      ("unarchive", "bring archived sessions back")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("ids", nargs="+", help="session ids or unique prefixes")
        s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("ui", help="open the dashboard (starts the server if needed)")
    scope_args(s)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("serve", help="run the dashboard server in the foreground")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("summarize", help="summarize sessions with Haiku",
                       description="With no ids, summarizes every session whose summary is missing or "
                                   "out of date (new prompts since it was written).")
    s.add_argument("ids", nargs="*", help="specific session ids or prefixes (always re-summarized)")
    s.add_argument("-d", "--dir", help="only sessions started in this directory or below it (e.g. . or ~/Code/app)")
    s.add_argument("--exact", action="store_true", help="with --dir: that directory only, not subdirectories")
    s.add_argument("--missing", action="store_true", help="only sessions with no summary at all")
    s.add_argument("-f", "--force", action="store_true", help="rewrite summaries from scratch even if up to date")
    s.add_argument("-j", "--jobs", type=int, default=4, help="parallel Haiku calls (default 4)")
    s.add_argument("-n", "--limit", type=int)
    s.set_defaults(fn=cmd_summarize)

    s = sub.add_parser("config", help="show or set options: " + ", ".join(DEFAULT_CONFIG))
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    sub.add_parser("sync", help="re-index transcripts").set_defaults(fn=cmd_sync)
    s = sub.add_parser("install", help="add hooks and the status line to ~/.claude/settings.json")
    s.add_argument("--no-statusline", action="store_true")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove cairn's hooks and status line").set_defaults(fn=cmd_uninstall)
    sub.add_parser("hook", help=argparse.SUPPRESS).set_defaults(fn=cmd_hook)
    s = sub.add_parser("statusline", help=argparse.SUPPRESS)
    s.add_argument("--wrap")
    s.set_defaults(fn=cmd_statusline)
    s = sub.add_parser("_update", help=argparse.SUPPRESS)
    s.add_argument("path")
    s.add_argument("event", nargs="?", default="")
    s.set_defaults(fn=cmd_update)

    argv = sys.argv[1:]
    if not argv or (argv[0] not in sub.choices and argv[0] not in ("-h", "--help")):
        argv = ["go", *argv]  # `cairn`, `cairn -a`, `cairn auth bug` all mean `cairn go …`
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except Exception as e:
        if args.cmd in ("hook", "_update", "statusline"):
            log(f"{args.cmd}: {e!r}")  # never surface errors inside a Claude session
        else:
            raise


if __name__ == "__main__":
    main()
