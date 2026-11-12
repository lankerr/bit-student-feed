"""Forward activity planning, anchored to the next Monday rather than post age."""
from datetime import datetime, timedelta
import re

from .activity_time import activity_expired
from .domain import TZ

CAREER = "北理就业官网·"
EVENT_SOURCES = {"宣讲会", "双选会", "指导活动"}
JOB_SOURCES = {"招聘职位", "实习信息"}
SKIP = re.compile(r"圆满|落幕|收官|回顾|喜报|荣获|祝贺|采购|招标|中标|成交|比选|询价|磋商|更正公告|结果公告|暂缓举办|延期举办|取消举办")
START = re.compile(r"活动开始（官网字段）：(\d{4}-\d{2}-\d{2})")
END = re.compile(r"(?:招聘有效截止|活动结束)（官网字段）：(\d{4}-\d{2}-\d{2})")
LABELS = {"urgent": "下周前需办理", "week1": "下周", "week2": "下下周", "week3": "下下下周",
          "later": "更长期预告与开放申请", "leads": "待核实线索"}


def window(now):
    local = datetime.fromtimestamp(now, TZ)
    monday = local.date() + timedelta(days=7-local.weekday())
    end = monday + timedelta(days=20)
    weeks = [{"key": "week"+str(i+1), "start": (monday+timedelta(days=7*i)).isoformat(),
              "end": (monday+timedelta(days=7*i+6)).isoformat()} for i in range(3)]
    return {"start": monday.isoformat(), "end": end.isoformat(), "weeks": weeks}


def _field(body, *labels):
    for label in labels:
        match = re.search(re.escape(label)+r"\s*[：:]\s*([^\n]+)", body or "")
        if match:
            return match.group(1).strip()[:160]
    return ""


def select(items, now):
    local = datetime.fromtimestamp(now, TZ)
    today = local.date().isoformat()
    span = window(now)
    groups = {key: [] for key in LABELS}
    for item in items:
        title, source = item.get("title", ""), item.get("source", "")
        c = item.get("classification") or {}
        if SKIP.search(title) or c.get("event_status") in ("expired", "recap") or activity_expired(item, local):
            continue
        body = item.get("body") or ""
        event, deadline = c.get("event_date"), c.get("deadline")
        kind = source[len(CAREER):] if source.startswith(CAREER) else ""
        is_job = kind in JOB_SOURCES
        verified = bool(c.get("verified_activity"))
        if kind in EVENT_SOURCES:
            match = START.search(body)
            event = match.group(1) if match else None
            verified = bool(event)
        if is_job:
            # A six-month list validity field is not a verified application deadline.
            verified = bool(verified and deadline and item.get("body_status") == "available"
                            and "官网字段" not in (c.get("deadline_evidence") or ""))
        dates = [day for day in (event, deadline) if day]
        if not verified or not dates:
            recent = item.get("published_at", 0) >= now-21*86400
            end_match=END.search(body)
            closing_soon=bool(end_match and today<=end_match.group(1)<=span['end'])
            useful_title = is_job or re.search(r"讲座|报名|征稿|论坛|竞赛|大赛|招募|申请|奖学金|宣讲|双选", title)
            if (recent or closing_soon) and useful_title:
                groups["leads"].append(_entry(item, c, "", event, deadline, False))
            continue
        if any(day < today for day in dates):
            continue
        due = min(dates)
        group = "urgent" if due < span["start"] else "later"
        for week in span["weeks"]:
            if week["start"] <= due <= week["end"]:
                group = week["key"]
                break
        groups[group].append(_entry(item, c, due, event, deadline, True))
    # Permanent article id first; a later weekly edition deliberately carries an
    # opportunity again while it remains open. Post age never filters main groups.
    seen = set()
    for key, rows in groups.items():
        rows.sort(key=lambda a: (a["fact"] or "9999", a["title"]))
        unique=[]
        for item in rows:
            if item["id"] not in seen:
                unique.append(item)
                seen.add(item["id"])
        groups[key]=unique
    return span, groups


def _entry(item, c, due, event, deadline, verified):
    body = item.get("body") or ""
    parts = []
    if event:
        parts.append("活动 "+event)
    if deadline:
        parts.append("报名/申请截止 "+deadline)
    is_job = item.get("source", "")[len(CAREER):] in JOB_SOURCES if item.get("source", "").startswith(CAREER) else False
    location = _field(body, "活动地点（官网字段）", "讲座地点", "活动地点", "地点")
    audience = _field(body, "面向对象", "参与对象", "参赛对象", "面向年级", "学历要求")
    note = "时间与报名条件仍待核实，不计入已核验预告" if not verified else (
        "具体投递要求以招聘正文为准" if is_job else "报名资格、名额及临时调整请核对原文")
    return {"id": item["id"], "title": item["title"], "url": item["url"], "source": item["source"],
            "fact": due, "when": "；".join(parts), "event_date": event, "deadline": deadline,
            "event_evidence": c.get("event_date_evidence") or _field(body, "活动开始（官网字段）"),
            "deadline_evidence": c.get("deadline_evidence") or "", "date_verified": verified,
            "summary": c.get("summary") or "", "next_step": c.get("next_step") or "打开原文核对报名和参与要求。",
            "location": location, "audience": audience, "scope_note": note}
