from datetime import datetime
import unittest

from bitfeed.activity_time import activity_expired, evidence_end
from bitfeed.domain import TZ


class ActivityTimeTest(unittest.TestCase):
    def test_same_day_event_is_removed_after_its_quoted_end(self):
        item = {"classification": {"event_date": "2026-11-12", "event_date_evidence": "2026年11月12日 10:30-11:30"}}
        self.assertFalse(activity_expired(item, datetime(2026, 11, 12, 10, 45, tzinfo=TZ)))
        self.assertTrue(activity_expired(item, datetime(2026, 11, 12, 11, 30, tzinfo=TZ)))

    def test_start_only_and_multiday_do_not_invent_an_end(self):
        self.assertIsNone(evidence_end("2026-11-12", "2026年11月12日19:00"))
        self.assertIsNone(evidence_end("2026-11-12", "2026年11月12日10:00至2026年11月13日11:00"))

    def test_deadline_clock_and_midnight(self):
        item = {"classification": {"deadline": "2026-11-13", "deadline_evidence": "截止时间为2026年11月13日17：00"}}
        self.assertFalse(activity_expired(item, datetime(2026, 11, 13, 16, 59, tzinfo=TZ)))
        self.assertTrue(activity_expired(item, datetime(2026, 11, 13, 17, 0, tzinfo=TZ)))
        self.assertEqual(evidence_end("2026-11-12", "2026年11月12日24:00", True), datetime(2026, 11, 13, tzinfo=TZ))

    def test_official_expiry_overrides_a_later_model_day(self):
        item = {"source_expiry": int(datetime(2026, 11, 12, 12, tzinfo=TZ).timestamp()),
                "classification": {"event_date": "2026-12-06"}}
        self.assertTrue(activity_expired(item, datetime(2026, 11, 12, 14, tzinfo=TZ)))
