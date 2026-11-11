import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from bitfeed import alerts, digest
from bitfeed.domain import TZ
from bitfeed.store import Store
from bitfeed.subscribe import Worker, ping_session
from test_weread import HTTP, Response

ACCOUNTS = {"accounts": [
    {"name": "北理就业", "fakeid": "Mzg5ODcwMTAyMA==", "tier": 0},
    {"name": "研会", "fakeid": "MjM5MTAwMjgwOQ==", "tier": 1},
    {"name": "民间", "fakeid": "MzIzODgyOTI5Mw==", "tier": 3}]}
JOB = "MP_WXS_3898701020"
GSU = "MP_WXS_2391002809"


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.weread = root / "weread"
        self.weread.mkdir()
        (self.weread / "session.json").write_text(json.dumps({"cookies": {"wr_vid": "1", "wr_skey": "k", "wr_rt": "r"}}))
        self.registry = root / "accounts.json"
        self.registry.write_text(json.dumps(ACCOUNTS, ensure_ascii=False), encoding="utf-8")
        self.store = Store(root / "data")
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, routes):
        self.http = HTTP(routes)
        return Worker(self.store, self.weread, self.registry, http=self.http, clock=self.clock,
                      sleep=self.clock.sleep, gap=(50, 50), body_gap=(25, 25))

    def state(self):
        return json.loads((self.weread / "scheduler.json").read_text(encoding="utf-8"))

    def test_follows_then_polls_and_imports_each_new_article_once(self):
        cover = {JOB: Response({"reviewId": JOB + "_OECV7K2xG9GXr3g5l~mg", "title": "宣讲会"}),
                 GSU: Response({"reviewId": GSU + "_abcdefgh12", "title": "研会活动"})}
        by_book = lambda params: cover[params["bookId"]]
        routes = {"/web/shelf/sync": Response({"books": [{"bookId": JOB}], "synckey": 1}),
                  "/web/shelf/add": Response({"succ": 1}), "/api/mp/cover": by_book}
        first = self.worker(routes).run(calls=3, bodies=0)
        self.assertEqual(first["followed"], 1)
        self.assertEqual(first["checked"], 1)
        self.assertEqual([c[1] for c in self.http.calls], ["/web/shelf/sync", "/web/shelf/add", "/api/mp/cover"])
        self.assertEqual(self.state()["accounts"][JOB]["last_title"], "宣讲会")
        self.assertNotIn("MP_WXS_3238829295", self.state()["accounts"])
        self.clock.now += 4 * 3600
        again = self.worker({"/api/mp/cover": by_book}).run(calls=2, bodies=0)
        self.assertEqual((again["checked"], again["new"]), (2, 1))
        self.assertEqual(self.state()["accounts"][JOB]["new_total"], 1)
        rows = self.store.missing_bodies("weread")
        self.assertEqual(sorted(r["url"] for r in rows), ["https://mp.weixin.qq.com/s/OECV7K2xG9GXr3g5l_mg",
                                                          "https://mp.weixin.qq.com/s/abcdefgh12"])
        self.assertEqual({r["publication_status"] for r in rows}, {"unknown"})

    def test_picture_post_stops_consuming_body_slots(self):
        now = int(self.clock.now)
        self.store.import_articles([{
            "url": "https://mp.weixin.qq.com/s/picturepost01", "title": "只有图片的讲座",
            "source": "经济学院", "published_at": now, "publication_status": "unknown",
            "kind": "wechat", "origin": "weread", "body_status": "title_only"}])
        worker = self.worker({})
        worker.state = worker._load_state()
        worker.body_gap = (0, 0)
        empty = {"body": "", "published_at": now, "title": "只有图片的讲座", "canonical": ""}
        with mock.patch("bitfeed.article_page.fetch", return_value=empty) as fetch:
            for _ in range(3):
                worker._fetch_bodies(2, {"bodies": 0})
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(self.store.missing_bodies("weread"), [])
        with self.store.connection() as conn:
            status = conn.execute("SELECT body_status FROM articles").fetchone()["body_status"]
        self.assertEqual(status, "no_text")

    def test_quota_answer_sets_persistent_backoff(self):
        routes = {"/web/shelf/sync": Response({"books": [{"bookId": JOB}, {"bookId": GSU}]}),
                  "/api/mp/cover": Response({"errCode": -2014})}
        result = self.worker(routes).run(calls=3, bodies=0)
        self.assertEqual(result["stopped"], "rate_limited")
        limits = self.state()["limits"]
        self.assertGreater(limits["cooldown_until"], self.clock.now)
        calls = len(self.http.calls)
        self.assertEqual(self.worker(routes).run()["stopped"], "cooldown")
        self.assertEqual(len(self.http.calls), 0)
        self.assertEqual(calls, 2)

    def test_expired_login_stops_and_is_reported(self):
        routes = {"/web/shelf/sync": Response({"errCode": -2012}), "/web/login/renewal": Response({"errCode": -2013})}
        result = self.worker(routes).run(calls=3, bodies=0)
        self.assertEqual(result["stopped"], "auth_expired")
        self.assertEqual(self.state()["session"]["state"], "auth_expired")
        found = alerts.conditions(self.weread, Path(self.tmp.name) / "wechat")
        self.assertIn("weread_login", found)

    def test_paces_calls_and_attaches_page_body_with_real_date(self):
        routes = {"/web/shelf/sync": Response({"books": [{"bookId": JOB}, {"bookId": GSU}]}),
                  "/api/mp/cover": Response({"reviewId": JOB + "_OECV7K2xG9GXr3g5l~mg", "title": "讲座报名"})}
        page = {"body": "报名截止：2026年11月20日", "published_at": 1762740000, "title": "讲座报名", "canonical": ""}
        start = self.clock.now
        with mock.patch("bitfeed.article_page.fetch", return_value=page):
            result = self.worker(routes).run(calls=2, bodies=1)
        self.assertEqual(result["bodies"], 1)
        self.assertGreaterEqual(self.clock.now - start, 50)
        self.assertEqual(self.store.missing_bodies("weread"), [])
        article = self.store.public_articles(days=4000)[0]
        self.assertEqual(article["publication_status"], "known")
        self.assertEqual(article["published_at"], 1762740000)
        audit = json.loads(self.store.metadata()["weread_audit"])
        self.assertEqual({a["scope"] for a in audit}, {"weread_latest"})

    def test_ping_session_renews_only_after_expired_key(self):
        http = HTTP({"/web/shelf/sync": Response({"books": [], "synckey": 1})})
        status = ping_session(self.weread, http=http, clock=self.clock)
        self.assertEqual(status["state"], "ok")
        self.assertEqual([c[1] for c in http.calls], ["/web/shelf/sync"])
        self.assertEqual(json.loads((self.weread / "scheduler.json").read_text(encoding="utf-8"))["session"]["state"], "ok")

        http = HTTP({"/web/shelf/sync": [Response({"errCode": -2012}), Response({"books": [], "synckey": 1})],
                     "/web/login/renewal": Response({"succ": 1}, cookies=[("wr_skey", "fresh")])})
        status = ping_session(self.weread, http=http, clock=self.clock)
        self.assertEqual(status["state"], "ok")
        self.assertEqual([c[1] for c in http.calls], ["/web/shelf/sync", "/web/login/renewal", "/web/shelf/sync"])
        self.assertEqual(json.loads((self.weread / "session.json").read_text(encoding="utf-8"))["cookies"]["wr_skey"], "fresh")

    def test_ping_session_skips_during_cooldown(self):
        (self.weread / "scheduler.json").write_text(json.dumps({
            "session": {"state": "ok"}, "limits": {"cooldown_until": self.clock() + 3600}}), encoding="utf-8")
        http = HTTP({"/web/shelf/sync": Response({"books": [], "synckey": 1})})
        self.assertEqual(ping_session(self.weread, http=http, clock=self.clock)["state"], "cooldown")
        self.assertEqual(http.calls, [])

    def test_ping_session_missing_login_is_auth_missing(self):
        (self.weread / "session.json").unlink()
        status = ping_session(self.weread, clock=self.clock)
        self.assertEqual(status["state"], "auth_missing")
        self.assertEqual(json.loads((self.weread / "scheduler.json").read_text(encoding="utf-8"))["session"]["state"], "auth_missing")


class DigestAlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        now = int(time.time())
        day = lambda offset: (datetime.now(TZ) + timedelta(days=offset)).strftime("%Y-%m-%d")
        self.talk_day = day(3)
        job = "https://job.bit.edu.cn/f/recruitmentinfo/show?recruitmentId="
        self.store.import_articles([
            {"url": "https://mp.weixin.qq.com/s/aaaaaaaaaa1", "title": "2027届选调生宣讲会", "source": "北理就业",
             "published_at": now - 3600, "origin": "weread"},
            {"url": "https://mp.weixin.qq.com/s/aaaaaaaaaa2", "title": "篮球赛圆满落幕", "source": "BITYOUTH",
             "published_at": now - 3600, "origin": "weread"},
            {"url": "https://mp.weixin.qq.com/s/aaaaaaaaaa3", "title": "秋日的校园", "source": "北京理工大学",
             "published_at": now - 3600, "origin": "weread"},
            {"url": "https://www.bit.edu.cn/tzgg17/zcgg/x.htm", "title": "红外无损检测系统采购更正公告", "source": "北理工主页",
             "published_at": now - 3600, "kind": "site"},
            {"url": "https://math.bit.edu.cn/x.htm", "title": "关于开展第六届费振勇奖学金评选的通知", "source": "数学学院",
             "published_at": now - 3600, "kind": "site"},
            {"url": "https://job.bit.edu.cn/f/recruitmentFair/show?id=talk1", "title": "某研究所2027届宣讲会",
             "source": "北理就业官网·宣讲会", "published_at": now, "publication_status": "unknown",
             "origin": "official_server", "kind": "official",
             "body": f"活动开始（官网字段）：{self.talk_day} 14:00:00",
             "source_event_start": f"{self.talk_day} 14:00:00"},
            {"url": "https://job.bit.edu.cn/f/bilateralchosefair/show?id=old1",
             "title": "（因疫情原因暂缓举办）北京理工大学2022届毕业生秋季综合招聘会",
             "source": "北理就业官网·双选会", "published_at": now, "publication_status": "unknown",
             "origin": "official_server", "kind": "official",
             "body": "活动开始（官网字段）：2021-10-27 14:00:00", "source_event_start": "2021-10-27 14:00:00"},
            {"url": job + "new1", "title": "本周新上的校园招聘", "source": "北理就业官网·招聘职位",
             "published_at": now - 2 * 86400, "origin": "official_server", "kind": "official",
             "body": "招聘有效截止（官网字段）：2099-01-15 00:00:00",
             "source_application_deadline": "2099-01-15 00:00:00"},
            {"url": job + "old1", "title": "一个多月前的校园招聘", "source": "北理就业官网·招聘职位",
             "published_at": now - 40 * 86400, "origin": "official_server", "kind": "official",
             "body": "招聘有效截止（官网字段）：2099-05-04 00:00:00",
             "source_application_deadline": "2099-05-04 00:00:00"},
            {"url": job + "soon1", "title": "三天后截止的校园招聘", "source": "北理就业官网·招聘职位",
             "published_at": now - 40 * 86400, "origin": "official_server", "kind": "official",
             "body": f"招聘有效截止（官网字段）：{day(3)} 00:00:00",
             "source_application_deadline": f"{day(3)} 00:00:00"}])

    def tearDown(self):
        self.tmp.cleanup()

    def titles(self, built, group):
        return [a["title"] for a in built["groups"][group]]

    def test_weekly_uses_publication_days_for_posts_and_event_days_for_the_careers_site(self):
        built = digest.build(self.store, use_model=False)
        self.assertEqual(sorted(self.titles(built, "daily")),
                         sorted(["2027届选调生宣讲会", "秋日的校园", "关于开展第六届费振勇奖学金评选的通知"]))
        self.assertEqual(self.titles(built, "career_events"), ["某研究所2027届宣讲会"])
        self.assertEqual(self.titles(built, "career_jobs"), ["本周新上的校园招聘"])
        self.assertEqual(self.titles(built, "jobs_closing"), ["三天后截止的校园招聘"])
        everything = json.dumps(built["groups"], ensure_ascii=False)
        for gone in ("圆满落幕", "采购", "暂缓举办", "一个多月前"):
            self.assertNotIn(gone, everything)
        text = digest.card(built, "http://example/weekly.docx")
        self.assertIn("活动 " + digest._short(self.talk_day), text)
        self.assertRegex(digest._short(self.talk_day), r"^\d{4}年")
        self.assertIn("http://example/weekly.docx", text)
        self.assertTrue(built["title"].startswith("北理一周汇总 · "))
        self.assertNotIn("暂缓举办", json.dumps(self.store.public_articles(days=4000), ensure_ascii=False))

    def test_model_verdicts_shape_the_card_and_the_word_file(self):
        calls = []

        def ask(store, query, limit, system):
            calls.append(query.splitlines()[0])
            if query.startswith("BITFEED_WEEKLY_SUMMARY_V1"):
                return json.dumps({"items": [], "summary": "本周重点是奖学金申请。"}), "school"
            items = []
            for line in query.splitlines():
                if not line[:1].isdigit():
                    continue
                n, label, source, when, title = line.split("｜", 4)
                items.append({"n": int(n), "keep": title != "秋日的校园", "star": "奖学金" in title,
                              "note": "可申请" if "奖学金" in title else ""})
            return json.dumps({"items": items}), "school"

        with mock.patch.object(digest, "SITE_DIR", Path(self.tmp.name) / "site"), \
                mock.patch("bitfeed.feishu.send", return_value={"state": "sent", "duplicate": False}) as send:
            result = digest.send(self.store, "test", ask=ask)
        self.assertEqual(result["model"], "ok")
        self.assertEqual(calls, ["BITFEED_WEEKLY_V1", "BITFEED_WEEKLY_SUMMARY_V1"])
        text = send.call_args.args[1]
        self.assertIn("本周重点是奖学金申请。", text)
        self.assertIn("【本周重点】", text)
        self.assertIn("费振勇奖学金评选的通知", text.split("【本周重点】")[1])
        self.assertNotIn("秋日的校园", text)
        files = list((Path(self.tmp.name) / "site" / "weekly").glob("bit-weekly-*.docx"))
        self.assertEqual(len(files), 1)
        self.assertIn(files[0].name, text)
        with zipfile.ZipFile(files[0]) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
            rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        self.assertIn("本周新上的校园招聘", document)
        self.assertIn("北理就业 · 未来一周的宣讲会与双选会", document)
        self.assertIn("模型判断本周参考价值较低", document)
        self.assertGreater(document.find("秋日的校园"), document.find("参考价值较低"))
        self.assertIn("job.bit.edu.cn", rels)

    def test_window_advances_only_after_delivery_and_model_trouble_keeps_everything(self):
        def broken(*args, **kwargs):
            raise RuntimeError("school API unavailable")

        with mock.patch.object(digest, "SITE_DIR", Path(self.tmp.name) / "site"):
            with mock.patch("bitfeed.feishu.send", return_value={"state": "rejected", "duplicate": False}):
                failed = digest.send(self.store, "test", ask=broken)
            self.assertEqual(failed["model"], "unavailable")
            self.assertNotIn("digest:window_end", self.store.metadata())
            with mock.patch("bitfeed.feishu.send", return_value={"state": "sent", "duplicate": False}) as send:
                digest.send(self.store, "test", ask=broken)
        self.assertIn("未能参与筛选", send.call_args.args[1])
        self.assertIn("digest:window_end", self.store.metadata())
        again = digest.build(self.store, use_model=False)
        self.assertEqual(again["groups"]["daily"], [])
        self.assertEqual(again["groups"]["career_jobs"], [])
        self.assertEqual(self.titles(again, "career_events"), ["某研究所2027届宣讲会"])

    def test_alerts_repeat_at_most_every_12_hours_and_announce_recovery(self):
        wechat = Path(self.tmp.name) / "wechat"
        wechat.mkdir()
        weread = Path(self.tmp.name) / "weread"
        weread.mkdir()
        (wechat / "keepalive.json").write_text(json.dumps({"state": "auth_expired"}))
        sent = []
        send = lambda title, text, template="orange": sent.append(title) or "sent"
        alerts.run(self.store, weread, wechat, send=send)
        alerts.run(self.store, weread, wechat, send=send)
        self.assertEqual(sent, ["北理消息站 · 公众号后台登录已失效"])
        (wechat / "keepalive.json").write_text(json.dumps({"state": "ok", "expires_at": time.time() + 4 * 86400}))
        alerts.run(self.store, weread, wechat, send=send)
        self.assertEqual(sent[-1], "北理消息站 · 已恢复")


class RegistryTest(unittest.TestCase):
    def test_seed_accounts_have_unique_fakeids(self):
        from bitfeed import accounts
        rows = accounts.load(include_disabled=True)
        fakeids = [a["fakeid"] for a in rows]
        self.assertEqual(len(fakeids), len(set(fakeids)))
        self.assertGreaterEqual(len(rows), 50)
        self.assertIn("北理工自动化学院", {a["name"] for a in rows})


if __name__ == "__main__":
    unittest.main()
