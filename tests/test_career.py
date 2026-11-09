from datetime import datetime
import unittest
from unittest.mock import patch

from bitfeed.career import BASE, SOURCES, TZ, collect


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=TZ)


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class PublicHTTP:
    def __init__(self, modify=None):
        self.calls = []
        self.modify = modify

    def post(self, url, data, headers, **kwargs):
        self.calls.append((url, dict(data), dict(headers), kwargs))
        spec = next(s for s in SOURCES if BASE+s["path"] == url and all(data.get(k)==v for k,v in s["params"].items()))
        number = data["pageNo"]
        row = {"id": spec["id"]+str(number), "title": "官方活动通知", "url": "/f/example/show?id="+spec["id"]+str(number),
               "startTime": "2026-11-20 14:00:00", "endTime": "2026-11-20 16:00:00", "place": "报告厅",
               "releaseDate": "2026-11-08 10:30", "description": "来自公开列表的摘要"}
        page = {"pageNo": number, "pageSize": 1, "count": 2, "totalPage": 2, "lastPage": number==2, "list": [row]}
        if self.modify:
            changed = self.modify(spec, page)
            if changed:
                return changed
        result = page
        for key in reversed(spec["container"].split(".")):
            result = {key: result}
        return Response({"state": 1, **result})


class CareerTest(unittest.TestCase):
    def run_collect(self, http, **kwargs):
        with patch("bitfeed.career._now", return_value=NOW):
            return collect(session=http, **kwargs)

    def test_all_eight_sources_paginate_without_credentials(self):
        http = PublicHTTP()
        data = self.run_collect(http, page_size=1)
        self.assertEqual(len(data["articles"]), 16)
        self.assertEqual(len(data["sources"]), 8)
        self.assertTrue(all(s["complete"] and s["pages"]==2 and s["fetched"]==2 for s in data["sources"]))
        for _,_,headers,options in http.calls:
            self.assertEqual(headers["Cookie"], "")
            self.assertNotIn("Authorization", headers)
            self.assertFalse(options["allow_redirects"])

    def test_future_event_is_not_its_publication_date(self):
        data = self.run_collect(PublicHTTP(), page_size=1)
        event = next(a for a in data["articles"] if a["source_id"]=="career-talks")
        self.assertEqual(event["publication_status"], "unknown")
        self.assertEqual(event["published_at"], int(NOW.timestamp()))
        self.assertEqual(event["source_event_start"], "2026-11-20 14:00:00")
        self.assertIn("活动开始（官网字段）：2026-11-20", event["body"])
        news = next(a for a in data["articles"] if a["source_id"]=="career-news")
        self.assertEqual(news["publication_status"], "known")
        self.assertLess(news["published_at"], event["published_at"])

    def test_page_cap_is_reported_as_partial(self):
        data = self.run_collect(PublicHTTP(), max_pages=1, page_size=1)
        self.assertTrue(all(s["state"]=="partial" and s["truncated"] and not s["complete"] for s in data["sources"]))
        self.assertEqual(len(data["articles"]), 8)

    def test_one_source_failure_keeps_earlier_rows_and_other_sources(self):
        def modify(spec,page):
            if spec["id"]=="career-talks" and page["pageNo"]==2:
                return Response({},503)
        data=self.run_collect(PublicHTTP(modify), page_size=1)
        audit=next(s for s in data["sources"] if s["id"]=="career-talks")
        self.assertEqual(audit["state"],"partial")
        self.assertEqual(audit["fetched"],1)
        self.assertEqual(audit["error"],"HTTP 503")
        self.assertEqual(len(data["articles"]),15)

    def test_personal_lost_document_notice_is_excluded_before_export(self):
        def modify(spec,page):
            if spec["id"]=="career-notices" and page["pageNo"]==1:
                page["list"][0].update(title="关于某同学遗失就业推荐表的公示",description="不应发布的测试信息")
        data=self.run_collect(PublicHTTP(modify),page_size=1)
        self.assertFalse(any("不应发布" in a["body"] for a in data["articles"]))
        audit=next(s for s in data["sources"] if s["id"]=="career-notices")
        self.assertEqual(audit["excluded"],1)
        self.assertTrue(audit["complete"])

    def test_duplicate_across_categories_has_one_article(self):
        def modify(spec,page):
            if spec["type"]=="news":
                page["list"][0]["url"]="/frontpage/bit/html/newsDetail.html?id=shared"+str(page["pageNo"])
        data=self.run_collect(PublicHTTP(modify),page_size=1)
        self.assertEqual(len(data["articles"]),12)

    def test_repeated_page_does_not_claim_exhaustion(self):
        def modify(spec,page):
            if spec["id"]=="career-talks":
                page.update(count=3,totalPage=3,lastPage=False)
                page["list"][0]["id"]="same"
                page["list"][0]["url"]="/f/example/show?id=same"
        data=self.run_collect(PublicHTTP(modify),page_size=1)
        audit=next(s for s in data["sources"] if s["id"]=="career-talks")
        self.assertFalse(audit["exhausted"])
        self.assertFalse(audit["complete"])
        self.assertEqual(audit["fetched"],1)

    def test_external_link_is_rejected_and_recorded(self):
        def modify(spec,page):
            if spec["id"]=="career-talks":
                page["list"][0]["url"]="https://unrelated.example/article"
        data=self.run_collect(PublicHTTP(modify),page_size=1)
        audit=next(s for s in data["sources"] if s["id"]=="career-talks")
        self.assertEqual(audit["invalid"],2)
        self.assertFalse(audit["complete"])
        self.assertFalse(any("unrelated.example" in a["url"] for a in data["articles"]))


if __name__=="__main__":unittest.main()
