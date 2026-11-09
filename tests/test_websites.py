from datetime import datetime
import unittest
from unittest.mock import patch

from bitfeed.websites import TZ, _load_config, collect

NOW = datetime(2026, 11, 9, 12, tzinfo=TZ)
SOURCE = {"id":"school-test", "name":"测试学院·通知公告", "url":"https://example.bit.edu.cn/tzgg/index.htm"}


class Response:
    def __init__(self, html, status=200):
        self.content=html.encode("utf-8")
        self.status_code=status


class PublicHTTP:
    def __init__(self, pages):
        self.pages=pages
        self.calls=[]

    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        response=self.pages[url]
        return response if isinstance(response,Response) else Response(response)


class WebsitesTest(unittest.TestCase):
    def run_collect(self,pages,sources=None,max_pages=3):
        http=PublicHTTP(pages)
        with patch("bitfeed.websites._load_config",return_value=sources or [SOURCE]), patch("bitfeed.websites._now",return_value=NOW):
            result=collect(session=http,max_pages=max_pages)
        return result,http

    def test_dates_belong_to_each_row_and_title_dates_are_not_publication(self):
        html='''<meta charset="utf-8"><div class="list"><div>2020-01-01</div><ul>
        <li><span class="date">2026-11-09</span><a href="a.htm">2026年12月1日学术讲座报名通知</a></li>
        <li><span>2026-11-08</span><a href="b.htm">第二条学院学生工作通知</a></li>
        </ul></div>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual([a["source_publication_date"] for a in data["articles"]],["2026-11-09","2026-11-08"])
        self.assertEqual(data["articles"][0]["source_date_evidence"],"2026-11-09")

    def test_multilink_ancestor_does_not_lend_its_first_date(self):
        html='''<meta charset="utf-8"><div class="group"><span>2026-11-09</span>
        <div><a href="a.htm">第一条没有单独日期的通知</a></div>
        <div><a href="b.htm">第二条没有单独日期的通知</a></div></div>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual(len(data["articles"]),2)
        self.assertTrue(all(a["publication_status"]=="unknown" for a in data["articles"]))
        self.assertTrue(all(a["published_at"]==int(NOW.timestamp()) for a in data["articles"]))

    def test_single_dateless_row_does_not_borrow_section_heading_date(self):
        html='''<meta charset="utf-8"><div><h2>2020-01-01</h2><ul>
        <li><a href="a.htm">没有发布日期的学院工作通知</a></li></ul></div>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual(data["articles"][0]["publication_status"],"unknown")

    def test_split_year_and_month_day_date(self):
        html='''<meta charset="utf-8"><li><a href="a.htm" title="学院创新竞赛报名工作通知">学院创新竞赛报名工作通知</a>
        <div class="date"><span>2026</span><span>11.09</span></div></li>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual(data["articles"][0]["source_publication_date"],"2026-11-09")

    def test_only_observed_next_link_is_followed(self):
        first='''<meta charset="utf-8"><li>2026-11-09<a href="a.htm">第一条学院公开通知消息</a></li><a href="index1.htm">下一页</a>'''
        second='''<meta charset="utf-8"><li>2026-11-08<a href="b.htm">第二条学院公开通知消息</a></li><div class="pages">第2页 / 共2页</div>'''
        second_url="https://example.bit.edu.cn/tzgg/index1.htm"
        data,http=self.run_collect({SOURCE["url"]:first,second_url:second})
        self.assertEqual([u for u,_ in http.calls],[SOURCE["url"],second_url])
        self.assertEqual(len(data["articles"]),2)
        self.assertTrue(data["sources"][0]["complete"])
        self.assertFalse(data["sources"][0]["history_complete"])
        for _,options in http.calls:
            self.assertEqual(options["headers"]["Cookie"],"")
            self.assertNotIn("Authorization",options["headers"])
            self.assertFalse(options["allow_redirects"])

    def test_page_cap_is_explicit(self):
        html='''<meta charset="utf-8"><li>2026-11-09<a href="a.htm">学院学生工作公开通知消息</a></li><a href="index1.htm">下一页</a>'''
        data,http=self.run_collect({SOURCE["url"]:html},max_pages=1)
        self.assertEqual(len(http.calls),1)
        self.assertEqual(data["sources"][0]["pagination_status"],"cap_reached")
        self.assertTrue(data["sources"][0]["truncated"])
        self.assertFalse(data["sources"][0]["complete"])

    def test_no_next_link_is_not_a_full_archive_claim(self):
        html='''<meta charset="utf-8"><li>2026-11-09<a href="a.htm">学院学生工作公开通知消息</a></li>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertTrue(data["sources"][0]["exhausted"])
        self.assertFalse(data["sources"][0]["complete"])
        self.assertFalse(data["sources"][0]["history_complete"])

    def test_redirect_does_not_authenticate_or_stop_other_source(self):
        other={"id":"other", "name":"其他学院", "url":"https://other.bit.edu.cn/tzgg/index.htm"}
        html='''<meta charset="utf-8"><li>2026-11-09<a href="b.htm">其他学院学生公开通知消息</a></li>'''
        data,_=self.run_collect({SOURCE["url"]:Response("",302),other["url"]:html},sources=[SOURCE,other])
        self.assertEqual(data["sources"][0]["state"],"failed")
        self.assertEqual(data["sources"][0]["error"],"HTTP 302")
        self.assertEqual(len(data["articles"]),1)

    def test_date_in_headline_alone_stays_unknown(self):
        html='''<meta charset="utf-8"><li><a href="a.htm">2026年11月8日学术讲座报名通知</a></li>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual(data["articles"][0]["publication_status"],"unknown")

    def test_navigation_foreign_links_and_column_pages_are_excluded(self):
        html='''<meta charset="utf-8"><nav><a href="nav.htm">这是导航中的站点介绍页面</a></nav>
        <li>2026-11-09<a href="https://foreign.example/a.htm">外站页面不属于官方来源</a></li>
        <li>2026-11-09<a href="../other/index.htm">其他学院栏目列表链接</a></li>
        <li>2026-11-09<a href="a.htm">学院学生工作公开通知消息</a></li>'''
        data,_=self.run_collect({SOURCE["url"]:html})
        self.assertEqual(len(data["articles"]),1)
        self.assertTrue(data["articles"][0]["url"].endswith("/a.htm"))

    def test_config_contains_only_thirty_public_sources(self):
        sources=_load_config()
        self.assertEqual(len(sources),30)
        self.assertTrue(all(set(s)=={"id","name","url"} for s in sources))
        self.assertTrue(all("bit.edu.cn" in s["url"] for s in sources))


if __name__=="__main__":unittest.main()
