import tempfile
import time
import unittest
from unittest.mock import Mock

from bitfeed import peer_unions
from bitfeed.store import Store


class PeerUnionTest(unittest.TestCase):
    def test_search_accepts_only_matching_publishers_and_recent_post_dates(self):
        account = {"name": "兄弟高校·校研会", "fakeid": "MjM5MTAwMjgwOQ=="}
        now = int(time.time())
        url = "https://mp.weixin.qq.com/s?__biz=MjM5MTAwMjgwOQ%3D%3D&mid=12345&idx=1"
        post = {"title": "<em>活动</em>报名", "date": now - 86400, "doc_url": url}
        items = [post, post, dict(post, doc_url=url.replace("MjM5", "MTM5")),
                 dict(post, date=now + 60), dict(post, date=now - 60 * 86400)]
        session = Mock()
        session.call.return_value = {"ret": -1, "content": {"ret": 0, "data": [{"items": items}]}}
        found = peer_unions.search_articles(session, account, now)
        self.assertEqual([x["title"] for x in found], ["活动报名"])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            self.assertEqual(store.import_peer_search(found), 1)
            self.assertEqual(store.import_peer_search(found), 0)
            self.assertIsNotNone(store.peer_cover_match("https://mp.weixin.qq.com/s/shortlink", account["name"], "活动报名", now))

    def test_audience_requires_explicit_evidence(self):
        classify = peer_unions.eligibility
        self.assertEqual(classify("与北京各高校交流。"), "外校资格待确认")
        self.assertEqual(classify("报名仅限本校在读研究生。"), "限主办校")
        self.assertIn("面向外校", classify("欢迎外校同学报名。"))
        self.assertIn("不同环节", classify("欢迎外校同学报名。比赛仅限我校学生。"))
        self.assertIn("所列学校", classify("报名对象：主办及协办学校研究生。"))
        self.assertEqual(classify("不欢迎外校同学报名。"), "外校资格待确认")
