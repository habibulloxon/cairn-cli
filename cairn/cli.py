"""The `cairn` command: argument parsing and everything printed to the terminal."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from urllib.parse import urlencode

from cairn import __version__, config, hooks, index, install, live, paths, server, sessions, store, summarize, terminal
from cairn.live import STATUS_GLYPH
from cairn.sessions import display_name, shown_branch
from cairn.util import ago, clip, log, plural, tilde

DEFAULT_PORT = 7444
LEGEND = "★ pinned  ▶ running  ! waiting  ● open  ⎇ current branch"


# ----------------------------------------------------------- helpers

def _scoped(conn, args, text):
    """(rows, root, hits) for go/ls/search: this project unless -a/-d, ranked or by search relevance."""
    root = None if args.all else sessions.normalize_dir(args.dir) if args.dir else sessions.current_scope()
    hits = index.search(conn, text) if args.deep and text else None
    text = None if hits is not None else text
    rows = sessions.query(conn, text, root=root, archived=args.archived, ids=hits)
    if root and not rows and not args.dir:
        print(f"No sessions in {tilde(root)}; showing all. (-d DIR to pick another directory)", file=sys.stderr)
        root = None
        rows = sessions.query(conn, text, archived=args.archived, ids=hits)
    if hits is not None:
        order = {sid: i for i, sid in enumerate(hits)}
        rows.sort(key=lambda r: order[r["id"]])
    else:
        rows = sessions.ranked(rows, sessions.current_branch() if root else None)
    return rows, root, hits


def _flags(row, open_now, branch):
    return (("★" if row["pinned"] else " ") + STATUS_GLYPH.get(open_now.get(row["id"]), " ")
            + ("⎇" if sessions.on_branch(row, branch) else " "))


def _plain_snippet(snippet, mark=("", "")):
    return snippet.replace("\x02", mark[0]).replace("\x03", mark[1])


def _confirm(question):
    """y/N from the terminal; False when there's no terminal to ask."""
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _pick(rows, root, hits, open_now, query):
    """Let the user choose one session: fzf if installed, else a numbered list."""
    if len(rows) == 1 and query:
        return rows[0]  # the query named it
    branch = sessions.current_branch() if root else None

    def line(r, name_width):
        return (f"{_flags(r, open_now, branch)} {ago(r['last_active']):>8}  "
                f"{clip(display_name(r), name_width):<{name_width}}  "
                f"{clip(shown_branch(r) or '-', 20):<20}  {tilde(r['cwd'])}")

    if shutil.which("fzf") and sys.stdin.isatty():
        def preview(r):
            if hits:
                return _plain_snippet(hits[r["id"]], ("»", "«"))
            return r["summary"] or clip(r["last_prompt"], 200)

        proc = subprocess.run(
            ["fzf", "--delimiter=\t", "--with-nth=2", "--no-sort", "--height=60%", "--reverse",
             f"--prompt={tilde(root) + ' ' if root else ''}session> ", f"--header={LEGEND}",
             "--preview=echo {3} | fold -s -w $FZF_PREVIEW_COLUMNS", "--preview-window=down,4,wrap"],
            input="\n".join(f"{r['id']}\t{line(r, 44)}\t{preview(r)}" for r in rows),
            capture_output=True, text=True,
        )
        chosen = proc.stdout.split("\t", 1)[0] if proc.returncode == 0 else ""
        return next((r for r in rows if r["id"] == chosen), None)

    shown = rows[:30]
    for i, r in enumerate(shown, 1):
        print(f"{i:>2}. {line(r, 48)}")
    try:
        choice = input("number> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return shown[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(shown) else None


# ----------------------------------------------------------- commands

def cmd_go(args, conn):
    """Pick a session and resume it in its own directory (or a new Ghostty tab)."""
    query = " ".join(args.query)
    rows, root, hits = _scoped(conn, args, query)
    if not rows:
        sys.exit("No matching sessions.")
    open_now = live.statuses(conn)
    chosen = _pick(rows, root, hits, open_now, query)
    if not chosen:
        return
    if args.print:
        print(sessions.resume_command(chosen))
        return
    name = display_name(chosen)
    status = open_now.get(chosen["id"])
    if status:
        # Two copies of one session both append to the same transcript.
        print(f"'{name}' is already open in another terminal ({status}).", file=sys.stderr)
        if not _confirm("Two copies would both write to the same transcript. Open anyway?"):
            return
    try:
        cwd = terminal.prepare_resume(conn, chosen)
    except RuntimeError as e:
        sys.exit(f"Can't resume '{name}': {e}")
    if chosen["missing"]:
        print("Restored the transcript from cairn's archive.", file=sys.stderr)
    if args.ghostty:
        try:
            terminal.open_in_ghostty(cwd, chosen["id"])
        except RuntimeError as e:
            sys.exit(f"Couldn't open Ghostty: {e}")
        print(f"Opened '{name}' in a new Ghostty tab.", file=sys.stderr)
        return
    os.chdir(cwd)
    print(f"→ {name}  ({tilde(cwd)})", file=sys.stderr)
    os.execvp("claude", ["claude", "--resume", chosen["id"]])


def cmd_ls(args, conn):
    rows, root, hits = _scoped(conn, args, " ".join(args.query))
    rows = rows[: args.limit] if args.limit else rows
    open_now = live.statuses(conn)
    if args.json:
        ticket_url = config.load()["ticket_url"]
        print(json.dumps([sessions.to_json(r, open_now, ticket_url) for r in rows], indent=2, ensure_ascii=False))
        return
    branch = sessions.current_branch() if root else None

    def show(r):
        print(f"{_flags(r, open_now, branch)} {r['id'][:8]}  {ago(r['last_active']):>8}  "
              f"{clip(display_name(r), 46):<46}  {clip(shown_branch(r) or '-', 22):<22}  {tilde(r['cwd'])}")
        detail = _plain_snippet(hits[r["id"]]) if hits else r["summary"]
        if detail:
            print(f"{'':14}{clip(detail, 120)}")

    if args.tickets:
        groups = {}
        for r in rows:
            groups.setdefault(sessions.ticket_of(r) or "(no ticket)", []).append(r)
        for ticket, group in groups.items():
            print(f"\n{ticket}  ({plural(len(group), 'session')})")
            for r in group:
                show(r)
    else:
        for r in rows:
            show(r)
    where = f" in {tilde(root)}" if root else ""
    print(f"\n{plural(len(rows), 'session')}{where} · {LEGEND} · -a all dirs", file=sys.stderr)


def cmd_search(args, conn):
    args.deep = True
    if args.list:
        args.limit, args.json, args.tickets = 30, False, False
        return cmd_ls(args, conn)
    args.print = False
    return cmd_go(args, conn)


def cmd_show(args, conn):
    """Everything about one session, untruncated."""
    r = sessions.get(conn, sessions.resolve_id(conn, args.id))
    status = live.statuses(conn).get(r["id"])
    earlier = ", ".join(b for b in json.loads(r["branches"] or "[]") if b != r["branch"])

    def wrap(text):
        return textwrap.fill(" ".join(str(text).split()), 100, initial_indent="  ", subsequent_indent="  ")

    print(f"{display_name(r)}{'  ★' if r['pinned'] else ''}{f'  [{status}]' if status else ''}")
    print(f"  {r['id']}")
    for label, value in (
        ("Directory", tilde(r["cwd"])),
        ("Branch", shown_branch(r) or "-"),
        ("Earlier", earlier),
        ("Ticket", sessions.ticket_of(r)),
        ("PR", r["pr_url"]),
        ("Active", f"{ago(r['last_active'])} · started {ago(r['started_at'])} · {plural(r['turns'], 'prompt')}"),
    ):
        if value:
            print(f"  {label + ':':<10} {value}")
    summary = r["summary"] or f"(none yet: cairn summarize {r['id'][:8]})"
    for label, text in (("Summary", summary), ("First prompt", r["first_prompt"]), ("Last prompt", r["last_prompt"])):
        if text:
            print(f"\n{label}\n{wrap(text)}")
    print(f"\nResume\n  {sessions.resume_command(r)}")


def cmd_mark(args, conn):
    """pin / unpin / archive / unarchive."""
    for prefix in args.ids:
        sid = sessions.resolve_id(conn, prefix)
        if args.cmd in ("pin", "unpin"):
            conn.execute("UPDATE sessions SET pinned=? WHERE id=?", (int(args.cmd == "pin"), sid))
        else:
            conn.execute("UPDATE sessions SET archived_at=? WHERE id=?",
                         (time.time() if args.cmd == "archive" else None, sid))
        print(f"{args.cmd}: {display_name(sessions.get(conn, sid))}")
    conn.commit()


def cmd_summarize(args, conn):
    if args.ids:
        rows = [sessions.get(conn, sessions.resolve_id(conn, i)) for i in args.ids]
        fresh = True
    else:
        rows = sessions.query(conn, archived=None)
        if args.dir:
            root = sessions.normalize_dir(args.dir)
            rows = [r for r in rows if sessions.in_dir(r, root, args.exact)]
        fresh = args.force
        if not fresh:
            rows = [r for r in rows if summarize.is_stale(r)]  # skip summaries that are up to date
        if args.missing:
            rows = [r for r in rows if not r["summary"]]
    rows = [r for r in rows if not r["missing"] and r["turns"]]
    rows = rows[: args.limit] if args.limit else rows
    if not rows:
        print("Nothing to summarize.")
        return
    # Ask first, unless it's one session named explicitly or -y was given.
    if not args.yes and not (args.ids and len(rows) == 1):
        print(summarize.plan_text(len(rows), args.jobs))
        if not sys.stdin.isatty():
            sys.exit("Not a terminal, so not asking: re-run with -y to go ahead.")
        if not _confirm("Continue?"):
            print("Cancelled.")
            return

    print(f"Summarizing {plural(len(rows), 'session')} with {summarize.MODEL} ({args.jobs} at a time)…")
    by_id = {r["id"]: r for r in rows}
    done = 0

    def report(session_id, summary):
        nonlocal done
        done += 1
        r = by_id[session_id]
        print(f"[{done}/{len(rows)}] {clip(display_name(r), 50)}  ({tilde(r['cwd'])})\n"
              f"    {summary or f'(failed or already running; see {tilde(str(paths.LOG_PATH))})'}", flush=True)

    ok, failed = summarize.summarize_many(list(by_id), args.jobs, fresh, report)
    print(f"Done: {ok} summarized, {failed} failed.")


def cmd_ui(args, conn):
    """Open the dashboard, starting the server in the background if needed."""
    import webbrowser

    if not server.start_background(args.port):
        sys.exit(f"The dashboard server didn't start on port {args.port}; see {tilde(str(paths.LOG_PATH))}.")
    root = None if args.all else sessions.normalize_dir(args.dir) if args.dir else sessions.current_scope()
    params = {k: v for k, v in (("dir", root), ("branch", root and sessions.current_branch())) if v}
    url = f"http://127.0.0.1:{args.port}/" + (f"?{urlencode(params)}" if params else "")
    webbrowser.open(url)
    print(url)


def cmd_serve(args, conn):
    print(f"cairn dashboard on http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    server.serve(args.port)


def cmd_config(args, conn):
    if not args.key:
        print(json.dumps(config.load(), indent=2))
    elif args.value is None:
        if args.key not in config.DEFAULTS:
            sys.exit(f"Unknown key {args.key}. Keys: {', '.join(config.DEFAULTS)}")
        print(config.load()[args.key])
    else:
        try:
            print(f"{args.key} = {json.dumps(config.set_value(args.key, args.value))}")
        except KeyError:
            sys.exit(f"Unknown key {args.key}. Keys: {', '.join(config.DEFAULTS)}")


def cmd_sync(args, conn):
    print(f"{plural(index.sync(conn), 'session')} updated")


def cmd_install(args, conn):
    try:
        backup = install.install(statusline=not args.no_statusline)
    except ValueError as e:
        sys.exit(str(e))
    if backup:
        print(f"Backup: {backup}")
    print(f"Added cairn hooks ({', '.join(hooks.HOOK_STATUS)}) to {paths.SETTINGS}")
    print(f"They run: {install.cairn_command()} hook")
    if not args.no_statusline:
        print("Status line: cairn line added under your existing one")
    print(f"Indexed {plural(index.sync(conn), 'session')} into {paths.DB_PATH}")


def cmd_uninstall(args, conn):
    try:
        backup = install.uninstall()
    except ValueError as e:
        sys.exit(str(e))
    if backup:
        print(f"Backup: {backup}")
    print(f"Removed cairn's hooks and status line from {paths.SETTINGS} (your data stays in {paths.DATA})")


# Internal commands, run by Claude Code: never let an error reach the session.

def cmd_hook(args, conn):
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    output = hooks.handle(payload)
    if output:
        print(output)


def cmd_statusline(args, conn):
    print(hooks.statusline(sys.stdin.read(), args.wrap))


def cmd_update(args, conn):
    hooks.update(args.path, args.event)


INTERNAL = {"hook", "statusline", "_update"}   # run by Claude Code; they open the database themselves
NO_DB = INTERNAL | {"serve", "ui", "config", "uninstall"}
NO_SYNC = {"sync", "install", "pin", "unpin", "archive", "unarchive"}


# ----------------------------------------------------------- arguments

def build_parser():
    p = argparse.ArgumentParser(prog="cairn", description="Find the right Claude Code session and hop back into it.")
    p.add_argument("--version", action="version", version=f"cairn {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def scope_args(s):
        s.add_argument("-a", "--all", action="store_true", help="sessions from every directory, not just this project")
        s.add_argument("-d", "--dir", help="sessions from this directory (and below) instead of the current project")
        s.add_argument("--archived", action="store_true", help="show archived sessions instead")

    s = sub.add_parser("go", help="pick a session and resume it in its directory (the default command)")
    s.add_argument("query", nargs="*")
    s.add_argument("-p", "--print", action="store_true", help="print the resume command instead of running it")
    s.add_argument("-g", "--ghostty", action="store_true", help="open it in a new Ghostty tab")
    s.add_argument("-s", "--deep", action="store_true", help="match the query against full transcripts")
    scope_args(s)
    s.set_defaults(fn=cmd_go)

    s = sub.add_parser("ls", help="list sessions: pinned, current branch, then newest")
    s.add_argument("query", nargs="*")
    s.add_argument("-n", "--limit", type=int, default=30)
    s.add_argument("-t", "--tickets", action="store_true", help="group by ticket (DEV-123)")
    s.add_argument("-s", "--deep", action="store_true", help="match the query against full transcripts")
    s.add_argument("--json", action="store_true")
    scope_args(s)
    s.set_defaults(fn=cmd_ls)

    s = sub.add_parser("search", help="full-text search of transcripts, then pick one to resume")
    s.add_argument("query", nargs="+")
    s.add_argument("-l", "--list", action="store_true", help="just list matches with snippets")
    s.add_argument("-g", "--ghostty", action="store_true", help="open the pick in a new Ghostty tab")
    scope_args(s)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("show", help="everything about one session: full summary, prompts, branches")
    s.add_argument("id", help="session id or unique prefix")
    s.set_defaults(fn=cmd_show)

    for name, hlp in (("pin", "keep sessions at the top"), ("unpin", "unpin sessions"),
                      ("archive", "hide finished sessions (they come back on new activity)"),
                      ("unarchive", "bring archived sessions back")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("ids", nargs="+", help="session ids or unique prefixes")
        s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("summarize", help="summarize sessions with Haiku",
                       description="With no ids, summarizes every session whose summary is missing or "
                                   "out of date. Asks before starting unless -y.")
    s.add_argument("ids", nargs="*", help="specific session ids or prefixes (always re-summarized)")
    s.add_argument("-d", "--dir", help="only sessions started in this directory or below it")
    s.add_argument("--exact", action="store_true", help="with --dir: that directory only, not subdirectories")
    s.add_argument("--missing", action="store_true", help="only sessions with no summary at all")
    s.add_argument("-f", "--force", action="store_true", help="rewrite summaries from scratch even if up to date")
    s.add_argument("-j", "--jobs", type=int, default=4, help="parallel Haiku calls (default 4)")
    s.add_argument("-n", "--limit", type=int)
    s.add_argument("-y", "--yes", action="store_true", help="don't ask first")
    s.set_defaults(fn=cmd_summarize)

    s = sub.add_parser("ui", help="open the dashboard (starts the server if needed)")
    scope_args(s)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(fn=cmd_ui)

    s = sub.add_parser("serve", help="run the dashboard server in the foreground")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("config", help="show or set options: " + ", ".join(config.DEFAULTS))
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(fn=cmd_config)

    sub.add_parser("sync", help="re-index transcripts now").set_defaults(fn=cmd_sync)
    s = sub.add_parser("install", help="add hooks and the status line to ~/.claude/settings.json")
    s.add_argument("--no-statusline", action="store_true")
    s.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove cairn's hooks and status line").set_defaults(fn=cmd_uninstall)

    sub.add_parser("hook", help=argparse.SUPPRESS).set_defaults(fn=cmd_hook)
    s = sub.add_parser("statusline", help=argparse.SUPPRESS)
    s.add_argument("--wrap")
    s.set_defaults(fn=cmd_statusline)
    s = sub.add_parser("_update", help=argparse.SUPPRESS)
    s.add_argument("path")
    s.add_argument("event", nargs="?", default="")
    s.set_defaults(fn=cmd_update)
    return p, sub


def main(argv=None):
    parser, sub = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or (argv[0] not in sub.choices and argv[0] not in ("-h", "--help", "--version")):
        argv = ["go", *argv]  # `cairn`, `cairn -a`, `cairn auth bug` all mean `cairn go …`
    args = parser.parse_args(argv)

    if args.cmd in INTERNAL:
        try:
            args.fn(args, None)
        except Exception as e:
            log(f"{args.cmd}: {e!r}")  # never surface errors inside a Claude session
        return
    if args.cmd in NO_DB:
        return args.fn(args, None)

    conn = store.connect()
    try:
        if args.cmd not in NO_SYNC:
            index.sync(conn)
        args.fn(args, conn)
    except sessions.NotFound as e:
        sys.exit(str(e))
    finally:
        conn.close()
