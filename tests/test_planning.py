from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bitfeed import digest
from bitfeed.domain import TZ
from bitfeed.planning import select, weekly_selection, window
from bitfeed.store import Store


class PlanningTest(unittest.TestCase):
    def setUp(self):
        # First biweekly Tuesday: 2026-11-24 09:00 Beijing time -> window 2026-11-27 (Fri) .. 2026-12-11 (Fri)
        self.now = datetime(2026, 11, 24, 9, 0, tzinfo=TZ).timestamp()

    def item(self, name, day, deadline=None, published_days_ago=60, **extra):
        return {
            "id": name,
            "title": name,
            "url": "https://mp.weixin.qq.com/s/" + name,
            "source": "学院",
            "published_at": self.now - published_days_ago * 86400,
            "classification": {
                "verified_activity": True,
                "event_status": "actionable",
                "event_date": day,
                "deadline": deadline,
                "summary": "",
                "next_step": "核对原文",
            },
            **extra,
        }

    def test_tuesday_covers_this_friday_through_two_fridays_out(self):
        span = window(self.now)
        self.assertEqual((span["start"], span["end"]), ("2026-11-27", "2026-12-11"))
        self.assertEqual(span["weeks"][0], {"key": "week1", "start": "2026-11-27", "end": "2026-12-03"})
        self.assertEqual(span["weeks"][1], {"key": "week2", "start": "2026-12-04", "end": "2026-12-11"})
        # Year rollover check: Tuesday 2026-12-22 -> 2026-12-25 (Fri) .. 2027-01-08 (Fri)
        year = window(datetime(2026, 12, 22, 9, tzinfo=TZ).timestamp())
        self.assertEqual((year["start"], year["end"]), ("2026-12-25", "2027-01-08"))

    def test_monday_through_friday_keep_same_friday_and_weekend_rolls_to_next_friday(self):
        for day in (23, 24, 25, 26, 27):
            span = window(datetime(2026, 11, day, 12, tzinfo=TZ).timestamp())
            self.assertEqual((span["start"], span["end"]), ("2026-11-27", "2026-12-11"))
        sat = window(datetime(2026, 11, 28, 12, tzinfo=TZ).timestamp())
        self.assertEqual((sat["start"], sat["end"]), ("2026-12-04", "2026-12-18"))

    def test_old_post_and_all_calendar_boundaries_are_kept(self):
        items = [
            self.item("old-publication", "2026-11-27"),
            self.item("week-one-end", "2026-12-03"),
            self.item("week-two-start", "2026-12-04"),
            self.item("second-friday-end", "2026-12-11"),
            self.item("beyond-window", "2026-12-12"),
        ]
        _, g = select(items, self.now)
        self.assertEqual({a["id"] for a in g["week1"]}, {"old-publication", "week-one-end"})
        self.assertEqual({a["id"] for a in g["week2"]}, {"week-two-start", "second-friday-end"})
        self.assertEqual([a["id"] for a in g["later"]], ["beyond-window"])

    def test_application_date_is_the_action_node_and_closed_registration_is_excluded(self):
        items = [
            self.item("apply-before-friday", "2026-12-20", "2026-11-25"),
            self.item("apply-in-second-half", "2027-01-20", "2026-12-08"),
            self.item("registration-closed", "2026-12-01", "2026-11-23"),
            self.item("活动回顾", "2026-11-30"),
            self.item("already-over", "2026-11-23"),
        ]
        _, g = select(items, self.now)
        self.assertEqual([a["id"] for a in g["urgent"]], ["apply-before-friday"])
        self.assertEqual([a["id"] for a in g["week2"]], ["apply-in-second-half"])
        self.assertEqual(sum(len(v) for v in g.values()), 2)

    def test_a_list_validity_field_is_not_a_verified_job_deadline(self):
        job = self.item(
            "job-lead",
            None,
            "2026-12-05",
            published_days_ago=2,
            source="北理就业官网·招聘职位",
            body_status="structured_list_metadata",
            body="招聘有效截止（官网字段）：2026-12-05 00:00:00",
        )
        job["classification"]["deadline_evidence"] = "招聘有效截止（官网字段）：2026-12-05"
        _, g = select([job], self.now)
        self.assertEqual([a["id"] for a in g["leads"]], ["job-lead"])
        self.assertEqual(sum(len(g[k]) for k in g if k != "leads"), 0)

    def test_clock_time_and_exact_official_end_override_day_only_claims(self):
        noon = datetime(2026, 11, 24, 12, tzinfo=TZ).timestamp()
        item = self.item("finished-this-morning", "2026-11-24")
        item["classification"]["event_date_evidence"] = "2026年11月24日 10:00-11:00"
        _, g = select([item], noon)
        self.assertFalse(any(g.values()))
        talk = self.item(
            "official-future",
            None,
            source="北理就业官网·宣讲会",
            body="活动开始（官网字段）：2026-12-05 14:00:00",
        )
        _, g = select([talk], noon)
        self.assertEqual([a["id"] for a in g["week2"]], ["official-future"])

    def test_an_open_opportunity_returns_in_the_next_biweekly_edition(self):
        item = self.item("still-open", None, "2026-12-11")
        _, first = select([item], self.now)
        _, second = select([item], self.now + 14 * 86400)
        self.assertEqual([a["id"] for a in first["week2"]], ["still-open"])
        self.assertEqual([a["id"] for a in second["week1"]], ["still-open"])

    def test_partial_registration_deadline_expires_before_the_future_event(self):
        item = self.item("操舞比赛", "2026-12-05", body="各队伍选出一名领队于11月23日前填写问卷报名，并扫码进通知群。")
        _, sunday = select([item], datetime(2026, 11, 22, 12, tzinfo=TZ).timestamp())
        row = sunday["urgent"][0]
        self.assertEqual(row["deadline"], "2026-11-23")
        self.assertIn("未写年份", row["scope_note"])
        self.assertIn("11月23日前", row["deadline_evidence"])
        _, tuesday = select([item], self.now)
        self.assertFalse(any(tuesday.values()))

    def test_registration_range_uses_its_end_and_keeps_the_original_quote(self):
        item = self.item(
            "选调报名",
            "2026-12-15",
            body="考生于2026年11月20日10:00—11月30日20:00期间登录网上报名服务平台进行注册。",
        )
        _, g = select([item], self.now)
        self.assertEqual(g["week1"][0]["deadline"], "2026-11-30")

    def test_future_official_date_with_closed_status_needs_verification(self):
        item = self.item(
            "2027届毕业生冬季大型招聘会",
            None,
            published_days_ago=2,
            source="北理就业官网·双选会",
            body="活动开始（官网字段）：2026-12-05 14:00:00\n官网显示状态（须结合日期核对）：已结束",
        )
        _, g = select([item], self.now)
        self.assertEqual([a["id"] for a in g["leads"]], ["2027届毕业生冬季大型招聘会"])
        self.assertFalse(g["week2"])

    def test_recent_import_does_not_make_old_or_result_notices_opportunities(self):
        items = [
            self.item("2024年奖学金申请通知", None, published_days_ago=1),
            self.item("2026年奖学金评选结果公示", None, published_days_ago=1),
            self.item("2026年学术论坛报名通知", None, published_days_ago=1),
        ]
        _, g = select(items, self.now)
        self.assertEqual([a["id"] for a in g["leads"]], ["2026年学术论坛报名通知"])

    def test_partial_deadline_year_rollover_clock_and_ambiguity(self):
        item = self.item("跨年报名", "2027-01-10", body="报名截止：12月31日17:00")
        before = datetime(2026, 12, 31, 16, tzinfo=TZ).timestamp()
        _, g = select([item], before)
        self.assertEqual(g["urgent"][0]["deadline"], "2026-12-31")
        _, g = select([item], before + 3600)
        self.assertFalse(any(g.values()))
        uncertain = self.item(
            "大赛报名",
            "2026-12-08",
            published_days_ago=2,
            body="本科生报名截止11月28日，研究生报名截止11月30日。",
        )
        _, g = select([uncertain], self.now)
        self.assertEqual([a["id"] for a in g["leads"]], ["大赛报名"])

    def test_biweekly_selection_keeps_all_categories_and_scores_in_main_window(self):
        low_career = {"id": "low_career", "title": "普通宣讲", "category": "career", "score": 55}
        low_other = {"id": "low_other", "title": "社团手工坊", "category": "other", "score": 50}
        major = {"id": "major", "title": "博士生论坛征稿", "category": "academic", "score": 92}
        distant_minor = {"id": "distant_minor", "title": "远期普通通知", "category": "other", "score": 60}
        distant_prep = {"id": "distant_prep", "title": "暑期实习生招收", "category": "career", "score": 90}
        inventory = {
            "urgent": [],
            "week1": [low_career],
            "week2": [low_other, major],
            "week3": [],
            "later": [distant_minor, distant_prep],
            "leads": [],
        }
        selected = weekly_selection(inventory)
        # All content in week1 and week2 is retained regardless of category or score!
        self.assertEqual(selected["week1"], [low_career])
        self.assertEqual(selected["week2"], [low_other, major])
        self.assertEqual(selected["later"], [distant_prep])
        self.assertEqual([x["id"] for x in selected["additional"]], ["distant_minor"])

    def test_preview_generation_exports_pool_and_has_no_delivery_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data")
            with patch("bitfeed.store.time.time", return_value=self.now):
                store.import_articles([
                    {
                        "title": "长期征稿",
                        "url": "https://cs.bit.edu.cn/future.htm",
                        "source": "学院",
                        "published_at": int(self.now) - 120 * 86400,
                        "body": "投稿截止2099年12月1日",
                    }
                ])
            a = store.candidates()[0]
            store.apply_classifications(
                {
                    "items": [
                        {
                            "article_id": a["id"],
                            "source_hash": a["source_hash"],
                            "category": "academic",
                            "event_status": "actionable",
                            "score": 90,
                            "confidence": 0.95,
                            "summary": "征稿信息",
                            "next_step": "核对原文投稿",
                            "reason": "截止时间明确",
                            "deadline": "2099-12-01",
                            "deadline_evidence": "2099年12月1日",
                            "event_date": None,
                            "event_date_evidence": "",
                        }
                    ]
                },
                "editor",
            )
            folder = Path(tmp) / "weekly"
            with patch("bitfeed.feishu.send", side_effect=AssertionError("preview must not send")):
                summary = digest.write_preview(store, now=self.now, use_model=False, directory=folder)
            self.assertEqual(summary["verified"], 1)
            self.assertEqual(summary["period"], "2026-11-27至2026-12-11")
            self.assertTrue((folder / "bit-preview-latest.docx").is_file())
            self.assertTrue((folder / "planning-pool.json").is_file())
            public = json.loads((folder / "preview.json").read_text(encoding="utf8"))
            self.assertNotIn("body", public["groups"]["later"][0])
            pool_json = json.loads((folder / "planning-pool.json").read_text(encoding="utf8"))
            self.assertNotIn("body", pool_json["entries"][0])
            self.assertEqual(json.loads(store.metadata()["weekly_preview"])["verified"], 1)


if __name__ == "__main__":
    unittest.main()
