import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from bitfeed.wechat_platform import Platform, PlatformError, collect, COOLDOWN_SECONDS


ACCOUNT = "Mzg5ODcwMTAyMA=="


def article(mid=1,idx=1,biz=ACCOUNT,**changes):
    return {"aid":f"{mid}_{idx}","title":"公开活动通知","create_time":1700000000,
        "link":f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn=public-signature",**changes}


class Response:
    def __init__(self,data,status=200):
        self.data,self.status_code,self.content=data,status,b""
    def json(self):return self.data


class HTTP:
    def __init__(self,responses):
        self.responses,self.calls=iter(responses),[]
        self.cookies=requests.cookies.RequestsCookieJar()
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        value=next(self.responses)
        if isinstance(value,Exception):raise value
        return value


class WechatPlatformTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.auth=Path(self.tmp.name)/"session.json"
        self.auth.write_text(json.dumps({"token":"test-token-only","fingerprint":"test-fingerprint",
            "cookies":[{"name":"test_cookie","value":"test-cookie-only","domain":"mp.weixin.qq.com",
                        "path":"/","expires":None}],"saved_at":"test"}),encoding="utf-8")
    def platform(self,responses):
        http=HTTP(responses);return Platform(self.auth,http),http

    def test_search_returns_only_public_fields_and_secure_transport(self):
        api,http=self.platform([Response({"base_resp":{"ret":0},"list":[
            {"fakeid":ACCOUNT,"nickname":"北理就业","alias":"bitjiuye","verify_status":2,
             "unrecognized_secret":"must-not-export"}]})])
        result=api.search("北理就业")
        self.assertEqual(result,[{"fakeid":ACCOUNT,"nickname":"北理就业","alias":"bitjiuye","verify_status":2}])
        url,kwargs=http.calls[0]
        self.assertEqual(url,"https://mp.weixin.qq.com/cgi-bin/searchbiz")
        self.assertTrue(kwargs["verify"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"],(10,30))
        self.assertEqual(kwargs["params"]["action"],"search_biz")
        self.assertEqual(kwargs["params"]["begin"],0)
        self.assertEqual(kwargs["params"]["token"],"test-token-only")
        self.assertEqual(http.cookies.get("test_cookie"),"test-cookie-only")
        self.assertFalse(http.trust_env)
        self.assertNotIn("test-token-only",json.dumps(result))

    def test_nested_publication_groups_expand_every_article(self):
        groups=[{"publish_info":json.dumps({"appmsgex":[article(1,1),article(1,2)]})},
                {"publish_info":{"appmsgex":[article(2,1)]}}]
        api,http=self.platform([Response({"base_resp":{"ret":0},"publish_page":json.dumps({"publish_list":groups,"total_count":7})})])
        page=api.list_page(ACCOUNT,begin=5)
        self.assertEqual(page["groups"],2)
        self.assertEqual(page["article_count"],3)
        self.assertEqual(page["total"],7)
        self.assertEqual(page["total_field"],"publish_page.total_count")
        self.assertNotIn("count",page)
        self.assertEqual({a["origin"] for a in page["articles"]},{"server_platform"})
        self.assertTrue(all(a["body_status"]=="not_fetched" for a in page["articles"]))
        self.assertEqual(http.calls[0][1]["params"]["sub_action"],"list_ex")
        self.assertEqual(http.calls[0][1]["params"]["begin"],5)

    def test_candidate_endpoints_are_explicit_and_never_rotated(self):
        cases={"free_publish":("free_publish_list","action","list"),
               "publish":("publish_list","action","publish_list"),
               "appmsg":("app_msg_list","action","list_ex")}
        for endpoint,(key,param,value) in cases.items():
            api,http=self.platform([Response({"base_resp":{"ret":0},key:[article()]})])
            result=api.list_page(ACCOUNT,endpoint=endpoint)
            self.assertEqual(len(http.calls),1)
            self.assertTrue(http.calls[0][0].endswith('/'+endpoint))
            self.assertEqual(http.calls[0][1]["params"][param],value)
            self.assertEqual(result["article_count"],1)
        api,http=self.platform([])
        with self.assertRaises(PlatformError):api.list_page(ACCOUNT,endpoint="arbitrary-path")
        self.assertEqual(http.calls,[])

    def test_own_account_or_unverifiable_link_is_rejected(self):
        for raw in (article(biz="OtherAccount=="),article(link="https://mp.weixin.qq.com/s/public-short-url")):
            api,http=self.platform([Response({"base_resp":{"ret":0},"free_publish_list":[raw]})])
            with self.assertRaises(PlatformError) as caught:api.list_page(ACCOUNT,endpoint="free_publish")
            self.assertEqual(caught.exception.state,"source_mismatch")
            self.assertEqual(len(http.calls),1)

    def test_rate_limit_is_persisted_and_blocks_new_instances(self):
        with patch('bitfeed.wechat_platform.time.time',return_value=2000000000):
            api,http=self.platform([Response({"base_resp":{"ret":200013}})])
            with self.assertRaises(PlatformError) as caught:api.list_page(ACCOUNT)
            self.assertEqual(caught.exception.state,"rate_limited")
            saved=json.loads(self.auth.with_name('cooldown.json').read_text())
            self.assertEqual(saved,{"until":2000000000+COOLDOWN_SECONDS,"state":"rate_limited"})
            for candidate in ('appmsgpublish','free_publish'):
                with self.assertRaises(PlatformError):api.list_page(ACCOUNT,endpoint=candidate)
            other,other_http=self.platform([])
            with self.assertRaises(PlatformError):other.search('北理就业')
            self.assertEqual(other_http.calls,[])
            self.assertEqual(len(http.calls),1)
        with patch('bitfeed.wechat_platform.time.time',return_value=2000000000+COOLDOWN_SECONDS+1):
            api,http=self.platform([Response({"base_resp":{"ret":0},"app_msg_list":[]})])
            self.assertEqual(api.list_page(ACCOUNT,endpoint='appmsg')["articles"],[])

    def test_expired_session_requires_manual_reauthorization(self):
        api,http=self.platform([Response({"base_resp":{"ret":200003}})])
        with self.assertRaises(PlatformError) as caught:api.search('北理就业')
        self.assertEqual(caught.exception.state,'auth_expired')
        with patch('bitfeed.wechat_platform.time.time',return_value=9999999999):
            other,other_http=self.platform([])
            with self.assertRaises(PlatformError) as caught:other.list_page(ACCOUNT)
            self.assertEqual(caught.exception.state,'auth_expired')
            self.assertEqual(other_http.calls,[])

    def test_transport_errors_do_not_echo_urls_or_tokens(self):
        api,http=self.platform([requests.exceptions.SSLError('https://bad?token=private-value')])
        with self.assertRaises(PlatformError) as caught:api.search('北理就业')
        self.assertEqual(str(caught.exception),'request_failed')
        self.assertNotIn('private-value',str(caught.exception))

    def test_collect_stops_every_account_after_rate_limit(self):
        path=Path(self.tmp.name)/'accounts.json'
        path.write_text(json.dumps({"accounts":[{"fakeid":ACCOUNT,"name":"北理就业"},
            {"fakeid":"OtherAccount==","name":"其他公开号"}]}),encoding='utf-8')
        http=HTTP([Response({"base_resp":{"ret":200013}})])
        result=collect(path,self.auth,session=http)
        self.assertEqual(result['state'],'rate_limited')
        self.assertEqual(len(http.calls),1)
        self.assertTrue(all(s['state']=='rate_limited' for s in result['sources']))
        self.assertFalse(any(s['complete'] for s in result['sources']))

    def test_suspended_upstream_makes_no_requests(self):
        path=Path(self.tmp.name)/'accounts.json'
        path.write_text(json.dumps({"accounts":[{"fakeid":ACCOUNT,"name":"北理就业",
            "enabled":False,"disabled_reason":"upstream_list_unavailable"}]}),encoding='utf-8')
        http=HTTP([])
        result=collect(path,self.auth,session=http)
        self.assertEqual(http.calls,[])
        self.assertEqual(result['sources'][0]['state'],'disabled')
        self.assertEqual(result['sources'][0]['disabled_reason'],'upstream_list_unavailable')
        self.assertFalse(result['sources'][0]['complete'])

    def test_collect_requires_verified_totals_and_detects_repeated_pages(self):
        path=Path(self.tmp.name)/'accounts.json'
        path.write_text(json.dumps({"accounts":[{"fakeid":ACCOUNT,"name":"北理就业"}]}),encoding='utf-8')
        first={"base_resp":{"ret":0},"app_msg_list":[article(i) for i in range(1,6)],"total_count":7}
        second={"base_resp":{"ret":0},"app_msg_list":[article(6),article(7)],"total_count":7}
        http=HTTP([Response(first),Response(second)])
        # Set the one known supported endpoint explicitly in the config.
        path.write_text(json.dumps({"accounts":[{"fakeid":ACCOUNT,"name":"北理就业","endpoint":"appmsg"}]}),encoding='utf-8')
        result=collect(path,self.auth,session=http)
        self.assertEqual(len(result['articles']),7)
        self.assertTrue(result['sources'][0]['complete'])
        self.assertEqual(http.calls[1][1]['params']['begin'],5)
        result=collect(path,self.auth,session=HTTP([Response(first),Response(first)]))
        self.assertEqual(result['sources'][0]['error'],'repeated_page')
        self.assertFalse(result['sources'][0]['complete'])


if __name__=='__main__':unittest.main()
