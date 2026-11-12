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
        self.assertIn("格拉摩根的指挥官".encode(), page)
        self.assertIn("每 30 分钟自动续期".encode(), page)

    def test_add_account_from_article_link_once(self):
        from bitfeed import accounts
        page = {"biz": "OTk5OTk5OTk5OQ==", "nickname": "测试学院官方号", "body": "", "published_at": None,
                "title": "t", "canonical": ""}
        with mock.patch("bitfeed.article_page.fetch", return_value=page):
            self.assertEqual(ops_server.add_account("https://mp.weixin.qq.com/s/abcdefgh12"),
                             (200, {"added": "测试学院官方号"}))
            self.assertEqual(ops_server.add_account("https://mp.weixin.qq.com/s/abcdefgh12")[1],
                             {"exists": "测试学院官方号"})
        self.assertEqual(ops_server.add_account("https://example.com/x")[0], 400)
        extra = Path(self.tmp.name) / "weread" / "extra-accounts.json"
        merged = accounts.load(extra=extra)
        self.assertIn("MP_WXS_9999999999", {a["book_id"] for a in merged})
        self.assertEqual(json.loads(extra.read_text(encoding="utf-8"))["accounts"][0]["fakeid"], "OTk5OTk5OTk5OQ==")

    def test_login_start_is_single_flight(self):
        fake = mock.Mock()
        fake.poll.return_value = None
        with mock.patch.object(ops_server.subprocess, "Popen", return_value=fake) as popen:
            self.assertTrue(ops_server.start_login("weread"))
            self.assertFalse(ops_server.start_login("weread"))
        self.assertEqual(popen.call_count, 1)
        ops_server._running.clear()

    def test_healthy_logins_do_not_start_a_qr_job_or_replace_the_session(self):
        import time
        stamp=time.time()
        (ops_server.WEREAD/'scheduler.json').write_text(json.dumps({'session':{'state':'ok','checked_at':stamp}}))
        (ops_server.WECHAT/'keepalive.json').write_text(json.dumps({'state':'ok','checked_at':stamp,'expires_at':stamp+86400}))
        for kind,root in [('weread',ops_server.WEREAD),('mp',ops_server.WECHAT)]:
            session=root/'session.json';session.write_text('existing-session')
            with mock.patch.object(ops_server.subprocess,'Popen') as popen:
                status,body=self.get('/ops/'+TOKEN+'/login/'+kind,'POST')
                self.assertEqual((status,json.loads(body)),(200,{'started':False,'already_authorized':True}))
                popen.assert_not_called()
            self.assertEqual(session.read_text(),'existing-session')

    def test_preview_download_allows_only_the_named_public_documents(self):
        folder=ops_server.SITE/'weekly';folder.mkdir()
        (folder/'bit-preview-latest.docx').write_bytes(b'preview')
        self.assertEqual(self.get('/weekly/bit-preview-latest.docx'),(200,b'preview'))
        self.assertEqual(self.get('/weekly/../../weread/session.json')[0],404)


if __name__ == "__main__":
    unittest.main()
