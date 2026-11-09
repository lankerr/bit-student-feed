"""Twice-weekly Feishu digest (Thu/Sun 12:00 Beijing time) built from the store."""
from datetime import datetime
import json
import os
import re
import time

from . import feishu
from .domain import TZ

SITE = os.getenv("BITFEED_PUBLIC_URL", "https://lankerr.github.io/bit-student-feed/")
WEEKDAY = "一二三四五六日"
RULES = (("career", re.compile(r"选调|招聘|宣讲|双选|校招|实习|就业|offer|岗位|人才引进|公务员|考公")),
         ("academic", re.compile(r"讲座|报告会|论坛|学术|沙龙|研讨|讲坛|课堂|分享会|青年汇")),
         ("competition", re.compile(r"竞赛|大赛|挑战杯|比赛|创新创业|互联网\+|作品征集|评选")),
         ("volunteer", re.compile(r"志愿|实践|支教|调研团|社会实践")),
         ("campus_service", re.compile(r"奖学金|助学|评优|推免|招生|报名|选课|考试|通知|公告|公示|申请|资助")))
SECTIONS = (("actionable", "近期可参加"), ("career", "升学就业"), ("academic", "学术讲座"),
            ("competition", "竞赛创新"), ("volunteer", "实践志愿"), ("campus_service", "通知与服务"))
SKIP = re.compile(r"圆满|落幕|收官|回顾|喜报|荣获|祝贺|节日快乐|生日快乐|快乐！$")


def _guess(title):
    for category, pattern in RULES:
        if pattern.search(title):
            return category
    return "other"


def _rows(store, since):
    with store.connection() as conn:
        rows = conn.execute('''SELECT a.id,a.title,a.url,a.source,a.published_at,a.first_seen,a.kind,a.origin,
            a.publication_status,a.source_hash,c.payload,c.source_hash AS chash FROM articles a
            LEFT JOIN classifications c ON c.article_id=a.id
            WHERE a.first_seen>? AND ((a.publication_status='known' AND a.published_at>=?)
                OR (a.publication_status='unknown' AND a.origin='weread'))''',
            (since, since - 2 * 86400)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        payload = item.pop("payload")
        item["classification"] = json.loads(payload) if payload and item.pop("chash") == item["source_hash"] else None
        result.append(item)
    return result


def _short(day):
    return day[5:].replace("-", "月", 1) + "日" if day else ""


def build(store, now=None):
    now = now or time.time()
    meta = store.metadata()
    since = int(float(meta.get("digest:window_end") or now - 4 * 86400))
    today = datetime.fromtimestamp(now, TZ).date().isoformat()
    buckets = {key: [] for key, _ in SECTIONS}
    fresh_wechat = {}
    chosen = []
    for item in _rows(store, since):
        c = item["classification"] or {}
        if c.get("event_status") in ("expired", "recap") or SKIP.search(item["title"]):
            continue
        if any(c.get(k) and c[k] < today for k in ("deadline", "event_date")):
            continue
        category = c.get("category") or _guess(item["title"])
        if c.get("verified_activity"):
            buckets["actionable"].append(item)
        elif category in buckets and (c.get("score", 50) >= 35 or not c):
            buckets[category].append(item)
        elif item["kind"] == "wechat":
            fresh_wechat.setdefault(item["source"], []).append(item)
    lines = []
    for key, label in SECTIONS:
        items = buckets[key]
        if key == "actionable":
            items.sort(key=lambda a: ((a["classification"] or {}).get("deadline") or (a["classification"] or {}).get("event_date") or "9999"))
        else:
            items.sort(key=lambda a: (-(a["classification"] or {}).get("score", 40), -a["published_at"]))
        if not items:
            continue
        lines.append(f"**【{label}】**")
        for a in items[:10 if key == "actionable" else 8]:
            c = a["classification"] or {}
            when = "；".join(x for x in (f"截止 {_short(c['deadline'])}" if c.get("deadline") else "",
                                        f"活动 {_short(c['event_date'])}" if c.get("event_date") else "") if x)
            lines.append(f"- [{a['title']}]({a['url']})  {a['source']}" + (f"｜{when}" if when else ""))
            chosen.append(a["id"])
        lines.append("")
    if fresh_wechat:
        lines.append("**【公众号其他新推送】**")
        for source, items in sorted(fresh_wechat.items(), key=lambda kv: -len(kv[1]))[:12]:
            items.sort(key=lambda a: -a["first_seen"])
            links = "；".join(f"[{a['title'][:28]}]({a['url']})" for a in items[:3])
            lines.append(f"- {source}：{links}")
            chosen.extend(a["id"] for a in items[:3])
        lines.append("")
    start = datetime.fromtimestamp(since, TZ)
    end = datetime.fromtimestamp(now, TZ)
    status = json.loads(meta.get("weread_status") or "{}")
    health = f"公众号订阅 {status.get('healthy', 0)}/{status.get('accounts', 0)} 个号正常" if status else "公众号订阅状态待更新"
    header = (f"{start.month}月{start.day}日至{end.month}月{end.day}日新增 {len(chosen)} 条精选。"
              "活动时间与报名条件以原文为准。\n")
    if not chosen:
        header = f"{start.month}月{start.day}日以来没有新的重要消息。\n"
    footer = f"\n[完整列表与 RSS 订阅]({SITE})｜{health}"
    title = f"北理消息汇总 · {end.month}月{end.day}日（周{WEEKDAY[end.weekday()]}）"
    return {"title": title, "markdown": header + "\n" + "\n".join(lines) + footer, "article_ids": chosen,
            "window": [since, int(now)]}


def send(store, mode=None, now=None):
    digest = build(store, now)
    mode = mode or os.getenv("BITFEED_DIGEST_MODE", "test")
    result = feishu.send(store, digest["markdown"], digest["article_ids"] or ["empty:" + str(digest["window"][1] // 86400)],
                         mode=mode, title=digest["title"])
    if result["state"] == "sent":
        store.set_meta("digest:window_end", digest["window"][1])
    store.record_job("feishu_digest", result["state"], json.dumps({"items": len(digest["article_ids"]),
                     "mode": mode, "duplicate": result.get("duplicate", False)}))
    return {**result, "items": len(digest["article_ids"]), "title": digest["title"]}
