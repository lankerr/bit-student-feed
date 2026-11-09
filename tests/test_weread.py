import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from bitfeed import article_page
from bitfeed.weread import (WeReadError, WeReadSession, article_url, book_id_from_fakeid,
                            fakeid_from_book_id, parse_articles)


class Response:
    def __init__(self, payload=None, status=200, cookies=(), text=None):
        self.status_code, self.payload = status, payload
        self.cookies = [SimpleNamespace(name=k, value=v) for k, v in cookies]
        self.text = text if text is not None else json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        if self.payload is None:
            raise ValueError()
        return self.payload


class HTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kwargs):
        path = url.split("weread.qq.com", 1)[1]
        self.calls.append((method, path, kwargs.get("headers", {}).get("Cookie", "")))
        answer = self.routes[path]
        if callable(answer):
            return answer(kwargs.get("params") or {})
        return answer.pop(0) if isinstance(answer, list) else answer

    def close(self):
        pass


class WeReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.json"
        self.path.write_text(json.dumps({"cookies": {"wr_vid": "123", "wr_skey": "old", "wr_rt": "web%40x", "wr_ql": "1"}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_fakeid_and_book_id_round_trip(self):
        self.assertEqual(book_id_from_fakeid("Mzg5ODcwMTAyMA=="), "MP_WXS_3898701020")
        self.assertEqual(fakeid_from_book_id("MP_WXS_3898701020"), "Mzg5ODcwMTAyMA==")
        with self.assertRaises(ValueError):
            book_id_from_fakeid("not-base64!")

    def test_article_url_restores_underscores_from_review_id(self):
        url = article_url(review_id="MP_WXS_3898701020_OECV7K2xG9GXr3g5l~mg", book_id="MP_WXS_3898701020")
        self.assertEqual(url, "https://mp.weixin.qq.com/s/OECV7K2xG9GXr3g5l_mg")
        with self.assertRaises(ValueError):
            article_url(review_id="MP_WXS_1_<script>")

    def test_parse_articles_expands_multi_article_pushes(self):
        payload = {"reviews": [{"createTime": 1700000000, "subReviews": [
            {"review": {"reviewId": "MP_WXS_1_aaaaaaaa1", "mpInfo": {"title": "一", "originalId": "aaaaaaaa1", "time": 1700000001}}},
            {"review": {"reviewId": "MP_WXS_1_bbbbbbbb2", "mpInfo": {"title": "二", "originalId": "bbbbbbbb2"}}}]}]}
        articles, groups = parse_articles(payload, "MP_WXS_1")
        self.assertEqual(groups, 1)
        self.assertEqual([a["title"] for a in articles], ["一", "二"])
        self.assertEqual(articles[1]["published_at"], 1700000000)

    def test_expired_key_is_renewed_once_and_request_replayed(self):
        http = HTTP({"/api/mp/cover": [Response({"errCode": -2012}), Response({"reviewId": "MP_WXS_1_abcdefgh1", "title": "新"})],
                     "/web/login/renewal": Response({"succ": 1}, cookies=[("wr_skey", "fresh")])})
        session = WeReadSession.load(self.path, http=http)
        self.assertEqual(session.cover("MP_WXS_1")["title"], "新")
        self.assertEqual([c[1] for c in http.calls], ["/api/mp/cover", "/web/login/renewal", "/api/mp/cover"])
        self.assertIn("wr_skey=fresh", http.calls[2][2])
        self.assertEqual(json.loads(self.path.read_text())["cookies"]["wr_skey"], "fresh")

    def test_refused_renewal_means_login_expired(self):
        http = HTTP({"/api/mp/cover": Response({"errCode": -2012}), "/web/login/renewal": Response({"errCode": -2013})})
        with self.assertRaises(WeReadError) as caught:
            WeReadSession.load(self.path, http=http).cover("MP_WXS_1")
        self.assertEqual(caught.exception.state, "auth_expired")

    def test_renewed_key_still_refused_is_treated_as_quota(self):
        http = HTTP({"/api/mp/cover": Response({"errCode": -2012}), "/web/login/renewal": Response({"succ": 1})})
        with self.assertRaises(WeReadError) as caught:
            WeReadSession.load(self.path, http=http).cover("MP_WXS_1")
        self.assertEqual(caught.exception.state, "rate_limited")

    def test_retired_list_endpoint_is_reported_as_unsupported(self):
        http = HTTP({"/web/mp/articles": Response({"errCode": -2041})})
        with self.assertRaises(WeReadError) as caught:
            WeReadSession.load(self.path, http=http).articles("MP_WXS_1")
        self.assertEqual(caught.exception.state, "unsupported")

    def test_missing_session_is_auth_missing(self):
        with self.assertRaises(WeReadError) as caught:
            WeReadSession.load(Path(self.tmp.name) / "none.json")
        self.assertEqual(caught.exception.state, "auth_missing")


class ArticlePageTest(unittest.TestCase):
    def test_content_noencode_body_and_publish_time(self):
        page = ("<script>var ct = \"1762740000\";var msg_title = '讲座通知';"
                "var msg_link = \"http://mp.weixin.qq.com/s?__biz=X&amp;mid=1\";"
                "window.x={content_noencode: '报名截止：2026年11月20日\\x0a地点：良乡'};</script>")
        parsed = article_page.parse(page)
        self.assertEqual(parsed["published_at"], 1762740000)
        self.assertIn("2026年11月20日", parsed["body"])
        self.assertIn("地点：良乡", parsed["body"])
        self.assertEqual(parsed["title"], "讲座通知")

    def test_js_content_fallback(self):
        page = '<div class="x" id="js_content" style=""><p>第一段</p><p>第二段&nbsp;内容</p></div><script>var ct = "1762740000";</script>'
        self.assertEqual(article_page.parse(page)["body"], "第一段\n第二段 内容")

    def test_verification_page_is_blocked(self):
        with self.assertRaises(article_page.PageError) as caught:
            article_page.parse("<title>环境异常</title><p>完成验证后即可继续访问</p>")
        self.assertEqual(caught.exception.state, "blocked")


if __name__ == "__main__":
    unittest.main()
