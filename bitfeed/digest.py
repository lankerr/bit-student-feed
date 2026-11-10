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
RULES = (("campus_service", re.compile(r"奖学金|助学金|评优|优秀学生|推免|资助")),
         ("career", re.compile(r"选调|招聘|宣讲|双选|校招|实习|就业|offer|岗位|人才引进|公务员|考公")),
         ("academic", re.compile(r"讲座|报告会|论坛|学术|沙龙|研讨|讲坛|课堂|分享会|青年汇")),
         ("competition", re.compile(r"竞赛|大赛|挑战杯|比赛|创新创业|互联网\+|作品征集")),
         ("volunteer", re.compile(r"志愿|实践|支教|调研团|社会实践")),
         ("campus_service", re.compile(r"招生|报名|选课|考试|通知|公告|公示|申请")))
SECTIONS = (("actionable", "近期可参加"), ("scholarship", "奖学金与评优"), ("career", "升学就业"),
            ("academic", "学术讲座"), ("competition", "竞赛创新"), ("volunteer", "实践志愿"),
            ("campus_service", "通知与服务"))
SKIP = re.compile(r"圆满|落幕|收官|回顾|喜报|荣获|祝贺|节日快乐|生日快乐|快乐！$|"
                  r"采购|招标|中标|成交|比选|询价|磋商|更正公告|结果公告")
SCHOLAR = re.compile(r"奖学金|助学金|评优|优秀学生|推免|资助")
CARD_LIMIT = 8000


def _guess(title):
    for category, pattern in RULES:
        if pattern.search(title):
            return category
    return "other"


def _rows(store, since):
    with store.connection() as conn:
        rows = conn.execute('''SELECT a.id,a.title,a.url,a.source,a.published_at,a.first_seen,a.kind,a.origin,
            a.publication_status,a.source_hash,a.body,a.source_expiry,c.payload,c.source_hash AS chash FROM articles a
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
    """Keep the year. A May date in a November card is usually next year, not last May."""
    if not day or len(day) < 10:
        return ""
    return f"{day[:4]}年{int(day[5:7])}月{int(day[8:10])}日"


def _official_day(item):
    """Employment-site end date wins over a model date when the collector stored one."""
    expiry = int(item.get("source_expiry") or 0)
    if expiry > 0:
        return datetime.fromtimestamp(expiry, TZ).date().isoformat()
    body = item.get("body") or ""
    match = re.search(r"(?:招聘有效截止|活动结束)（官网字段）：(\d{4}-\d{2}-\d{2})", body)
    return match.group(1) if match else ""


def _due(item):
    official = _official_day(item)
    if official:
        return official
    return (item.get("classification") or {}).get("deadline") or ""


def build(store, now=None):
    now = now or time.time()
    meta = store.metadata()
    since = int(float(meta.get("digest:window_end") or now - 4 * 86400))
    today = datetime.fromtimestamp(now, TZ).date().isoformat()
    buckets = {key: [] for key, _ in SECTIONS}
    fresh_wechat = {}
    for item in _rows(store, since):
        c = item["classification"] or {}
        if c.get("event_status") in ("expired", "recap") or SKIP.search(item["title"]):
            continue
        official = _official_day(item)
        if official and official < today:
            continue
        if any(c.get(k) and c[k] < today for k in ("deadline", "event_date")):
            continue
        if SCHOLAR.search(item["title"]):
            buckets["scholarship"].append(item)
            continue
        category = c.get("category") or _guess(item["title"])
        if c.get("verified_activity"):
            buckets["actionable"].append(item)
        elif category in buckets and (c.get("score", 50) >= 35 or not c):
            buckets[category].append(item)
        elif item["kind"] == "wechat" or item.get("origin") == "weread":
            fresh_wechat.setdefault(item["source"], []).append(item)
    sections = []
    for key, label in SECTIONS:
        items = buckets[key]
        if key in ("actionable", "career"):
            items.sort(key=lambda a: (_due(a) or (a["classification"] or {}).get("event_date") or "9999", -a["published_at"]))
        else:
            items.sort(key=lambda a: (-(a["classification"] or {}).get("score", 40), -a["published_at"]))
        if not items:
            continue
        pairs = []
        for a in items:
            c = a["classification"] or {}
            when = "；".join(x for x in (f"截止 {_short(_due(a))}" if _due(a) else "",
                                        f"活动 {_short(c['event_date'])}" if c.get("event_date") else "") if x)
            pairs.append((f"- [{a['title']}]({a['url']})  {a['source']}" + (f"｜{when}" if when else ""), a["id"]))
        sections.append((label, pairs))
    if fresh_wechat:
        pairs = []
        for source, items in sorted(fresh_wechat.items(), key=lambda kv: -max(a["first_seen"] for a in kv[1])):
            items.sort(key=lambda a: -a["first_seen"])
            for a in items:
                pairs.append((f"- {source}：[{a['title']}]({a['url']})", a["id"]))
        sections.append(("公众号其他新推送", pairs))
    start = datetime.fromtimestamp(since, TZ)
    end = datetime.fromtimestamp(now, TZ)
    status = json.loads(meta.get("weread_status") or "{}")
    health = f"公众号订阅 {status.get('healthy', 0)}/{status.get('accounts', 0)} 个号正常" if status else "公众号订阅状态待更新"
    chosen = [identifier for _, pairs in sections for _, identifier in pairs]
    header = (f"{start.month}月{start.day}日至{end.month}月{end.day}日新增 {len(chosen)} 条。"
              "招标公告和活动回顾未列入。活动时间与报名条件以原文为准。\n")
    if not chosen:
        header = f"{start.month}月{start.day}日以来没有新的重要消息。\n"
    footer = f"\n[完整列表与 RSS 订阅]({SITE})｜{health}"
    title = f"北理消息汇总 · {end.month}月{end.day}日（周{WEEKDAY[end.weekday()]}）"
    cards = _cards(header, sections, footer)
    markdown = header + "\n" + "\n".join(
        "**【" + label + "】**\n" + "\n".join(line for line, _ in pairs) + "\n" for label, pairs in sections) + footer
    return {"title": title, "markdown": markdown, "article_ids": chosen, "cards": cards,
            "window": [since, int(now)]}


def _cards(header, sections, footer):
    """Split a digest into Feishu cards. One card silently drops text past 12,000 characters."""
    cards, lines, ids = [], [], []

    def flush():
        if lines:
            cards.append((list(lines), list(ids)))
        lines.clear()
        ids.clear()

    for label, pairs in sections:
        pending = list(pairs)
        while pending:
            heading = f"**【{label}】**"
            used = len(header) + len(footer) + len("\n".join(lines)) + len(heading) + 4
            room = CARD_LIMIT - used
            if lines and room < 180:
                flush()
                continue
            take = []
            for pair in pending:
                if take and len(pair[0]) + 1 > room:
                    break
                take.append(pair)
                room -= len(pair[0]) + 1
            if not take:
                take = [pending[0]]
            lines.append(heading)
            for line, identifier in take:
                lines.append(line)
                ids.append(identifier)
            lines.append("")
            pending = pending[len(take):]
            if pending:
                flush()
    flush()
    if not cards:
        cards.append(([], []))
    rendered = []
    count = len(cards)
    for index, (body, card_ids) in enumerate(cards, 1):
        text = (header if index == 1 else "") + "\n".join(body) + (footer if index == count else "")
        rendered.append({"markdown": text.strip() + ("\n" if text.strip() else ""), "article_ids": card_ids,
                         "index": index, "count": count})
    return rendered


def send(store, mode=None, now=None):
    digest = build(store, now)
    mode = mode or os.getenv("BITFEED_DIGEST_MODE", "test")
    cards = digest["cards"] or [{"markdown": digest["markdown"], "article_ids": [], "index": 1, "count": 1}]
    results = []
    for card in cards:
        title = digest["title"] if card["count"] == 1 else f"{digest['title']}（{card['index']}/{card['count']}）"
        identifiers = card["article_ids"] or ["empty:" + str(digest["window"][1] // 86400)]
        results.append(feishu.send(store, card["markdown"], identifiers, mode=mode, title=title))
    delivered = [item for item in results if item["state"] == "sent" or item.get("duplicate")]
    if any(item["state"] == "sent" for item in results) and len(delivered) == len(results):
        store.set_meta("digest:window_end", digest["window"][1])
    state = "sent" if delivered and len(delivered) == len(results) else results[-1]["state"]
    result = {"state": state, "duplicate": all(item.get("duplicate") for item in results),
              "items": len(digest["article_ids"]), "title": digest["title"], "cards": len(results)}
    store.record_job("feishu_digest", state, json.dumps({"items": result["items"], "mode": mode,
                     "cards": len(results), "duplicate": result["duplicate"]}))
    return result
