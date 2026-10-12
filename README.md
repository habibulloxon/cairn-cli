# cairn

Find the right Claude Code session and hop back into it, instead of starting a new one.

Claude Code keeps every session as a transcript, but `claude --resume` only lists them by
directory and you have to remember which one held the work. cairn indexes those transcripts
into a local database and gives you a picker, a dashboard and a few hooks around them:

- **One row per session**: name, a one-line summary (written by Haiku in the background),
  directory, git branch, ticket, last activity, and whether it's open right now.
- **Resume in the right place**: `cairn` lists this project's sessions (the ones on your current
  branch first) and runs `claude --resume` in the directory the session belongs to.
- **A nudge before you duplicate work**: starting a new session on a branch that already has
  one shows a reminder with the command to resume it instead.
- **Search inside every transcript**, pin active work, archive finished work.
- **Sessions survive Claude Code's 30-day cleanup**: transcripts are kept and restored on resume.

Local only: one SQLite file in `~/.cairn`, a server on `127.0.0.1`. Standard-library Python.

## Install

```bash
brew install habibulloxon/tap/cairn
cairn install
```

`cairn install` backs up `~/.claude/settings.json`, then adds cairn's hooks and a status line
(see [What it changes](#what-it-changes)), and indexes the sessions you already have.
`cairn uninstall` removes all of it. To run from a checkout instead, see [LOCAL_SETUP.md](LOCAL_SETUP.md).

Needs [Claude Code](https://code.claude.com) logged in. Optional: [fzf](https://github.com/junegunn/fzf)
for the picker, [Ghostty](https://ghostty.org) 1.3+ (macOS) to open sessions in a new tab.

## Use

```bash
cairn                    # pick a session from this project and resume it in its directory
cairn auth bug           # pre-filtered; resumes directly if only one session matches
cairn 3f2a9c1e           # resume by id prefix (what the start-of-session reminder prints)
cairn -g                 # ...in a new Ghostty tab instead of this terminal
cairn -a / -d DIR        # every directory / another directory
cairn -p                 # print the `cd … && claude --resume …` command instead

cairn ls [query]         # table: ★ pinned  ▶ running  ! waiting on you  ● open  ⎇ on your branch
cairn ls -t              # grouped by ticket (DEV-1157)
cairn show 3f2a9c1e      # everything about one session: full summary, prompts, branches
cairn search webhook retry       # full-text search of every transcript, then pick one
cairn pin ID… / unpin ID…        # keep active work at the top
cairn archive ID… / unarchive    # hide finished work (it comes back on new activity)
cairn ui                 # dashboard at http://127.0.0.1:7444, filtered to this project

cairn summarize --dir ~/Code/app # summarize a project's sessions that have none or an old one
cairn summarize --missing        # every session without a summary (asks first; -y skips that)
cairn config ticket_url 'https://linear.app/acme/issue/{ticket}'   # make tickets links
```

"This project" is the git repo you're in (or the current directory outside git), including
subdirectories, plus sessions started in a multi-repo parent folder whose work was in this repo.

## How it works

| Claude Code hook | What cairn does |
|---|---|
| `SessionStart` | Marks the session open. On a fresh start, if this project has a session on the same feature branch (or one active in the last 24h), shows a reminder to resume it. |
| `UserPromptSubmit`, `PostToolUse` | Marks it running. |
| `PermissionRequest` | Marks it waiting on you. |
| `Stop` | Marks it idle; in the background, re-indexes it and refreshes the summary every 3 prompts or 10 minutes. |
| `SessionEnd` | Marks it closed and writes a final summary. |

Hooks return in about 50ms and never print errors into a session (they go to `~/.cairn/cairn.log`).
A session whose `claude` process exits without `SessionEnd` (a closed tab) is noticed by PID.
Opening a session that's already open elsewhere asks first, since two copies would write to one
transcript.

**Summaries** run `claude -p --model haiku --effort low` with no tools, MCP servers, hooks or
extended thinking, and `--no-session-persistence`, so they leave no transcript of their own. Each
takes a few seconds and counts against your Claude plan like any other request.

**Branches.** Sessions started in a folder holding several repos have no recorded branch. For
those, the summary call also gets the session's git commands and output, and Haiku names the
branch; the guess is kept only if that exact name appears in the git output, and it's shown as
`≈ DEV-1157`. **Tickets** come from the branch or title (`DEV-1157`, `dev-485-follow-up`).

**Transcripts survive cleanup.** Claude Code deletes transcripts after `cleanupPeriodDays`
(30 by default). cairn hard-links each one into `~/.cairn/transcripts`, which takes no extra
space while the original exists, and copies it back when you resume a deleted session.

## What it changes

- `~/.claude/settings.json`: six hook entries running `cairn hook`, and the status line, which
  becomes `cairn statusline --wrap '<your previous status line>'` (your line, then
  `▲ <session> · <ticket> · <id>`; skip with `cairn install --no-statusline`). Other settings and
  hooks are left alone, and a timestamped backup is written first.
- `~/.cairn/`: the database, transcript links, `config.json` and `cairn.log`.

## Privacy

Everything stays on your machine except the summary calls: those send an excerpt of the
session (the first prompt, the most recent part of the conversation and, for branch inference,
its git commands) to Anthropic through your own Claude Code login, the same way Claude Code
itself does. The dashboard server listens on `127.0.0.1` and refuses requests from other websites.

## Configuration

`cairn config` shows and sets `ticket_url`, `nudge` (true/false) and `nudge_hours` (24).
Environment: `CAIRN_HOME` (data, default `~/.cairn`), `CAIRN_MODEL` (default `haiku`),
`CAIRN_CLAUDE_DIR` (default `~/.claude`), `CAIRN_UI_DIST` (the built dashboard).

## Development

```
bin/cairn            launcher for running from a checkout
cairn/cli.py         commands and terminal output
cairn/hooks.py       hook handler and status line (run inside Claude Code)
cairn/install.py     editing ~/.claude/settings.json
cairn/transcripts.py reading Claude Code transcripts
cairn/index.py       syncing them into the database, search, transcript archive
cairn/sessions.py    queries, project scoping, tickets
cairn/summarize.py   Haiku summaries and branch inference
cairn/live.py        which sessions are open
cairn/server.py      dashboard server and JSON API
cairn/store.py       SQLite schema and migrations
tests/               python3 -m unittest discover -s tests -t .
scripts/release.sh   build, publish a GitHub release, update the Homebrew formula
```

The dashboard is a separate repo, [cairn-ui](https://github.com/habibulloxon/cairn-ui)
(React + shadcn/ui); releases bundle its build. [LOCAL_SETUP.md](LOCAL_SETUP.md) covers running
both from source.

## License

MIT
