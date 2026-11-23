from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bitfeed import digest
from bitfeed.domain import TZ
from bitfeed.github_ops import run_github_push
from bitfeed.push_config import (
    DEFAULTS,
    load_config,
    load_toml_text,
    schedule_due,
    upcoming_runs,
)
from bitfeed.store import Store


class PushConfigTest(unittest.TestCase):
    def test_repo_default_toml_is_valid_and_matches_biweekly_tuesday(self):
        cfg, source = load_config()
        self.assertEqual(source, "local")
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["every_n_weeks"], 2)
        self.assertEqual(cfg["push_weekday"], 2)
        self.assertEqual(cfg["push_time"], "09:00")
        self.assertEqual(cfg["anchor_date"], "2026-11-24")
        self.assertEqual(cfg["start_weekday"], 5)
        self.assertEqual(cfg["span_days"], 15)

    def test_invalid_toml_inputs_produce_readable_chinese_errors(self):
        with self.assertRaisesRegex(ValueError, "push_weekday"):
            load_toml_text("push_weekday = 9\n")
        with self.assertRaisesRegex(ValueError, "push_time"):
            load_toml_text('push_time = "25:00"\n')
        with self.assertRaisesRegex(ValueError, "anchor_date"):
            load_toml_text('anchor_date = "2026-13-40"\n')
        with self.assertRaisesRegex(ValueError, "未识别的字段"):
            load_toml_text("typo_field = 1\n")

    def test_biweekly_tuesday_schedule_and_weekday_change_safety(self):
        cfg = dict(DEFAULTS)
        # Before 09:00 on first Tuesday -> not due yet
        t_early = datetime(2026, 11, 24, 8, 59, tzinfo=TZ).timestamp()
        self.assertEqual(schedule_due(cfg, t_early)["reason"], "before_push_time")

        # At 09:00 on first Tuesday 2026-11-24 -> due!
        t_due = datetime(2026, 11, 24, 9, 0, tzinfo=TZ).timestamp()
        due_info = schedule_due(cfg, t_due)
        self.assertTrue(due_info["due"])
        self.assertEqual(due_info["cycle_key"], "2026-11-24T09:00:00+08:00")

        # Next week Tuesday 2026-12-01 -> off_week!
        t_off = datetime(2026, 12, 1, 9, 30, tzinfo=TZ).timestamp()
        self.assertFalse(schedule_due(cfg, t_off)["due"])
        self.assertEqual(schedule_due(cfg, t_off)["reason"], "off_week")

        # Two weeks later Tuesday 2026-12-08 -> due again!
        t_next_biweek = datetime(2026, 12, 8, 9, 0, tzinfo=TZ).timestamp()
        self.assertTrue(schedule_due(cfg, t_next_biweek)["due"])

        # Beginner edits push_weekday = 3 (Wednesday) without changing anchor_date: still works!
        wed_cfg = {**cfg, "push_weekday": 3}
        t_wed = datetime(2026, 11, 25, 9, 0, tzinfo=TZ).timestamp()
        self.assertTrue(schedule_due(wed_cfg, t_wed)["due"])

        # Holiday pause enabled = false
        paused = {**cfg, "enabled": False}
        self.assertEqual(schedule_due(paused, t_due)["reason"], "disabled")

    def test_upcoming_runs_lists_consecutive_biweekly_windows(self):
        now = datetime(2026, 11, 23, 20, 0, tzinfo=TZ).timestamp()
        runs = upcoming_runs(DEFAULTS, now=now, count=2)
        self.assertEqual(runs[0]["push_date"], "2026-11-24")
        self.assertEqual((runs[0]["window_start"], runs[0]["window_end"]), ("2026-11-27", "2026-12-11"))
        self.assertEqual(runs[1]["push_date"], "2026-12-08")
        self.assertEqual((runs[1]["window_start"], runs[1]["window_end"]), ("2026-12-11", "2026-12-25"))

    def test_remote_config_sync_falls_back_cleanly_on_error(self):
        class BrokenHTTP:
            def get(self, *args, **kwargs):
                raise OSError("should not crash")

        class BadStatusHTTP:
            def get(self, *args, **kwargs):
                class R:
                    status_code = 500
                    content = b""
                return R()

        cfg, src = load_config(sync_remote=True, session=BadStatusHTTP())
        self.assertEqual(src, "local_fallback")
        self.assertEqual(cfg["every_n_weeks"], 2)

    def test_digest_send_respects_biweekly_schedule_and_deduplicates_same_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "data")
            site_dir = Path(tmp) / "site"

            class OkHTTP:
                calls = 0
                def post(self, url, **kwargs):
                    self.calls += 1
                    class Resp:
                        status_code = 200
                        def json(self):
                            return {"code": 0}
                    return Resp()

            http = OkHTTP()
            env = {
                "FEISHU_TEST_WEBHOOK": "https://open.feishu.cn/open-apis/bot/v2/hook/test-biweekly",
                "BITFEED_DIGEST_MODE": "test",
            }
            with patch.dict(os.environ, env), patch.object(digest, "SITE_DIR", site_dir):
                # Off-week Tuesday skips without sending when enforce_schedule=True
                off_ts = datetime(2026, 12, 1, 9, 5, tzinfo=TZ).timestamp()
                skipped = digest.send(store, now=off_ts, use_model=False, enforce_schedule=True)
                self.assertEqual(skipped["state"], "skipped_schedule")

                # Active Tuesday sends once, then deduplicates at 09:30
                t1 = datetime(2026, 11, 24, 9, 0, tzinfo=TZ).timestamp()
                t2 = datetime(2026, 11, 24, 9, 30, tzinfo=TZ).timestamp()
                with patch("bitfeed.feishu.requests.Session", return_value=http):
                    first = digest.send(store, now=t1, use_model=False, enforce_schedule=True)
                    second = digest.send(store, now=t2, use_model=False, enforce_schedule=True)
                self.assertEqual(first["state"], "sent")
                self.assertTrue(second["duplicate"])
                self.assertEqual(http.calls, 1)

    def test_github_push_preview_and_send_with_step_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pool_file = tmp_path / "pool.json"
            pool_file.write_text(
                json.dumps({
                    "entries": [
                        {
                            "id": "act-1",
                            "title": "人工智能前沿学术报告",
                            "url": "https://cs.bit.edu.cn/a1.htm",
                            "source": "计算机学院",
                            "fact": "2026-11-28",
                            "when": "活动 2026-11-28",
                            "event_date": "2026-11-28",
                            "deadline": None,
                            "event_evidence": "2026年11月28日",
                            "deadline_evidence": "",
                            "date_verified": True,
                            "deadline_year_inferred": False,
                            "category": "academic",
                            "score": 88,
                            "summary": "学术讲座",
                            "next_step": "按时参加",
                            "location": "中关村校区中心教学楼",
                            "audience": "全校师生",
                            "scope_note": "报名资格请核对原文",
                        }
                    ]
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            summary_md = tmp_path / "summary.md"
            now_ts = datetime(2026, 11, 24, 9, 10, tzinfo=TZ).timestamp()
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_md)}):
                res = run_github_push(
                    source_url=str(pool_file),
                    action="preview_only",
                    output_dir=str(tmp_path / "out"),
                    now=now_ts,
                )
            self.assertTrue(res["ok"])
            self.assertEqual(res["period"], "2026-11-27至2026-12-11")
            self.assertEqual(res["items"], 1)
            self.assertTrue(Path(res["docx_path"]).is_file())
            self.assertIn("人工智能前沿学术报告", summary_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
