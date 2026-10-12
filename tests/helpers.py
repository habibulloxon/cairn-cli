"""Fixtures: a throwaway ~/.claude and ~/.cairn, and fake transcripts."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from cairn import paths

SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"


def user(text, cwd="/work/app", branch="DEV-42", ts="2026-10-01T10:00:00.000Z", **extra):
    return {"type": "user", "message": {"role": "user", "content": text}, "cwd": cwd,
            "gitBranch": branch, "timestamp": ts, **extra}


def assistant(text=None, bash=None, cwd="/work/app", branch="DEV-42", ts="2026-10-01T10:01:00.000Z"):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if bash:
        content.append({"type": "tool_use", "name": "Bash", "input": {"command": bash}})
    return {"type": "assistant", "message": {"role": "assistant", "content": content}, "cwd": cwd,
            "gitBranch": branch, "timestamp": ts}


def tool_result(output):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": output}]}, "timestamp": "2026-10-01T10:02:00.000Z"}


class TempHome(unittest.TestCase):
    """Points cairn at empty temporary claude/data dirs for each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cairn-test-"))
        self.claude = self.tmp / "claude"
        self.data = self.tmp / "data"
        (self.claude / "projects").mkdir(parents=True)
        paths.configure(data=self.data, claude=self.claude, ui_dist=self.tmp / "dist")

    def tearDown(self):
        paths.configure()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_transcript(self, session_id, records, project="-work-app"):
        path = self.claude / "projects" / project / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        return path
