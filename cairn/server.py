"""The dashboard server: the built UI plus a small JSON API, on 127.0.0.1 only."""

import json
import mimetypes
import re
import subprocess
import threading
import time
import urllib.request
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cairn import __version__, config, index, live, paths, sessions, store, summarize, terminal

SESSION_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class SummaryBatch:
    """One background batch at a time, started from the dashboard's "Summarize N" button."""

    _lock = threading.Lock()
    _state = {"running": False, "total": 0, "done": 0, "failed": 0}

    @classmethod
    def start(cls, ids):
        with cls._lock:
            if cls._state["running"]:
                return False
            cls._state = {"running": True, "total": len(ids), "done": 0, "failed": 0}

        def progress(_id, summary):
            with cls._lock:
                cls._state["done"] += 1
                cls._state["failed"] += not summary

        def run():
            try:
                summarize.summarize_many(ids, jobs=4, on_done=progress)
            finally:
                with cls._lock:
                    cls._state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return True

    @classmethod
    def state(cls):
        with cls._lock:
            return dict(cls._state)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ---------------------------------------------------------- responses

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False))

    def send_static(self, path):
        dist = paths.UI_DIST.resolve()
        index_html = dist / "index.html"
        if not index_html.exists():
            return self.send(503, f"Dashboard not built: no {index_html}. See LOCAL_SETUP.md.",
                             "text/plain; charset=utf-8")
        target = (dist / path.lstrip("/")).resolve()
        if not target.is_file() or dist not in target.parents:
            target = index_html  # "/", unknown routes, and anything outside dist/
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self.send(200, target.read_bytes(), ctype)

    def local_only(self):
        """Refuse other websites (Origin) and DNS rebinding (Host): only the dashboard may call the API."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        ok = host in LOCAL_HOSTS and (not origin or urlparse(origin).hostname in LOCAL_HOSTS)
        if not ok:
            self.send_json({"error": "forbidden"}, 403)
        return ok

    # ---------------------------------------------------------- routes

    def do_GET(self):
        if not self.local_only():
            return
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self.send_static(url.path)
        if url.path == "/api/health":
            return self.send_json({"app": "cairn", "version": __version__})
        if url.path == "/api/sessions":
            with closing(store.connect()) as conn:
                index.sync(conn)
                open_now = live.statuses(conn)
                ticket_url = config.load()["ticket_url"]
                rows = sessions.query(conn, archived=None)
                return self.send_json({"sessions": [sessions.to_json(r, open_now, ticket_url) for r in rows]})
        if url.path == "/api/search":
            text = (parse_qs(url.query).get("q") or [""])[0]
            with closing(store.connect()) as conn:
                hits = index.search(conn, text)
            return self.send_json({"hits": [{"id": k, "snippet": v} for k, v in hits.items()]})
        if url.path == "/api/summarize-batch":
            return self.send_json(SummaryBatch.state())
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.local_only():
            return
        url = urlparse(self.path)
        if url.path == "/api/summarize-batch":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self.send_json({"error": "bad json"}, 400)
            ids = [i for i in body.get("ids", []) if isinstance(i, str) and re.fullmatch(SESSION_ID, i)]
            started = bool(ids) and SummaryBatch.start(ids)
            return self.send_json({"started": started, **SummaryBatch.state()})

        m = re.fullmatch(rf"/api/(summarize|open|pin|archive)/({SESSION_ID})", url.path)
        if not m:
            return self.send_json({"error": "not found"}, 404)
        action, sid = m.groups()
        on = parse_qs(url.query).get("on") == ["1"]
        with closing(store.connect()) as conn:
            row = sessions.get(conn, sid)
            if not row:
                return self.send_json({"error": "no such session"}, 404)
            if action == "summarize":
                return self.send_json({"summary": summarize.summarize(conn, sid)})
            if action == "open":
                try:
                    terminal.open_in_ghostty(terminal.prepare_resume(conn, row), sid)
                except Exception as e:
                    return self.send_json({"ok": False, "error": str(e)})
            elif action == "pin":
                conn.execute("UPDATE sessions SET pinned=? WHERE id=?", (int(on), sid))
            elif action == "archive":
                conn.execute("UPDATE sessions SET archived_at=? WHERE id=?", (time.time() if on else None, sid))
            conn.commit()
        self.send_json({"ok": True})


def serve(port):
    """Serve until interrupted."""
    with closing(store.connect()) as conn:
        index.sync(conn)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def is_running(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
            return json.load(r).get("app") == "cairn"
    except Exception:
        return False


def start_background(port):
    """Start `cairn serve` detached and wait (up to 5s) until it answers. Returns True if it's up."""
    if is_running(port):
        return True
    paths.DATA.mkdir(parents=True, exist_ok=True)
    with open(paths.LOG_PATH, "a") as log_file:
        subprocess.Popen([*paths.self_argv(), "serve", "--port", str(port)],
                         stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                         start_new_session=True)
    for _ in range(50):
        if is_running(port):
            return True
        time.sleep(0.1)
    return False
