import unittest

from cairn import transcripts
from tests.helpers import SID_A, TempHome, assistant, tool_result, user


class ParseTest(TempHome):
    def test_fields_and_dialogue(self):
        path = self.write_transcript(SID_A, [
            {"type": "custom-title", "customTitle": "My task"},
            {"type": "ai-title", "aiTitle": "Fix login"},
            user("fix the login bug", cwd="/work/app", branch="main", ts="2026-10-01T09:00:00Z"),
            assistant("Looking at auth.py", bash="git checkout -b DEV-42", cwd="/work/app/sub"),
            tool_result("Switched to a new branch 'DEV-42'"),
            user("<local-command-stdout>noise</local-command-stdout>"),
            user("now add tests", cwd="/work/app/sub", ts="2026-10-01T11:00:00Z"),
            {"type": "pr-link", "prUrl": "https://example.com/pr/1"},
        ])
        info, dialogue = transcripts.parse(path)
        self.assertEqual(info["id"], SID_A)
        self.assertEqual(info["custom_title"], "My task")
        self.assertEqual(info["ai_title"], "Fix login")
        self.assertEqual(info["cwd"], "/work/app")  # the launch dir, not the later one
        self.assertEqual(info["branch"], "DEV-42")
        self.assertEqual(info["branches"], ["main", "DEV-42"])
        self.assertEqual(info["turns"], 2)  # tool results and command noise aren't prompts
        self.assertEqual(info["first_prompt"], "fix the login bug")
        self.assertEqual(info["last_prompt"], "now add tests")
        self.assertEqual(info["started_at"], "2026-10-01T09:00:00Z")
        self.assertEqual(info["pr_url"], "https://example.com/pr/1")
        self.assertIn(("Cmd", "git checkout -b DEV-42"), dialogue)
        self.assertIn(("Claude", "Looking at auth.py"), dialogue)

    def test_sidechains_and_pasted_content(self):
        path = self.write_transcript(SID_A, [
            user("subagent prompt", isSidechain=True),
            user('look <pasted_content id="x">secret dump</pasted_content id="x"> here'),
            "not json",
        ])
        path.write_text(path.read_text() + "{broken\n")
        info, _ = transcripts.parse(path)
        self.assertEqual(info["turns"], 1)
        self.assertEqual(info["first_prompt"], "look [pasted] here")

    def test_head_is_not_a_branch(self):
        path = self.write_transcript(SID_A, [user("hi", branch="HEAD")])
        self.assertIsNone(transcripts.parse(path)[0]["branch"])


class GitEvidenceTest(TempHome):
    def test_keeps_git_lines_most_recent_last(self):
        path = self.write_transcript(SID_A, [
            assistant(bash="ls -la"),
            assistant(bash="git checkout DEV-1"),
            tool_result("Switched to branch 'DEV-1'\nunrelated output"),
            assistant(bash="git push -u origin DEV-2"),
            tool_result([{"type": "text", "text": " * [new branch]      DEV-2 -> DEV-2"}]),
        ])
        evidence = transcripts.git_evidence(path)
        self.assertNotIn("ls -la", evidence)
        self.assertNotIn("unrelated", evidence)
        lines = evidence.splitlines()
        self.assertEqual(lines[0], "$ git checkout DEV-1")
        self.assertIn("[new branch]", lines[-1])


if __name__ == "__main__":
    unittest.main()
