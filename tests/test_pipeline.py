import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import requests

from bitfeed.classifier import classify, hiagent_stream
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

    def test_guidance_puts_the_soonest_open_date_first(self):
        from bitfeed.export import guidance
        def item(title, deadline, score, expiry=0):
            return {"title":title,"source":"北理就业","url":"https://job.bit.edu.cn/f/recruitmentinfo/show?recruitmentId="+title,
                    "source_expiry":expiry,"classification":{"verified_activity":True,"score":score,"deadline":deadline,
                    "event_date":None,"summary":"招聘","next_step":"打开原文核对应聘要求。"}}
        text, active = guidance({"generated_at":"2026-11-10T12:00:00+08:00","articles":[
            item("较晚的校园招聘","2099-06-01",99),
            item("较早的校园招聘","2099-01-15",10),
            item("官网已经截止","2099-03-01",90,1)]})
        self.assertEqual([a["title"] for a in active], ["较早的校园招聘","较晚的校园招聘"])
        self.assertLess(text.find("较早的校园招聘"), text.find("较晚的校园招聘"))
        self.assertIn("2099年1月15日", text)
        self.assertNotIn("官网已经截止", text)
        self.assertIn("## 更晚仍开放", text)

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

    def test_unknown_publication_keeps_first_seen_across_runs(self):
        original=source(origin="official_server",publication_status="unknown")
        self.store.import_articles([original])
        result=self.store.import_articles([{**original,"published_at":original["published_at"]+60}])
        self.assertEqual(result["changed"],0)
        a=self.store.public_articles()[0]
        self.assertEqual(a["published_at"],original["published_at"])
        self.assertEqual(a["publication_status"],"unknown")
        self.assertEqual(a["origin"],"official_server")

    def test_source_expired_history_does_not_consume_model_calls(self):
        self.store.import_articles([source(source_event_end="2020-01-01 12:00:00")])
        self.assertFalse(self.store.candidates())

    def test_publication_date_and_contradictory_status_cannot_be_recommendations(self):
        for status,body in (("title_and_publication_date","官网列表发布日期（非活动日期）：2099年12月1日"),
                            ("structured_list_metadata","活动开始：2099年12月1日\n官网显示状态（须结合日期核对）：已结束")):
            a=normalize_article(source(body=body,body_status=status))
            c=validate_classification(answer(a,event_date="2099-12-01",event_date_evidence="2099年12月1日"),a)
            self.assertFalse(c["verified_activity"])

    def test_hiagent_uses_fresh_conversation_and_official_blocking_json(self):
        self.store.import_articles([source()])
        a=self.store.candidates()[0]
        contract={"confirmed_from_platform":True,"transport":"hiagent",
            "url":"https://agent.bit.edu.cn/api/proxy/api/v1/chat_query_v2",
            "model":"published-v4", "headers":{"Apikey":"{{api_key}}"},
            "body":{"UserID":"bitfeed-worker","AppConversationID":"{{conversation_id}}",
                    "Query":"{{query}}","ResponseMode":"blocking"},"response_path":"answer"}
        path=Path(self.tmp.name)/'hiagent.json';path.write_text(json.dumps(contract),encoding='utf-8')
        class Response:
            status_code=200
            def __init__(inner,data):inner.data=data
            def json(inner):return inner.data
        class HTTP:
            calls=[]
            def post(inner,url,**kwargs):
                inner.calls.append(url)
                self.assertFalse(kwargs['allow_redirects'])
                if url.endswith('/create_conversation'):
                    return Response({'Conversation':{'AppConversationID':'fresh-conversation'},'BaseResp':None})
                self.assertEqual(kwargs['json']['AppConversationID'],'fresh-conversation')
                self.assertEqual(kwargs['json']['ResponseMode'],'blocking')
                self.assertTrue(kwargs['json']['Query'].startswith('BITFEED_CLASSIFY_V1'))
                self.assertNotIn('test-secret',json.dumps(kwargs['json']))
                return Response({'answer':json.dumps({'items':[answer(a)]}),'total_tokens':100})
        http=HTTP()
        with patch.dict(os.environ,{"BIT_AGENT_API_KEY":"test-secret"}):
            self.assertEqual(classify(self.store,path,http,max_batches=1)['classified'],1)
        self.assertEqual(len(http.calls),2)

    def test_stream_ignores_thoughts_and_rejects_incomplete_answers(self):
        class Response:
            closed=False
            def __init__(inner,events):inner.events=events
            def iter_lines(inner,**kwargs):
                for e in inner.events:yield 'data: '+json.dumps(e,ensure_ascii=False)
            def close(inner):inner.closed=True
        response=Response([{'event':'message_start','id':'main'},
            {'event':'think_message','answer':'不属于最终回答'},
            {'event':'message','id':'child','answer':'不属于本批次'},
            {'event':'message','id':'main','answer':'{"items":'},
            {'event':'message','id':'main','answer':'[]}'}, {'event':'message_end'}])
        self.assertEqual(hiagent_stream(response)['answer'],'{"items":[]}')
        self.assertTrue(response.closed)
        with self.assertRaises(RuntimeError):hiagent_stream(Response([{'event':'message','answer':'partial'}]))

    def test_hallucinated_contact_does_not_enter_recommendation(self):
        a=normalize_article(source())
        c=validate_classification(answer(a,next_step='请拨打010-12345678报名。'),a)
        self.assertEqual(c["event_status"],"needs_verification")
        self.assertFalse(c["verified_activity"])
        self.assertNotIn("010-12345678",c["next_step"])

    def test_model_queue_prioritizes_upcoming_source_evidence_over_undated_archive(self):
        old=source(url='https://cs.bit.edu.cn/old.htm',publication_status='unknown',
            body_status='title_and_publication_date',published_at=int(time.time()))
        upcoming=source(url='https://job.bit.edu.cn/f/recruitmentFair/show?id=1',
            publication_status='unknown',body_status='structured_list_metadata',
            source_event_end='2099-12-01 18:00:00',published_at=int(time.time())-60)
        self.store.import_articles([old,upcoming])
        self.assertEqual(self.store.candidates(1)[0]['url'],upcoming['url'])

    def test_old_full_text_with_a_future_date_is_not_lost_to_the_post_age_cutoff(self):
        self.store.import_articles([
            source(url='https://cs.bit.edu.cn/future-old.htm',published_at=int(time.time())-120*86400,
                   body='论坛征稿截止2099年12月1日，欢迎提交论文摘要'),
            source(url='https://cs.bit.edu.cn/past-old.htm',published_at=int(time.time())-120*86400,
                   body='活动于2020年12月1日举行')])
        queued=self.store.candidates()
        self.assertEqual([a['url'] for a in queued],['https://cs.bit.edu.cn/future-old.htm'])
        a=queued[0]
        self.store.apply_classifications({'items':[answer(a,deadline='2099-12-01',deadline_evidence='2099年12月1日')]},'editor')
        self.assertIn(a['id'],{x['id'] for x in self.store.public_articles(days=90)})

    def test_job_list_validity_does_not_become_a_verified_application_on_the_website(self):
        self.store.import_articles([source(source='北理就业官网·招聘职位',body_status='structured_list_metadata',
            body='招聘有效截止（官网字段）：2099-12-01',source_application_deadline='2099-12-01')])
        a=self.store.candidates()[0]
        self.store.apply_classifications({'items':[answer(a,category='career',deadline='2099-12-01',deadline_evidence='2099-12-01')]},'school')
        c=self.store.public_articles()[0]['classification']
        self.assertFalse(c['verified_activity'])
        self.assertEqual(c['event_status'],'needs_verification')

    def test_model_queue_prioritizes_full_body_weread_over_career_list_backlog(self):
        career=source(url='https://job.bit.edu.cn/f/recruitmentFair/show?id=2',
            publication_status='unknown',body_status='structured_list_metadata',
            source_event_end='2099-12-01 18:00:00',published_at=int(time.time())-30)
        weread=source(url='https://mp.weixin.qq.com/s/fullbody01',origin='weread',
            publication_status='known',body_status='available',body='11月20日讲座报名',
            published_at=int(time.time())-120)
        self.store.import_articles([career,weread])
        self.assertEqual(self.store.candidates(1)[0]['url'],weread['url'])

    def _contract(self):
        path = Path(self.tmp.name)/"contract.json"
        path.write_text(json.dumps({"confirmed_from_platform":True,
            "url":"https://agent.bit.edu.cn/api/proxy/api/v1/chat_query_v2",
            "model":"published-v4","headers":{"Apikey":"{{api_key}}"},
            "body":{"Query":"{{query}}"},"response_path":"answer"}), encoding="utf-8")
        return path

    def test_exhausted_daily_budget_does_not_call_the_model(self):
        self.store.import_articles([source()])
        self.store.reserve_call(1)
        class HTTP:
            calls = []
            def post(self, *args, **kwargs):
                self.calls.append(args)
                raise AssertionError("budget stop must not call the school API")
        with patch.dict(os.environ, {"BIT_AGENT_API_KEY":"test-secret","BIT_AGENT_DAILY_REQUEST_LIMIT":"1"}):
            result = classify(self.store, self._contract(), HTTP(), max_batches=3)
        self.assertEqual(result["state"], "budget")
        self.assertEqual(result["classified"], 0)
        self.assertEqual(result["requests"], 0)

    def test_budget_reached_mid_run_keeps_completed_batches(self):
        self.store.import_articles([source(), source(url="https://cs.bit.edu.cn/second.htm")])
        self.store.reserve_call(2)
        class HTTP:
            def post(self, url, **kwargs):
                query = kwargs["json"]["Query"]
                article_id = json.loads(query.split("数据：",1)[1])[0]["article_id"]
                row = next(a for a in self.store.candidates(5) if a["id"]==article_id)
                class Response:
                    status_code = 200
                    def json(inner):
                        return {"answer": json.dumps({"items":[answer(row)]}), "model":"published-v4"}
                return Response()
        http = HTTP()
        http.store = self.store
        with patch.dict(os.environ, {"BIT_AGENT_API_KEY":"test-secret","BIT_AGENT_DAILY_REQUEST_LIMIT":"2",
                                     "BIT_AGENT_BATCH_SIZE":"1"}):
            result = classify(self.store, self._contract(), http, max_batches=3)
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["classified"], 1)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["reason"], "daily model request budget reached")


if __name__=="__main__":unittest.main()
