# cairn

Find the right Claude Code session and hop back into it, instead of starting a new one.

cairn indexes the transcripts Claude Code already writes (`~/.claude/projects/*/*.jsonl`) into
`~/.cairn/cairn.db`: id, name, directory, git branch, ticket, last activity, PR link, whether the
session is open right now, and a one-line summary written by Haiku in the background.
Standard-library Python, no dependencies. The dashboard lives in
[cairn-ui](https://github.com/habibulloxon/cairn-ui); see [LOCAL_SETUP.md](LOCAL_SETUP.md) to run both.

```bash
cairn                    # pick a session from this project (fzf) and resume it in its own directory
cairn auth bug           # same, pre-filtered; resumes directly if only one matches
cairn 35255c3f           # resume by id prefix (what the start-of-session reminder prints)
cairn -g                 # ...in a new Ghostty tab instead of this terminal
cairn -a / -d DIR        # every directory / another directory
cairn -p                 # print the `cd … && claude --resume …` command instead

cairn ls [query]         # table: ★ pinned, ▶ running, ! waiting on you, ● open, ⎇ on your branch
cairn ls -t              # grouped by ticket (DEV-1157)
cairn search webhook retry       # full-text search of every transcript, then pick one to resume
cairn search -l webhook retry    # just list matches with snippets
cairn pin ID… / unpin ID…        # keep active tasks at the top
cairn archive ID… / unarchive    # hide finished work (comes back on new activity); ls --archived
cairn ui                 # dashboard at http://127.0.0.1:7444, filtered to this project and branch

cairn summarize --dir ~/Code/app # summarize every session started in a dir (and below)
cairn summarize --missing -n 20  # backfill sessions that have no summary yet
cairn config ticket_url 'https://linear.app/acme/issue/{ticket}'   # make ticket chips links
cairn install / uninstall        # hooks + status line in ~/.claude/settings.json (backed up)
```

"This project" is the git repo root you're in (or the cwd outside git), including subdirectories,
and sessions started in a multi-repo parent folder whose work was in this repo. Ordering is pinned
first, then sessions on your current branch, then most recent.

## What runs where

| Hook | Does |
|---|---|
| `SessionStart` | Marks the session open. On a fresh start, if this project has a session on the same feature branch (or one active in the last 24h), shows a reminder with `cairn <id>` to resume it instead. |
| `UserPromptSubmit`, `PostToolUse` | Marks the session running. |
| `PermissionRequest` | Marks it waiting on you. |
| `Stop` | Marks it idle, then (detached) re-indexes it and refreshes the summary every 3 prompts or 10 min. |
| `SessionEnd` | Marks it closed and writes a final summary. |

Hooks take ~45ms and never print errors into a session (they go to `~/.cairn/cairn.log`). Sessions
whose `claude` process died without `SessionEnd` are detected by PID (`$CLAUDE_PID`) and shown closed.
Resuming a session that's still open elsewhere asks first: two copies would write to one transcript.

**Summaries** run `claude -p --model haiku --effort low --no-session-persistence --tools ""` with thinking and hooks off (~4s each),
so they use your normal login, leave no transcript, and don't trigger cairn again.

**Branches Claude Code didn't record** (multi-repo folders, non-git dirs): the summary call also gets
the session's git commands and output, and Haiku names the branch and sub-repo. The guess is kept only
if that exact name appears in the git evidence, and is shown as `≈ DEV-1157`.

**Tickets** come from the branch, inferred branch or title (`DEV-1157`, `dev-485-follow-up`).

**Transcripts survive cleanup.** Claude Code deletes transcripts after `cleanupPeriodDays` (30 by
default). cairn hard-links each one into `~/.cairn/transcripts` (no extra disk while the original
exists), and when you resume a deleted session it copies it back first.

**Status line**: `cairn install` wraps your existing status line command and adds a line with the
session's name, ticket and short id. `cairn install --no-statusline` skips that; `uninstall` restores it.

**Dashboard** (`ui/`, React + shadcn/ui, black and white, follows the system light/dark setting
with a toggle): search or full-transcript search, project filter, group by ticket, pin, archive,
summarize, copy resume command / ID, and *Open*, which opens a new Ghostty tab in the session's
directory running `claude --resume`. `cairn serve` serves the built app from `ui/dist`; after
changing it, rebuild with `cd ui && npm install && npm run build` (or `npm run dev` against a
running `cairn serve` for hot reload).

Config (`cairn config`): `ticket_url`, `nudge` (true/false), `nudge_hours` (24).
Environment: `CAIRN_HOME` (data dir, default `~/.cairn`), `CAIRN_MODEL` (default `haiku`).
