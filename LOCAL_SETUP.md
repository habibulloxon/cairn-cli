# Running cairn locally (CLI + UI)

cairn is two repos that work together:

| Repo | What it is |
|---|---|
| [`cairn-cli`](https://github.com/habibulloxon/cairn-cli) | `cairn.py`: the indexer, Claude Code hooks, Haiku summaries, terminal picker, and the local server the dashboard talks to. Standard-library Python. |
| [`cairn-ui`](https://github.com/habibulloxon/cairn-ui) | The dashboard: React + shadcn/ui, built to static files that the CLI serves. |

## Requirements

- macOS (Linux works too, except *Open in Ghostty*)
- Python 3.10+ (no packages needed)
- Node 20+ and npm (only to build the UI)
- [Claude Code](https://code.claude.com) logged in (`claude` on your `PATH`); summaries run through it
- Optional: [fzf](https://github.com/junegunn/fzf) for the terminal picker, [Ghostty](https://ghostty.org) 1.3+ for *Open*

## 1. Clone both, UI inside the CLI

The CLI serves the dashboard from `./ui/dist`, so clone the UI into a `ui` folder inside the CLI
checkout (the CLI repo ignores `ui/`):

```bash
git clone https://github.com/habibulloxon/cairn-cli.git ~/Code/cairn
git clone https://github.com/habibulloxon/cairn-ui.git  ~/Code/cairn/ui
```

Prefer them side by side? Clone the UI anywhere and point the CLI at its build:
`export CAIRN_UI_DIST=~/Code/cairn-ui/dist` (in your shell profile).

## 2. Build the UI

```bash
cd ~/Code/cairn/ui
npm install
npm run build        # writes ui/dist, which `cairn serve` picks up
```

## 3. Install the CLI

```bash
chmod +x ~/Code/cairn/cairn.py
ln -sf ~/Code/cairn/cairn.py ~/.local/bin/cairn   # any directory on your PATH
cairn install
```

`cairn install` backs up `~/.claude/settings.json`, then adds cairn's hooks (SessionStart,
UserPromptSubmit, PermissionRequest, PostToolUse, Stop, SessionEnd) and wraps your status line
with a cairn line. It also indexes every existing transcript, so your history shows up right
away. Hooks run with the Python that ran `cairn install`; re-run it if you switch Pythons or move
the checkout. `cairn uninstall` removes everything it added (your data stays in `~/.cairn`).

Optional:

```bash
cairn summarize --missing -n 20      # backfill summaries for existing sessions (~4s each, uses your plan)
cairn config ticket_url 'https://linear.app/<team>/issue/{ticket}'   # make DEV-123 chips links
```

## 4. Use it

```bash
cairn               # pick a session from this project and resume it here
cairn -g            # ...in a new Ghostty tab
cairn ls            # table in the terminal
cairn search webhook retry   # search inside every transcript
cairn ui            # open the dashboard (starts the server on 127.0.0.1:7444)
```

Everything is local: data in `~/.cairn`, the server listens on `127.0.0.1` only.

## Working on the UI

```bash
cairn serve                  # API on 127.0.0.1:7444 (keep it running)
cd ~/Code/cairn/ui && npm run dev   # hot-reloading UI; /api is proxied to cairn serve
```

Run `npm run build` when you're done so `cairn ui` serves the new version.

## Updating

```bash
git -C ~/Code/cairn pull
git -C ~/Code/cairn/ui pull && (cd ~/Code/cairn/ui && npm install && npm run build)
cairn install        # only needed if the hooks changed
```
