"""Small helpers shared by every module."""

import json
import subprocess
import time
from datetime import datetime

from cairn import paths


def log(msg):
    """Append to ~/.cairn/cairn.log. Used where printing would land inside a Claude session."""
    try:
        paths.DATA.mkdir(parents=True, exist_ok=True)
        with open(paths.LOG_PATH, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


def clip(text, n):
    """Collapse whitespace and cut to n characters with an ellipsis."""
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def tilde(path):
    home = str(paths.HOME)
    if path and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path or ""


def epoch(iso):
    """Seconds since the epoch for an ISO timestamp like 2026-10-11T10:51:32.730Z; 0 if unparseable."""
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
    """stdout of a git command in cwd, or "" if git fails or isn't there."""
    try:
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default
