import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import requests

from bitfeed.albums import collect, ENDPOINT, ORIGIN


ALBUM_URL = "https://mp.weixin.qq.com/mp/appmsgalbum?__biz=TestBiz%3D%3D&action=getalbum&album_id=123456789"


def row(msgid, itemidx=1, **changes):
    return {"msgid":str(msgid), "itemidx":str(itemidx), "title":"公开活动通知",
            "url":f"https://mp.weixin.qq.com/s?__biz=TestBiz%3D%3D&mid={msgid}&idx={itemidx}&sn=test",
            "create_time":str(int(time.time())), **changes}


def page(rows, count, continued="0"):
    return {"base_resp":{"ret":0}, "getalbum_resp":{"base_info":{"article_count":str(count),
            "nickname":"测试来源"}, "article_list":rows, "continue_flag":continued}}


class Response:
    def __init__(self, data=None, status=200, text=""):
        self.data, self.status_code, self.text, self.content = data, status, text, text.encode()
    def json(self):
        if self.data is None:raise ValueError()
        return self.data


class HTTP:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []
    def get(self, url, **kwargs):
        self.calls.append((url,kwargs))
        response=next(self.responses)
        if isinstance(response,Exception):raise response
        return response


class AlbumsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"albums.json"
    def config(self, albums=None):
        self.path.write_text(json.dumps({"albums":albums if albums is not None else [
            {"id":"test-album","name":"测试公开合集","url":ALBUM_URL}]}),encoding="utf-8")
        return self.path

    def test_unconfigured_never_constructs_http_session(self):
        with patch('bitfeed.albums.requests.Session',side_effect=AssertionError('no request')):
            self.assertEqual(collect(self.path)["state"],"unconfigured")
            result=collect(self.config([]))
        self.assertEqual(result["articles"],[])
        self.assertEqual(result["state"],"unconfigured")

    def test_cursor_pagination_and_public_only_transport(self):
        http=HTTP([Response(page([row(10),row(10,2)],3,"1")),Response(page([row(11)],3))])
        result=collect(self.config(),http)
        audit=result["sources"][0]
        self.assertEqual(result["state"],"ok")
        self.assertEqual(len(result["articles"]),3)
        self.assertEqual(audit["pages"],2)
        self.assertTrue(audit["complete"])
        self.assertFalse(audit["account_complete"])
        self.assertEqual(audit["scope"],"album_only")
        self.assertEqual(http.calls[1][1]["params"]["begin_msgid"],"10")
        self.assertEqual(http.calls[1][1]["params"]["begin_itemidx"],"2")
        for url,kwargs in http.calls:
            self.assertEqual(url,ENDPOINT)
            self.assertEqual(kwargs["headers"]["Cookie"],"")
            self.assertEqual(kwargs["headers"]["Authorization"],"")
            self.assertEqual(kwargs["auth"],())
            self.assertTrue(kwargs["verify"])
            self.assertFalse(kwargs["allow_redirects"])
            self.assertTrue(all(not value for value in kwargs["proxies"].values()))
            self.assertFalse(set(kwargs["params"]) & {"cookie","token","key","uin","pass_ticket","appmsg_token"})
        for article in result["articles"]:
            self.assertEqual(article["origin"],ORIGIN)
            self.assertEqual(article["body_status"],"not_fetched")
            self.assertEqual(article["body"],"")

    def test_singleton_and_empty_album_terminal_responses(self):
        result=collect(self.config(),HTTP([Response(page(row(1),1))]))
        self.assertTrue(result["sources"][0]["complete"])
        result=collect(self.config(),HTTP([Response(page([],0))]))
        self.assertTrue(result["sources"][0]["complete"])
        self.assertEqual(result["articles"],[])

    def test_repeated_page_stops_and_retains_earlier_metadata(self):
        first=page([row(1),row(1,2)],5,"1")
        http=HTTP([Response(first),Response(first)])
        result=collect(self.config(),http)
        self.assertEqual(len(http.calls),2)
        self.assertEqual(len(result["articles"]),2)
        self.assertEqual(result["sources"][0]["error"],"repeated_page")
        self.assertFalse(result["sources"][0]["complete"])

    def test_page_cap_and_count_drift_never_claim_complete(self):
        result=collect(self.config(),HTTP([Response(page([row(1)],2,"1"))]),max_pages=1)
        self.assertTrue(result["sources"][0]["truncated"])
        self.assertEqual(result["sources"][0]["state"],"partial")
        result=collect(self.config(),HTTP([Response(page([row(1)],2,"1")),Response(page([row(2)],3))]))
        audit=result["sources"][0]
        self.assertTrue(audit["count_changed"])
        self.assertTrue(audit["exhausted"])
        self.assertFalse(audit["complete"])

    def test_verification_page_is_blocked_and_never_retried(self):
        http=HTTP([Response(text='<html>当前环境异常，完成验证后继续访问</html>')])
        result=collect(self.config(),http)
        self.assertEqual(result["state"],"blocked")
        self.assertEqual(result["sources"][0]["error"],"verification_page")
        self.assertEqual(result["articles"],[])
        self.assertEqual(len(http.calls),1)
        result=collect(self.config(),HTTP([Response(status=302)]))
        self.assertEqual(result["state"],"blocked")
        valid=page([row(1,title="验证码使用说明")],1)
        result=collect(self.config(),HTTP([Response(valid,text=json.dumps(valid,ensure_ascii=False))]))
        self.assertEqual(result["state"],"ok")

    def test_invalid_metadata_and_count_mismatch_are_partial(self):
        result=collect(self.config(),HTTP([Response(page([row(1),row(2,title="")],2))]))
        audit=result["sources"][0]
        self.assertEqual(audit["invalid"],1)
        self.assertEqual(len(result["articles"]),1)
        self.assertFalse(audit["complete"])
        result=collect(self.config(),HTTP([Response(page([row(1)],2))]))
        self.assertFalse(result["sources"][0]["count_matches"])
        self.assertEqual(result["state"],"partial")

    def test_config_rejects_credentials_and_other_hosts_without_exposing_them(self):
        for url in [ALBUM_URL.replace('mp.weixin.qq.com','example.com'),ALBUM_URL+'&key=private-secret']:
            http=HTTP([])
            result=collect(self.config([{"url":url}]),http)
            self.assertEqual(result["state"],"failed")
            self.assertEqual(http.calls,[])
            self.assertNotIn('private-secret',json.dumps(result))
        result=collect(self.config([{"url":ALBUM_URL.replace('__biz=','fakeid=')}]),HTTP([Response(page([],0))]))
        self.assertEqual(result["state"],"ok")

    def test_public_business_and_transport_failures_are_explicit(self):
        result=collect(self.config(),HTTP([Response({"base_resp":{"ret":200013}})]))
        self.assertEqual(result["state"],"blocked")
        result=collect(self.config(),HTTP([requests.Timeout('private-cookie-in-exception')]))
        self.assertEqual(result["state"],"failed")
        self.assertNotIn('private-cookie',json.dumps(result))


if __name__=='__main__':unittest.main()
