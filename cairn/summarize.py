"""One-line session summaries (and inferred branches) from Haiku, via `claude -p`."""

import os
import re
import json
import shutil
import subprocess
import time
from contextlib import closing
from pathlib import Path

from cairn import paths, store, transcripts
from cairn.util import clip, log, plural

MODEL = os.environ.get("CAIRN_MODEL", "haiku")
EVERY_TURNS = 3          # on Stop, re-summarize after this many new prompts
MIN_GAP = 10 * 60        # ...or after this many seconds with any new prompt
EXCERPT_CHARS = 14000
SECONDS_EACH = 6         # typical call with thinking off; for the "about N min" estimate
TIMEOUT = 180

SYSTEM_PROMPT = (
    "You write one-line summaries of Claude Code sessions for a session picker. "
    "Given an excerpt of a coding session, reply with ONLY the summary: one or two "
    "plain sentences, at most 220 characters, past tense, concrete (name the feature, "
    "bug, file or command). Say what was done and where it stands, e.g. "
    "'Added CSV export to the reports API; tests pass, frontend button still TODO.' "
    "No preamble, no quotes, no markdown."
)
BRANCH_PROMPT = (
    "\n\nThe session's git branch wasn't recorded (it ran in a folder that holds several "
    "repos, or outside one). From the git activity below, also identify the branch the "
    "session's work was done on: the feature branch that was checked out, committed or "
    "pushed, not main/master/dev unless the work really was committed there. Use null if "
    "the evidence doesn't make it clear; never guess a name that isn't in the evidence. "
    "If the work was in a sub-repository, give its directory name as repo.\n"
    "Reply with ONLY this JSON instead: "
    '{"summary": "...", "branch": "..." or null, "repo": "..." or null}'
)


# ----------------------------------------------------------- when to run

def needs_summary(row, final=True):
    """New prompts since the last summary. Without `final` (mid-session), only every few turns."""
    if not row or row["missing"] or not row["turns"]:
        return False
    new_turns = row["turns"] - (row["summary_turns"] or 0)
    if new_turns <= 0:
        return False
    if final or not row["summary"]:
        return True
    return new_turns >= EVERY_TURNS or time.time() - (row["summary_at"] or 0) >= MIN_GAP


def needs_branch(row):
    """No recorded branch, and Haiku was never asked to infer one."""
    return bool(row and not row["missing"] and row["turns"]
                and not row["branch"] and row["branch_inferred_at"] is None)


def is_stale(row):
    return needs_summary(row) or needs_branch(row)


def plan_text(n, jobs):
    """The confirmation shown before summarizing several sessions (the dashboard words it the same)."""
    secs = max(1, -(-n // max(1, jobs))) * SECONDS_EACH
    eta = f"{secs}s" if secs < 90 else f"{round(secs / 60)} min"
    return (f"You are going to create summaries for {plural(n, 'session')}: "
            f"{plural(n, 'Haiku call')} on your Claude plan, about {eta} at {jobs} at a time.")


# ----------------------------------------------------------- the call

def build_excerpt(dialogue, previous):
    """The first prompt plus as much of the most recent conversation as fits."""
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
    parts = [f"Previous summary (update it): {previous}"] if previous else []
    parts.append(f"Session started with: {clip(first, 1200)}")
    parts.append("Most recent part of the session:\n" + "\n\n".join(tail))
    return "\n\n".join(parts)


def parse_reply(text, evidence):
    """{summary, branch, repo} from Haiku's reply.

    A branch is kept only if it literally appears in the git evidence, so Haiku can't invent one.
    """
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
        branch = branch[len("origin/"):] if branch.startswith("origin/") else branch
        if re.search(rf"(?<![\w./-]){re.escape(branch)}(?![\w/-])", evidence):
            out["branch"] = branch
            if repo and repo.strip("/") in evidence:
                out["repo"] = repo.strip("/")
    return out


def _ask_haiku(system, prompt):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude is not on PATH")
    paths.DATA.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [
            claude, "-p", "--model", MODEL, "--effort", "low",
            "--no-session-persistence",          # leaves no transcript, so it's not a session itself
            "--tools", "", "--strict-mcp-config",
            "--settings", '{"disableAllHooks": true}',
            "--system-prompt", system,
        ],
        input=prompt, capture_output=True, text=True, timeout=TIMEOUT,
        cwd=paths.DATA,  # no project CLAUDE.md gets loaded
        # CAIRN_INTERNAL: belt and braces against our own hooks. No extended thinking: with a high
        # global effortLevel Haiku spent ~4000 thinking tokens (30s) on a 60-word summary.
        env=dict(os.environ, CAIRN_INTERNAL="1", MAX_THINKING_TOKENS="0"),
    )


def _lock(session_id):
    """A lock file so one session is never summarized twice at once; None if already held."""
    paths.LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock = paths.LOCK_DIR / f"{session_id}.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > TIMEOUT + 60:
            lock.unlink()  # stale: a summarizer died
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return lock
    except FileExistsError:
        return None


def summarize(conn, session_id, fresh=False):
    """Write a new summary for one session. Returns it, or None (see the log for why).

    `fresh` ignores the previous summary instead of asking Haiku to update it.
    """
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or row["missing"] or not row["turns"]:
        return None
    lock = _lock(session_id)
    if not lock:
        return None
    try:
        path = Path(row["transcript"])
        info, dialogue = transcripts.parse(path)
        system = SYSTEM_PROMPT
        prompt = build_excerpt(dialogue, None if fresh else row["summary"])
        evidence = None if info["branch"] else transcripts.git_evidence(path)
        if evidence:
            system += BRANCH_PROMPT
            prompt += "\n\nGit activity in this session (commands and output):\n" + evidence

        proc = _ask_haiku(system, prompt)
        reply = parse_reply(proc.stdout.strip(), evidence)
        summary = clip(reply["summary"].strip('"'), 300)
        if proc.returncode != 0 or not summary:
            log(f"summarize {session_id}: rc={proc.returncode} {proc.stderr.strip()[:300]}")
            return None

        now = time.time()
        conn.execute("UPDATE sessions SET summary=?, summary_turns=?, summary_at=? WHERE id=?",
                     (summary, info["turns"], now, session_id))
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
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as e:
        log(f"summarize {session_id}: {e!r}")
        return None
    finally:
        lock.unlink(missing_ok=True)


def summarize_many(ids, jobs=4, fresh=False, on_done=None):
    """Summarize sessions in parallel; on_done(id, summary) after each. Returns (ok, failed)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def work(session_id):
        with closing(store.connect()) as conn:  # sqlite connections can't be shared across threads
            return session_id, summarize(conn, session_id, fresh=fresh)

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for fut in as_completed([pool.submit(work, i) for i in ids]):
            session_id, summary = fut.result()
            ok += bool(summary)
            failed += not summary
            if on_done:
                on_done(session_id, summary)
    return ok, failed
