"""Getting a session ready to resume, and opening it in a new Ghostty tab."""

import os
import subprocess
import sys

from cairn import index, paths

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


def prepare_resume(conn, row):
    """Check a session can be resumed, restoring its transcript if needed. Returns its directory."""
    if not index.restore_transcript(conn, row):
        raise RuntimeError("Its transcript was deleted by Claude Code's cleanup before cairn archived it.")
    cwd = row["cwd"] or str(paths.HOME)
    if not os.path.isdir(cwd):
        raise RuntimeError(f"Directory {cwd} no longer exists.")
    return cwd


def open_in_ghostty(cwd, session_id):
    """Open a new Ghostty tab (Ghostty 1.3+, macOS) in cwd running `claude --resume`."""
    if sys.platform != "darwin":
        raise RuntimeError("Opening in Ghostty needs macOS.")
    proc = subprocess.run(["osascript", "-e", GHOSTTY_SCRIPT, cwd, f"claude --resume {session_id}"],
                          capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "osascript failed")
