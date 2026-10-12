import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from cairn import install, paths, server
from cairn.hooks import HOOK_STATUS
from tests.helpers import TempHome

OTHER_HOOK = {"hooks": [{"type": "command", "command": "other-tool notify"}]}


class InstallTest(TempHome):
    def settings(self):
        return json.loads(paths.SETTINGS.read_text())

    def cairn_hooks(self):
        return [h["command"] for groups in self.settings().get("hooks", {}).values()
                for g in groups for h in g["hooks"] if install.is_cairn_hook(h)]

    def test_install_is_idempotent_and_uninstall_restores(self):
        original = {"model": "opus", "hooks": {"Stop": [OTHER_HOOK]},
                    "statusLine": {"type": "command", "command": "npx -y ccstatusline@latest"}}
        paths.SETTINGS.write_text(json.dumps(original))

        install.install()
        install.install()  # re-running replaces, never duplicates
        self.assertEqual(len(self.cairn_hooks()), len(HOOK_STATUS))
        self.assertIn(OTHER_HOOK, self.settings()["hooks"]["Stop"])
        line = self.settings()["statusLine"]["command"]
        self.assertEqual(install.unwrap_statusline(line), "npx -y ccstatusline@latest")

        install.uninstall()
        self.assertEqual(self.settings(), original)

    def test_replaces_legacy_cairn_py_entries(self):
        legacy = "/usr/bin/python3 /old/cairn/cairn.py"
        paths.SETTINGS.write_text(json.dumps({
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"{legacy} hook"}]}]},
            "statusLine": {"type": "command", "command": f"{legacy} statusline --wrap 'ccstatusline'"},
        }))
        install.install()
        self.assertFalse(any("/old/cairn" in c for c in self.cairn_hooks()))
        self.assertEqual(install.unwrap_statusline(self.settings()["statusLine"]["command"]), "ccstatusline")

    def test_homebrew_command(self):
        import os
        os.environ["CAIRN_BIN"] = "/opt/homebrew/bin/cairn"
        try:
            self.assertEqual(install.cairn_command(), "/opt/homebrew/bin/cairn")
        finally:
            del os.environ["CAIRN_BIN"]

    def test_refuses_to_overwrite_broken_settings(self):
        paths.SETTINGS.write_text("{not json")
        with self.assertRaises(ValueError):
            install.install()
        self.assertEqual(paths.SETTINGS.read_text(), "{not json")

    def test_not_cairn(self):
        self.assertFalse(install.is_cairn_hook({"command": "cairnish hook"}))
        self.assertIsNone(install.unwrap_statusline("npx ccstatusline"))


class ServerTest(TempHome):
    def setUp(self):
        super().setUp()
        paths.UI_DIST.mkdir(parents=True)
        (paths.UI_DIST / "index.html").write_text("<!doctype html>app")
        (paths.UI_DIST.parent / "secret.txt").write_text("outside dist")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def request(self, path, method="GET", headers=None, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_api_and_static(self):
        self.assertEqual(json.loads(self.request("/api/health")[1])["app"], "cairn")
        self.assertEqual(self.request("/api/sessions")[0], 200)
        self.assertEqual(self.request("/")[1], "<!doctype html>app")
        self.assertEqual(self.request("/../secret.txt")[1], "<!doctype html>app")
        self.assertTrue(server.is_running(self.port))

    def test_refuses_other_sites(self):
        self.assertEqual(self.request("/api/sessions", headers={"Host": "evil.example"})[0], 403)
        status, _ = self.request("/api/pin/aaaaaaaa-1111-4111-8111-111111111111?on=1", "POST",
                                 headers={"Origin": "https://evil.example"}, data=b"")
        self.assertEqual(status, 403)

    def test_rejects_bad_ids(self):
        self.assertEqual(self.request("/api/pin/not-an-id", "POST", data=b"")[0], 404)
        status, body = self.request("/api/summarize-batch", "POST", data=b'{"ids": ["../../x"]}',
                                    headers={"Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)["started"]), (200, False))


if __name__ == "__main__":
    unittest.main()
