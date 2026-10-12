"""Reading Claude Code transcripts (~/.claude/projects/<dir>/<session-id>.jsonl)."""

import json
import re

from cairn.util import clip

PASTED_RE = re.compile(r"<pasted_content[^>]*>.*?</pasted_content[^>]*>", re.S)
NOISE_PREFIXES = (
    "<local-command", "<command-name", "<command-message", "<system-reminder",
    "<task-notification", "Caveat:", "[Request interrupted",
)


def _records(path):
    """Each JSON object in the transcript; unreadable lines are skipped."""
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def prompt_text(rec):
    """The human-typed text of a user record, or None for tool results and meta records."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if not parts:
            return None  # a turn that only carries tool results
        content = "\n".join(parts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return None
    return PASTED_RE.sub("[pasted]", text).strip() or None


def parse(path):
    """(session fields, dialogue) for a transcript.

    The dialogue is a list of (who, text) with who in User / Claude / Cmd (a Bash command).
    """
    info = {
        "id": path.stem, "transcript": str(path), "cwd": None, "branch": None,
        "branches": [], "custom_title": None, "ai_title": None,
        "first_prompt": None, "last_prompt": None, "turns": 0,
        "started_at": None, "last_active": None, "pr_url": None,
    }
    dialogue = []
    for rec in _records(path):
        kind = rec.get("type")
        if kind == "custom-title":
            info["custom_title"] = rec.get("customTitle") or info["custom_title"]
        elif kind == "ai-title":
            info["ai_title"] = rec.get("aiTitle") or info["ai_title"]
        elif kind == "pr-link":
            info["pr_url"] = rec.get("prUrl") or info["pr_url"]
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            continue

        # The launch directory, not the latest one: Claude Code files the transcript
        # under it, so `claude --resume` only finds the session from there.
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
        for c in content if isinstance(content, list) else ():
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and c.get("text", "").strip():
                dialogue.append(("Claude", c["text"].strip()))
            elif c.get("type") == "tool_use" and c.get("name") == "Bash":
                cmd = str((c.get("input") or {}).get("command", "")).strip()
                if cmd:
                    dialogue.append(("Cmd", cmd))
    return info, dialogue


GIT_CMD_RE = re.compile(r"\bgit\b.*\b(checkout|switch|push|worktree|branch|merge|rebase|pull|commit)\b")
GIT_OUT_RE = re.compile(
    r"^(On branch \S+|Switched to (a new )?branch .+|Your branch .+|\[[\w./-]+ [0-9a-f]{7,}\] .*"
    r"|.*\[new branch\].*|.*\b[\w./-]+ -> [\w./-]+$|branch '.+' set up to track.*)"
)


def git_evidence(path, max_lines=50):
    """The session's branch-related git commands and output lines, oldest first, as one block.

    Used to infer the branch when Claude Code didn't record one.
    """
    lines = []
    for rec in _records(path):
        if rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else ():
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
    seen, recent = set(), []
    for line in reversed(lines):  # keep the most recent occurrence of each line
        if line not in seen:
            seen.add(line)
            recent.append(line)
    return "\n".join(reversed(recent[:max_lines]))
