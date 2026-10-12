import json
import os
import sqlite3
import unittest

from cairn import index, live, paths, sessions, store, summarize
from tests.helpers import SID_A, SID_B, TempHome, assistant, user


def row(**fields):
    """A sessions row as a dict, with defaults for every column the helpers read."""
    base = {"id": SID_A, "cwd": "/work/app", "branch": None, "branches": "[]", "inferred_branch": None,
            "inferred_repo": None, "custom_title": None, "ai_title": None, "first_prompt": None,
            "archived_at": None, "last_active": "2026-10-01T10:00:00Z", "pinned": 0, "turns": 1,
            "missing": 0, "summary": None, "summary_turns": 0, "summary_at": None, "branch_inferred_at": None}
    base.update(fields)
    return base


class SyncTest(TempHome):
    def test_sync_search_and_missing(self):
        self.write_transcript(SID_A, [user("add webhook retries"), assistant("Done, retries use backoff")])
        self.write_transcript(SID_B, [user("unrelated work", cwd="/work/other")], project="-work-other")
        conn = store.connect()
        self.assertEqual(index.sync(conn), 2)
        self.assertEqual(index.sync(conn), 0)  # unchanged files aren't re-read

        hits = index.search(conn, "webhook backoff")
        self.assertEqual(list(hits), [SID_A])
        self.assertIn("\x02", hits[SID_A])
        self.assertEqual(index.search(conn, 'NEAR(" OR *'), {})  # user text can't inject FTS syntax

        self.assertEqual([r["id"] for r in sessions.query(conn, root="/work/app")], [SID_A])
        self.assertEqual([r["id"] for r in sessions.query(conn, "unrelated")], [SID_B])

        # Claude Code's cleanup deletes a transcript: flagged missing, still restorable.
        transcript = self.claude / "projects" / "-work-app" / f"{SID_A}.jsonl"
        transcript.unlink()
        index.sync(conn)
        r = sessions.get(conn, SID_A)
        self.assertTrue(r["missing"])
        self.assertTrue(index.resumable(r))
        self.assertTrue(index.restore_transcript(conn, r))
        self.assertTrue(transcript.exists())
        self.assertFalse(sessions.get(conn, SID_A)["missing"])

    def test_archive_is_a_hard_link(self):
        path = self.write_transcript(SID_A, [user("hi")])
        index.sync(store.connect())
        self.assertEqual(index.archive_path(path).stat().st_ino, path.stat().st_ino)

    def test_migrates_old_database(self):
        paths.DATA.mkdir(parents=True)
        old = sqlite3.connect(paths.DB_PATH)
        # The first release's table: no pinned/archived/inferred columns, plus a since-dropped one.
        old.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, transcript TEXT, cwd TEXT, branch TEXT, "
                    "branches TEXT, custom_title TEXT, ai_title TEXT, first_prompt TEXT, last_prompt TEXT, "
                    "turns INTEGER, started_at TEXT, last_active TEXT, pr_url TEXT, summary TEXT, "
                    "summary_turns INTEGER, summary_at REAL, file_size INTEGER, file_mtime REAL, "
                    "missing INTEGER, next_step TEXT)")
        old.execute("INSERT INTO sessions (id, summary) VALUES ('x', 'kept')")
        old.commit()
        old.close()
        conn = store.connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        self.assertIn("pinned", cols)
        self.assertNotIn("next_step", cols)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], store.SCHEMA_VERSION)
        self.assertEqual(conn.execute("SELECT summary FROM sessions").fetchone()[0], "kept")

    def test_resolve_id(self):
        self.write_transcript(SID_A, [user("hi")])
        conn = store.connect()
        index.sync(conn)
        self.assertEqual(sessions.resolve_id(conn, "aaaaaaaa"), SID_A)
        for bad in ("aa", "a%", "zzzzzzzz", "bbbbbbbb"):
            with self.assertRaises(sessions.NotFound):
                sessions.resolve_id(conn, bad)


class SessionHelpersTest(unittest.TestCase):
    def test_ticket_of(self):
        self.assertEqual(sessions.ticket_of(row(branch="dev-485-follow-up")), "DEV-485")
        self.assertEqual(sessions.ticket_of(row(inferred_branch="DEV-1157")), "DEV-1157")
        self.assertEqual(sessions.ticket_of(row(branches=json.dumps(["main", "ABC-9"]))), "ABC-9")
        self.assertEqual(sessions.ticket_of(row(first_prompt="see utf-8 and SHA-256 then PROJ-77")), "PROJ-77")
        self.assertIsNone(sessions.ticket_of(row(ai_title="fix end-2 handling")))

    def test_in_dir(self):
        self.assertTrue(sessions.in_dir(row(cwd="/work/app/sub"), "/work/app"))
        self.assertFalse(sessions.in_dir(row(cwd="/work/app/sub"), "/work/app", exact=True))
        self.assertFalse(sessions.in_dir(row(cwd="/work/apple"), "/work/app"))
        # Started in a multi-repo parent, work inferred to be in this repo.
        self.assertTrue(sessions.in_dir(row(cwd="/work", inferred_repo="app"), "/work/app"))

    def test_archived_comes_back_on_activity(self):
        archived = 1790000000.0
        self.assertTrue(sessions.is_archived(row(archived_at=archived, last_active="2026-09-01T00:00:00Z")))
        self.assertFalse(sessions.is_archived(row(archived_at=archived, last_active="2026-12-01T00:00:00Z")))

    def test_ranked(self):
        rows = [row(id="1"), row(id="2", branch="DEV-9"), row(id="3", pinned=1)]
        self.assertEqual([r["id"] for r in sessions.ranked(rows, "DEV-9")], ["3", "2", "1"])


class SummarizeHelpersTest(unittest.TestCase):
    EVIDENCE = "$ git checkout DEV-1157\n  [DEV-1157 a1b2c3d] add mask\n$ git -C rental-service push"

    def test_parse_reply_keeps_only_evidenced_branches(self):
        reply = summarize.parse_reply('{"summary": "Did X", "branch": "origin/DEV-1157", "repo": "rental-service"}',
                                      self.EVIDENCE)
        self.assertEqual(reply, {"summary": "Did X", "branch": "DEV-1157", "repo": "rental-service"})
        invented = summarize.parse_reply('{"summary": "Did X", "branch": "DEV-11"}', self.EVIDENCE)
        self.assertIsNone(invented["branch"])  # DEV-11 is only a prefix of DEV-1157

    def test_parse_reply_plain_text(self):
        self.assertEqual(summarize.parse_reply("Did X.", None)["summary"], "Did X.")
        fenced = summarize.parse_reply('```json\n{"summary": "Did Y", "branch": null}\n```', self.EVIDENCE)
        self.assertEqual((fenced["summary"], fenced["branch"]), ("Did Y", None))

    def test_needs_summary(self):
        self.assertTrue(summarize.needs_summary(row(turns=2)))
        self.assertFalse(summarize.needs_summary(row(turns=2, summary="s", summary_turns=2)))
        mid = row(turns=3, summary="s", summary_turns=2, summary_at=9e12)  # 1 new prompt, just summarized
        self.assertFalse(summarize.needs_summary(mid, final=False))
        self.assertTrue(summarize.needs_summary(mid, final=True))
        self.assertFalse(summarize.needs_summary(row(turns=0)))

    def test_excerpt_has_first_prompt_and_latest(self):
        dialogue = [("User", "start here")] + [("Claude", "x" * 800)] * 40 + [("User", "the latest ask")]
        text = summarize.build_excerpt(dialogue, "old summary")
        self.assertIn("start here", text)
        self.assertIn("the latest ask", text)
        self.assertIn("old summary", text)
        self.assertLess(len(text), summarize.EXCERPT_CHARS + 2000)

    def test_plan_text(self):
        self.assertIn("for 1 session:", summarize.plan_text(1, 4))
        self.assertIn("about 42s", summarize.plan_text(28, 4))


class LiveTest(TempHome):
    def test_dead_process_is_not_open(self):
        conn = store.connect()
        conn.execute("INSERT INTO live VALUES (?, 'running', ?, 0)", (SID_A, 999999))
        conn.execute("INSERT INTO live VALUES (?, 'idle', ?, 0)", (SID_B, os.getpid()))  # alive, but not claude
        self.assertEqual(live.statuses(conn), {})


if __name__ == "__main__":
    unittest.main()
