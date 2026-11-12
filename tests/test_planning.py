from datetime import datetime, timedelta
import unittest

from bitfeed.domain import TZ
from bitfeed.planning import select, window
from bitfeed.store import Store
from bitfeed import digest
from pathlib import Path
import tempfile
import json
from unittest.mock import patch


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,14,12,tzinfo=TZ).timestamp()

    def item(self,name,day,deadline=None,published_days_ago=60,**extra):
        return {"id":name,"title":name,"url":"https://mp.weixin.qq.com/s/"+name,
                "source":"学院","published_at":self.now-published_days_ago*86400,
                "classification":{"verified_activity":True,"event_status":"actionable","event_date":day,
                                  "deadline":deadline,"summary":"","next_step":"核对原文"},**extra}

    def test_saturday_covers_next_monday_through_third_sunday(self):
        span=window(self.now)
        self.assertEqual((span["start"],span["end"]),("2026-11-16","2026-12-06"))
        self.assertEqual(span['weeks'][1],{'key':'week2','start':'2026-11-23','end':'2026-11-29'})
        year=window(datetime(2026,12,26,12,tzinfo=TZ).timestamp())
        self.assertEqual((year['start'],year['end']),('2026-12-28','2027-01-17'))

    def test_old_post_and_all_calendar_boundaries_are_kept(self):
        items=[self.item('old-publication','2026-11-16'),self.item('week-one-end','2026-11-22'),
               self.item('week-two-start','2026-11-23'),self.item('third-sunday','2026-12-06'),
               self.item('beyond-three-weeks','2026-12-07')]
        _,g=select(items,self.now)
        self.assertEqual({a['id'] for a in g['week1']},{'old-publication','week-one-end'})
        self.assertEqual([a['id'] for a in g['week2']],['week-two-start'])
        self.assertEqual([a['id'] for a in g['week3']],['third-sunday'])
        self.assertEqual([a['id'] for a in g['later']],['beyond-three-weeks'])

    def test_application_date_is_the_action_node_and_closed_registration_is_excluded(self):
        items=[self.item('apply-this-weekend','2026-12-20','2026-11-15'),
               self.item('apply-in-second-week','2027-01-20','2026-11-25'),
               self.item('registration-closed','2026-11-20','2026-11-13'),
               self.item('活动回顾','2026-11-20'),self.item('already-over','2026-11-13')]
        _,g=select(items,self.now)
        self.assertEqual([a['id'] for a in g['urgent']],['apply-this-weekend'])
        self.assertEqual([a['id'] for a in g['week2']],['apply-in-second-week'])
        self.assertEqual(sum(len(v) for v in g.values()),2)

    def test_a_list_validity_field_is_not_a_verified_job_deadline(self):
        job=self.item('job-lead',None,'2026-12-01',published_days_ago=2,
            source='北理就业官网·招聘职位',body_status='structured_list_metadata',
            body='招聘有效截止（官网字段）：2026-12-01 00:00:00')
        job['classification']['deadline_evidence']='招聘有效截止（官网字段）：2026-12-01'
        _,g=select([job],self.now)
        self.assertEqual([a['id'] for a in g['leads']],['job-lead'])
        self.assertEqual(sum(len(g[k]) for k in g if k!='leads'),0)

    def test_clock_time_and_exact_official_end_override_day_only_claims(self):
        item=self.item('finished-this-morning','2026-11-14')
        item['classification']['event_date_evidence']='2026年11月14日 10:00-11:00'
        _,g=select([item],self.now)
        self.assertFalse(any(g.values()))
        talk=self.item('official-future',None,source='北理就业官网·宣讲会',
                       body='活动开始（官网字段）：2026-11-24 14:00:00')
        _,g=select([talk],self.now)
        self.assertEqual([a['id'] for a in g['week2']],['official-future'])

    def test_an_open_opportunity_returns_in_the_next_weekly_edition(self):
        item=self.item('still-open',None,'2026-12-01')
        _,first=select([item],self.now)
        _,second=select([item],self.now+7*86400)
        self.assertEqual([a['id'] for a in first['week3']],['still-open'])
        self.assertEqual([a['id'] for a in second['week2']],['still-open'])

    def test_partial_registration_deadline_expires_before_the_future_event(self):
        item=self.item('操舞比赛','2026-11-28',body='各队伍选出一名领队于11月13日前填写问卷报名，并扫码进通知群。')
        _,thursday=select([item],datetime(2026,11,12,12,tzinfo=TZ).timestamp())
        row=thursday['urgent'][0]
        self.assertEqual(row['deadline'],'2026-11-13')
        self.assertIn('未写年份',row['scope_note'])
        self.assertIn('11月13日前',row['deadline_evidence'])
        _,saturday=select([item],self.now)
        self.assertFalse(any(saturday.values()))

    def test_registration_range_uses_its_end_and_keeps_the_original_quote(self):
        item=self.item('选调报名','2026-12-06',body='考生于2026年11月10日10:00—11月20日20:00期间登录网上报名服务平台进行注册。')
        _,g=select([item],self.now)
        self.assertEqual(g['week1'][0]['deadline'],'2026-11-20')

    def test_future_official_date_with_closed_status_needs_verification(self):
        item=self.item('冬季双选会',None,published_days_ago=2,source='北理就业官网·双选会',
                       body='活动开始（官网字段）：2026-11-25 14:00:00\n官网显示状态（须结合日期核对）：已结束')
        _,g=select([item],self.now)
        self.assertEqual([a['id'] for a in g['leads']],['冬季双选会'])
        self.assertFalse(g['week2'])

    def test_recent_import_does_not_make_old_or_result_notices_opportunities(self):
        items=[self.item('2024年奖学金申请通知',None,published_days_ago=1),
               self.item('2026年奖学金评选结果公示',None,published_days_ago=1),
               self.item('2026年学术论坛报名通知',None,published_days_ago=1)]
        _,g=select(items,self.now)
        self.assertEqual([a['id'] for a in g['leads']],['2026年学术论坛报名通知'])

    def test_partial_deadline_year_rollover_clock_and_ambiguity(self):
        item=self.item('跨年报名','2027-01-10',body='报名截止：12月31日17:00')
        before=datetime(2026,12,31,16,tzinfo=TZ).timestamp()
        _,g=select([item],before)
        self.assertEqual(g['urgent'][0]['deadline'],'2026-12-31')
        _,g=select([item],before+3600)
        self.assertFalse(any(g.values()))
        uncertain=self.item('大赛报名','2026-11-28',published_days_ago=2,body='本科生报名截止11月20日，研究生报名截止11月22日。')
        _,g=select([uncertain],self.now)
        self.assertEqual([a['id'] for a in g['leads']],['大赛报名'])

    def test_preview_generation_has_no_delivery_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'data')
            store.import_articles([{'title':'长期征稿','url':'https://cs.bit.edu.cn/future.htm',
                'source':'学院','published_at':int(self.now)-120*86400,'body':'投稿截止2099年12月1日'}])
            a=store.candidates()[0]
            store.apply_classifications({'items':[{'article_id':a['id'],'source_hash':a['source_hash'],
                'category':'academic','event_status':'actionable','score':90,'confidence':.95,
                'summary':'征稿信息','next_step':'核对原文投稿','reason':'截止时间明确',
                'deadline':'2099-12-01','deadline_evidence':'2099年12月1日','event_date':None,'event_date_evidence':''}]},'editor')
            folder=Path(tmp)/'weekly'
            with patch('bitfeed.feishu.send',side_effect=AssertionError('preview must not send')):
                summary=digest.write_preview(store,now=self.now,use_model=False,directory=folder)
            self.assertEqual(summary['verified'],1)
            self.assertEqual(summary['period'],'2026-11-16至2026-12-06')
            self.assertTrue((folder/'bit-preview-latest.docx').is_file())
            public=json.loads((folder/'preview.json').read_text(encoding='utf8'))
            self.assertNotIn('body',public['groups']['later'][0])
            self.assertEqual(json.loads(store.metadata()['weekly_preview'])['verified'],1)
