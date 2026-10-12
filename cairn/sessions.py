"""Looking sessions up, filtering them to a project, and describing them."""

import json
import os
import re
import shlex
from pathlib import Path

from cairn import index, summarize
from cairn.util import clip, epoch, git, tilde

TICKET_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,10})-(\d{1,6})(?![0-9])")
TICKET_UPPER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,10})-(\d{1,6})(?![0-9])")
# Prefixes that look like tickets but aren't (UTF-8, SHA-256, step-2 …).
NOT_TICKETS = {"UTF", "SHA", "ISO", "HTTP", "HTTPS", "RFC", "TLS", "SSL", "AES", "MD", "GPT", "IPV",
               "WCAG", "ES", "CVE", "PEP", "TS", "STEP", "PHASE", "PART", "ITEM", "FIX", "TOP", "DAY",
               "WEEK", "SPRINT", "VERSION", "OPUS", "HAIKU", "SONNET", "CLAUDE", "TEST", "TESTS", "PR"}
SEARCH_FIELDS = ("id", "custom_title", "ai_title", "summary", "cwd", "branches",
                 "inferred_branch", "inferred_repo", "first_prompt", "last_prompt")
ID_PREFIX_RE = re.compile(r"[0-9a-f][0-9a-f-]{3,35}")


class NotFound(LookupError):
    pass


# ----------------------------------------------------------- describing

def display_name(row):
    return row["custom_title"] or row["ai_title"] or clip(row["first_prompt"], 70) or "(untitled)"


def resume_command(row):
    cmd = f"claude --resume {row['id']}"
    return f"cd {shlex.quote(row['cwd'])} && {cmd}" if row["cwd"] else cmd


def shown_branch(row):
    """The recorded branch, or ≈ the inferred one."""
    if row["branch"]:
        return row["branch"]
    return f"≈{row['inferred_branch']}" if row["inferred_branch"] else None


def ticket_of(row):
    """A tracker ID like DEV-1157 from the branch, title or first prompt."""
    branches = json.loads(row["branches"] or "[]")
    candidates = [(row["branch"], TICKET_RE), (row["inferred_branch"], TICKET_RE),
                  (row["custom_title"], TICKET_RE)]
    candidates += [(b, TICKET_RE) for b in reversed(branches)]
    # Free text only counts in upper case, to skip words like "end-2".
    candidates += [(row["ai_title"], TICKET_UPPER_RE), (row["first_prompt"], TICKET_UPPER_RE)]
    for text, rx in candidates:
        for m in rx.finditer(text or ""):
            prefix = m.group(1).upper()
            if prefix not in NOT_TICKETS:
                return f"{prefix}-{m.group(2)}"
    return None


def is_archived(row):
    # New activity after archiving brings a session back.
    return bool(row["archived_at"]) and epoch(row["last_active"]) <= row["archived_at"]


def to_json(row, live=None, ticket_url=""):
    """The dashboard's view of a session."""
    ticket = ticket_of(row)
    return {
        "id": row["id"],
        "name": display_name(row),
        "custom_title": row["custom_title"],
        "ai_title": row["ai_title"],
        "summary": row["summary"],
        "first_prompt": clip(row["first_prompt"], 300),
        "last_prompt": clip(row["last_prompt"], 300),
        "cwd": row["cwd"],
        "dir": tilde(row["cwd"]),
        "branch": row["branch"],
        "branches": json.loads(row["branches"] or "[]"),
        "inferred_branch": row["inferred_branch"],
        "inferred_repo": row["inferred_repo"],
        "ticket": ticket,
        "ticket_url": ticket_url.replace("{ticket}", ticket) if ticket and ticket_url else None,
        "turns": row["turns"],
        "started_at": row["started_at"],
        "last_active": row["last_active"],
        "pr_url": row["pr_url"],
        "missing": bool(row["missing"]),
        "resumable": index.resumable(row),
        "pinned": bool(row["pinned"]),
        "archived": is_archived(row),
        "status": (live or {}).get(row["id"]),
        "resume": resume_command(row),
        "stale": summarize.is_stale(row),
    }


# ----------------------------------------------------------- scope

def current_scope(cwd=None):
    """The project you're standing in: the git repo root if inside one, else the directory."""
    cwd = cwd or os.getcwd()
    return git(cwd, "rev-parse", "--show-toplevel") or cwd


def current_branch(cwd=None):
    return git(cwd or os.getcwd(), "branch", "--show-current") or None


def in_dir(row, root, exact=False):
    """Started in root (or below it), or in a multi-repo parent folder whose work was in root."""
    cwd = row["cwd"] or ""
    if cwd == root or (not exact and cwd.startswith(root.rstrip("/") + "/")):
        return True
    return bool(row["inferred_repo"]) and os.path.join(cwd, row["inferred_repo"]) == root


def on_branch(row, branch):
    return bool(branch) and branch in (row["branch"], row["inferred_branch"])


def normalize_dir(path):
    return str(Path(path).expanduser().resolve())


# ----------------------------------------------------------- queries

def get(conn, session_id):
    return conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()


def resolve_id(conn, prefix):
    """The full id for a unique id prefix (at least 4 characters)."""
    prefix = prefix.strip().lower()
    if not ID_PREFIX_RE.fullmatch(prefix):
        raise NotFound(f"'{prefix}' isn't a session id (or a prefix of at least 4 characters).")
    rows = conn.execute("SELECT id FROM sessions WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)).fetchall()
    if len(rows) != 1:
        raise NotFound(f"{'No' if not rows else 'More than one'} session matches '{prefix}'.")
    return rows[0]["id"]


def query(conn, text=None, root=None, exact=False, archived=False, ids=None, include_empty=False):
    """Sessions, newest first.

    text: every word must appear in the name, summary, branch, prompts …
    root: only sessions in this project (see in_dir).
    archived: False (default) hides archived sessions, True shows only those, None shows both.
    ids: only these ids (transcript search results).
    """
    words = text.lower().split() if text else []
    out = []
    for r in conn.execute("SELECT * FROM sessions ORDER BY last_active DESC"):
        if not include_empty and not r["turns"]:
            continue
        if archived is not None and is_archived(r) != archived:
            continue
        if ids is not None and r["id"] not in ids:
            continue
        if root and not in_dir(r, root, exact):
            continue
        if words:
            hay = " ".join(str(r[k] or "") for k in SEARCH_FIELDS).lower()
            if not all(w in hay for w in words):
                continue
        out.append(r)
    return out


def ranked(rows, branch):
    """Pinned first, then sessions on the current branch, then by recency (input order)."""
    return sorted(rows, key=lambda r: (not r["pinned"], not on_branch(r, branch)))
