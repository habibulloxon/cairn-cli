# Running cairn from source (CLI + UI)

Most people should install with Homebrew (`brew install habibulloxon/tap/cairn`). This guide is
for working on cairn itself. It's two repos that work together:

| Repo | What it is |
|---|---|
| [`cairn-cli`](https://github.com/habibulloxon/cairn-cli) | The `cairn` command: indexer, Claude Code hooks, Haiku summaries, terminal picker, and the local server the dashboard talks to. Standard-library Python. |
| [`cairn-ui`](https://github.com/habibulloxon/cairn-ui) | The dashboard: React + shadcn/ui, built to static files that the CLI serves. |

## Requirements

- macOS or Linux (*Open in Ghostty* is macOS only)
- Python 3.9+ (no packages needed)
- Node 20+ and npm, to build the UI
- [Claude Code](https://code.claude.com) logged in (`claude` on your `PATH`); summaries run through it
- Optional: [fzf](https://github.com/junegunn/fzf) for the picker, [Ghostty](https://ghostty.org) 1.3+ for *Open*

## 1. Clone both, UI inside the CLI

The CLI serves the dashboard from `./ui/dist`, so clone the UI into a `ui` folder inside the CLI
checkout (the CLI repo ignores `ui/`):

```bash
git clone https://github.com/habibulloxon/cairn-cli.git ~/Code/cairn
git clone https://github.com/habibulloxon/cairn-ui.git  ~/Code/cairn/ui
```

Prefer them side by side? Clone the UI anywhere and point the CLI at its build with
`export CAIRN_UI_DIST=~/Code/cairn-ui/dist` in your shell profile.

## 2. Build the UI

```bash
cd ~/Code/cairn/ui
npm install
npm run build        # writes ui/dist, which `cairn serve` picks up
```

## 3. Put `cairn` on your PATH and install the hooks

```bash
ln -sf ~/Code/cairn/bin/cairn ~/.local/bin/cairn   # any directory on your PATH
cairn install
```

`cairn install` backs up `~/.claude/settings.json`, then adds the hooks and status line, which run
`<the Python you ran it with> ~/Code/cairn/bin/cairn`. Re-run it if you move the checkout or
switch Pythons; it replaces its old entries. `cairn uninstall` removes everything it added (your
data stays in `~/.cairn`). Don't have both this and the Homebrew version installed: whichever ran
`cairn install` last owns the hooks.

Optional:

```bash
cairn summarize --missing -n 20      # backfill summaries for existing sessions (asks first)
cairn config ticket_url 'https://linear.app/<team>/issue/{ticket}'
```

## 4. Develop

```bash
python3 -m unittest discover -s tests -t .   # CLI tests; they never touch ~/.claude or ~/.cairn

cairn serve                         # API on 127.0.0.1:7444 (keep it running)
cd ~/Code/cairn/ui && npm run dev   # hot-reloading UI; /api is proxied to cairn serve
```

Run `npm run build` when you're done so `cairn ui` serves the new version.

## Updating

```bash
git -C ~/Code/cairn pull
git -C ~/Code/cairn/ui pull && (cd ~/Code/cairn/ui && npm install && npm run build)
cairn install        # only needed if the hooks changed
```

## Releasing

Bump `__version__` in `cairn/__init__.py`, commit, then run `scripts/release.sh`. It runs the
tests, builds the UI, uploads `cairn-<version>.tar.gz` (CLI + built dashboard) as a GitHub
release, and updates `Formula/cairn.rb` in the tap (cloned at `../homebrew-tap`, or `$TAP_DIR`).
