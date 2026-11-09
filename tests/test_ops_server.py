import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bitfeed import ops_server

TOKEN = "t0ken-for-tests-1234567890"


class OpsServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("site", "weread", "wechat"):
            (root / name).mkdir()
        (root / "site" / "data.json").write_text('{"schema_version":2}', encoding="utf-8")
        (root / "wechat" / "keepalive.json").write_text(json.dumps({"state": "ok", "expires_at": 1794590958}))
        self.patches = [mock.patch.object(ops_server, "SITE", root / "site"),
                        mock.patch.object(ops_server, "WEREAD", root / "weread"),
                        mock.patch.object(ops_server, "WECHAT", root / "wechat"),
                        mock.patch.object(ops_server, "LIVE", root / "live"),
                        mock.patch.dict(os.environ, {"BITFEED_OPS_TOKEN": TOKEN})]
        for patch in self.patches:
            patch.start()
        self.server = ops_server.ThreadingHTTPServer(("127.0.0.1", 0), ops_server.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def get(self, path, method="GET"):
        try:
            with urlopen(Request(self.base + path, method=method, data=b"" if method == "POST" else None), timeout=5) as r:
                return r.status, r.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_public_endpoints(self):
        self.assertEqual(self.get("/site-data.json"), (200, b'{"schema_version":2}'))
        status, body = self.get("/feed.json?days=7&limit=5")
        self.assertEqual((status, json.loads(body)["count"]), (200, 0))

    def test_ops_requires_exact_token(self):
        self.assertEqual(self.get("/ops/wrong-token-wrong-token-123/")[0], 404)
        self.assertEqual(self.get("/ops/" + TOKEN + "x/status.json")[0], 404)
        self.assertEqual(self.get("/ops/wrong-token-wrong-token-123/login/weread", "POST")[0], 404)
        status, body = self.get("/ops/" + TOKEN + "/status.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["mp"]["session"], "ok")
        status, page = self.get("/ops/" + TOKEN + "/")
        self.assertEqual(status, 200)
        self.assertIn("扫码登录微信读书".encode(), page)

    def test_login_start_is_single_flight(self):
        fake = mock.Mock()
        fake.poll.return_value = None
        with mock.patch.object(ops_server.subprocess, "Popen", return_value=fake) as popen:
            self.assertTrue(ops_server.start_login("weread"))
            self.assertFalse(ops_server.start_login("weread"))
        self.assertEqual(popen.call_count, 1)
        ops_server._running.clear()


if __name__ == "__main__":
    unittest.main()
