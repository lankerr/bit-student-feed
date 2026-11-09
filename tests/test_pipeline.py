import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import requests

from bitfeed.classifier import classify
from bitfeed.domain import normalize_article, validate_classification
from bitfeed.export import build, public_data
from bitfeed.feishu import send
from bitfeed.store import Store


def source(**changes):
    return {"title":"学术讲座报名通知", "url":"https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=old",
            "source":"测试官方来源", "published_at":int(time.time()), **changes}


def answer(a,**changes):
    return {"article_id":a["id"],"source_hash":a["source_hash"],"category":"academic",
        "event_status":"actionable","score":90,"confidence":.95,"summary":"讲座通知",
        "next_step":"核对原文报名要求。","reason":"近期讲座","deadline":None,
        "deadline_evidence":"","event_date":None,"event_date_evidence":"",**changes}


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/"data")

    def test_dedup_signature_change_and_body_preservation(self):
        original=source()
        self.store.import_articles([{**original,"body":"正文原始内容"}])
        a=self.store.candidates()[0]
        self.store.apply_classifications({"items":[answer(a)]},"school",[a["id"]])
        result=self.store.import_articles([{**original,"url":original["url"].replace('old','new')}])
        self.assertEqual(result["added"],0)
        self.assertEqual(result["changed"],0)
        self.assertFalse(self.store.candidates())

    def test_no_date_no_recommendation_and_recap_override(self):
        a=normalize_article(source())
        self.assertFalse(validate_classification(answer(a),a)["verified_activity"])
        a=normalize_article(source(title="大赛圆满收官",body="2099年12月1日"))
        c=validate_classification(answer(a,event_date="2099-12-01",event_date_evidence="2099年12月1日"),a)
        self.assertEqual(c["event_status"],"recap")
        a=normalize_article(source(body="2099年12月1日讲座"))
        c=validate_classification(answer(a,event_date="2099-12-01",event_date_evidence="2099年12月1日"),a)
        self.assertTrue(c["verified_activity"])
        c=validate_classification(answer(a,event_date="2099-12-02",event_date_evidence="2099年12月2日"),a)
        self.assertFalse(c["verified_activity"])

    def test_classifier_batch_validation_is_atomic(self):
        self.store.import_articles([source(),source(url="https://cs.bit.edu.cn/test.htm")])
        a,b=self.store.candidates()
        with self.assertRaises(ValueError):
            self.store.apply_classifications({"items":[answer(a),answer(b,source_hash="forged")]},"school")
        self.assertEqual(len(self.store.candidates()),2)
        with self.assertRaises(ValueError):
            self.store.apply_classifications({"items":[answer(a)]},"school",[a["id"],b["id"]])

    def test_public_export_excludes_body_and_credentials_and_keeps_pending(self):
        self.store.import_articles([source(body="public article body")])
        data=public_data(self.store)
        self.assertEqual(data["classifier"]["pending"],1)
        self.assertNotIn("body",data["articles"][0])
        dest=Path(self.tmp.name)/"site"
        result=build(data,dest)
        self.assertEqual(result["verified_activities"],0)
        self.assertTrue((dest/"feed.xml").exists())
        self.assertIn("当前没有通过",(dest/"daily.md").read_text(encoding="utf-8"))

    def test_no_key_no_network_and_call_budget(self):
        with patch.dict(os.environ,{"BIT_AGENT_API_KEY":""}):
            self.assertEqual(classify(self.store)["state"],"pending_configuration")
        self.store.reserve_call(1)
        with self.assertRaises(ValueError):self.store.reserve_call(1)

    def test_school_api_contract_real_transport_and_rejection(self):
        self.store.import_articles([source()])
        a=self.store.candidates()[0]
        contract={"confirmed_from_platform":True,"url":"https://agent.bit.edu.cn/api/test-contract",
            "model":"school-test","headers":{"x-key":"{{api_key}}"},
            "body":{"question":"{{query}}","app":"{{app_id}}"},"response_path":"data.answer"}
        path=Path(self.tmp.name)/"contract.json"
        path.write_text(json.dumps(contract),encoding="utf-8")
        class HTTP:
            called=False
            def post(inner,url,**kwargs):
                inner.called=True
                self.assertEqual(kwargs["headers"]["x-key"],"test-secret")
                self.assertFalse(kwargs["allow_redirects"])
                self.assertNotIn("test-secret",json.dumps(kwargs["json"]))
                class Response:
                    status_code=200
                    def json(inner):return {"data":{"answer":json.dumps({"items":[answer(a)]})}}
                return Response()
        http=HTTP()
        with patch.dict(os.environ,{"BIT_AGENT_API_KEY":"test-secret"}):
            self.assertEqual(classify(self.store,path,http)["classified"],1)
            self.assertTrue(http.called)
            contract["url"]="https://example.com/api"
            path.write_text(json.dumps(contract),encoding="utf-8")
            with self.assertRaises(ValueError):classify(self.store,path,http)

    def test_feishu_timeout_is_unknown_and_deduplicated(self):
        class HTTP:
            calls=0
            def post(inner,*args,**kwargs):
                inner.calls+=1
                raise requests.Timeout()
        http=HTTP()
        with patch.dict(os.environ,{"FEISHU_TEST_WEBHOOK":"https://open.feishu.cn/open-apis/bot/v2/hook/test-only"}):
            result=send(self.store,"测试",["article-1"],session=http)
            self.assertEqual(result["state"],"unknown")
            self.assertTrue(send(self.store,"测试",["article-1"],session=http)["duplicate"])
            self.assertEqual(http.calls,1)
            with self.assertRaises(ValueError):send(self.store,"测试",["article-1"],mode="production",session=http)

    def test_reject_untrusted_source(self):
        with self.assertRaises(ValueError):self.store.import_articles([source(url="https://evil.example/a")])


if __name__=="__main__":unittest.main()
