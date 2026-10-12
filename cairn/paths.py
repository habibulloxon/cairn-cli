"""Where cairn reads and writes.

Everything else reads these as `paths.X` (not `from paths import X`), so
configure() can repoint them, e.g. at temporary directories in tests.
"""

import os
import sys
from pathlib import Path

# The repo checkout, or Homebrew's libexec: holds bin/cairn and ui/dist.
PKG_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PKG_ROOT / "bin" / "cairn"


def self_argv():
    """The argv that runs this cairn: the launcher in a checkout or Homebrew, else `python -m cairn`."""
    if LAUNCHER.exists():
        return [sys.executable, str(LAUNCHER)]
    return [sys.executable, "-m", "cairn"]  # installed with pip/pipx


def configure(data=None, claude=None, ui_dist=None):
    global HOME, CLAUDE_DIR, PROJECTS, SETTINGS
    global DATA, DB_PATH, LOG_PATH, LOCK_DIR, ARCHIVE, CONFIG_PATH, UI_DIST

    HOME = Path.home()
    CLAUDE_DIR = Path(claude or os.environ.get("CAIRN_CLAUDE_DIR") or HOME / ".claude").expanduser()
    PROJECTS = CLAUDE_DIR / "projects"  # Claude Code transcripts: projects/<dir>/<session>.jsonl
    SETTINGS = CLAUDE_DIR / "settings.json"

    DATA = Path(data or os.environ.get("CAIRN_HOME") or HOME / ".cairn").expanduser()
    DB_PATH = DATA / "cairn.db"
    LOG_PATH = DATA / "cairn.log"
    LOCK_DIR = DATA / "locks"
    ARCHIVE = DATA / "transcripts"  # hard links to transcripts, so cleanup can't take them
    CONFIG_PATH = DATA / "config.json"

    # The built dashboard (the cairn-ui repo, cloned into ./ui and built with `npm run build`).
    UI_DIST = Path(ui_dist or os.environ.get("CAIRN_UI_DIST") or PKG_ROOT / "ui" / "dist").expanduser()


configure()
