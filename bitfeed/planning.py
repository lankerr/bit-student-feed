"""Biweekly forward activity planning, anchored to this Friday through two Fridays out."""
from datetime import date, datetime, timedelta
import re

from .activity_time import activity_expired
from .domain import TZ
from . import peer_unions
from .push_config import DEFAULTS, load_config, validate_config, window_dates

CAREER = "北理就业官网·"
EVENT_SOURCES = {"宣讲会", "双选会", "指导活动"}
JOB_SOURCES = {"招聘职位", "实习信息"}
SKIP = re.compile(r"圆满|落幕|收官|回顾|喜报|荣获|祝贺|采购|招标|中标|成交|比选|询价|磋商|更正公告|结果公告|暂缓举办|延期举办|取消举办")
START = re.compile(r"活动开始（官网字段）：(\d{4}-\d{2}-\d{2})")
END = re.compile(r"(?:招聘有效截止|活动结束)（官网字段）：(\d{4}-\d{2}-\d{2})")
STATUS = re.compile(r"官网显示状态（须结合日期核对）：([^\n]+)")
LABELS = {
    "urgent": "本周五前即将截止/举办",
    "week1": "第一周（本周五至下周四）",
    "week2": "第二周（下周五至下下周五）",
    "week3": "延伸周活动",
    "later": "下下周五之后重要长期预告",
    "additional": "补充查阅的其他远期已核验活动",
    "leads": "待核实线索",
}
PARTIAL_DATE = re.compile(
    r"(?:(?P<year>20\d{2})年)?(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?:\s*(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{2})|点))?"
)


def registration_cutoff(body, event):
    """Resolve explicit registration cutoffs against the same article's event year.

    The original quote and inferred year remain visible; unrelated month/day
    mentions and competing registration dates are not promoted to verified dates.
    """
    if not event:
        return None
    anchor = datetime.fromisoformat(event).date()
    found = []
    for sentence in re.split(r"[\n。；！？]", body):
        if not re.search(r"报名|申请|投稿|提交材料|提交作品|材料提交|作品提交", sentence):
            continue
        matches = list(PARTIAL_DATE.finditer(sentence))
        for index, match in enumerate(matches):
            before, after = sentence[:match.start()], sentence[match.end():]
            cutoff = bool(
                re.search(r"(?:截止|截至)(?:时间)?[为至：:\s]*$", before)
                or re.match(r"\s*(?:前|截止)", after)
            )
            # A registration range ends at its second date, not its opening date.
            interval = bool(
                index
                and re.fullmatch(r"\s*[-—–~～至]\s*", sentence[matches[index - 1].end():match.start()])
                and (
                    re.search(r"(?:报名|申请|投稿)(?:时间|日期|时段)?[：:\s]*$", sentence[:matches[index - 1].start()])
                    or re.match(r"\s*期间", after)
                    and re.search(r"注册|填写.{0,20}报考|参加报名", after)
                )
            )
            if not (cutoff or interval):
                continue
            month, day = int(match["month"]), int(match["day"])
            year = int(match["year"]) if match["year"] else anchor.year - (month > anchor.month)
            try:
                dt = datetime(year, month, day, tzinfo=TZ)
                clock = None
                if match["hour"]:
                    hour, minute = int(match["hour"]), int(match["minute"] or 0)
                    clock = dt + timedelta(days=1) if hour == 24 and not minute else dt.replace(hour=hour, minute=minute)
            except ValueError:
                continue
            found.append({
                "day": dt.date().isoformat(),
                "clock": clock,
                "evidence": sentence.strip(),
                "year_inferred": not bool(match["year"]),
            })
    distinct = {(x["day"], x["clock"]) for x in found}
    if len(distinct) > 1:
        return {"ambiguous": True}
    return found[0] if found else None


def window(now, cfg=None, start_date=None, end_date=None):
    local = datetime.fromtimestamp(now, TZ)
    if start_date and end_date:
        start = date.fromisoformat(str(start_date).strip())
        end = date.fromisoformat(str(end_date).strip())
        if end < start:
            raise ValueError("结束日期不能早于开始日期")
    else:
        resolved = validate_config(cfg) if cfg is not None else load_config()[0]
        start, end = window_dates(local.date(), resolved)

    total_days = (end - start).days + 1
    if total_days <= 7:
        weeks = [{"key": "week1", "start": start.isoformat(), "end": end.isoformat()}]
    elif total_days <= 16:
        w1_end = start + timedelta(days=6)
        w2_start = start + timedelta(days=7)
        weeks = [
            {"key": "week1", "start": start.isoformat(), "end": w1_end.isoformat()},
            {"key": "week2", "start": w2_start.isoformat(), "end": end.isoformat()},
        ]
    else:
        weeks = [
            {"key": "week1", "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()},
            {"key": "week2", "start": (start + timedelta(days=7)).isoformat(), "end": (start + timedelta(days=13)).isoformat()},
            {"key": "week3", "start": (start + timedelta(days=14)).isoformat(), "end": end.isoformat()},
        ]
    return {"start": start.isoformat(), "end": end.isoformat(), "weeks": weeks}


def _field(body, *labels):
    for label in labels:
        match = re.search(re.escape(label) + r"\s*[：:]\s*([^\n]+)", body or "")
        if match:
            return match.group(1).strip()[:160]
    return ""


def collect_pool(items, now):
    """Extract body-free verified activity entries and candidate leads."""
    local = datetime.fromtimestamp(now, TZ)
    pool = []
    for item in items:
        title, source = item.get("title", ""), item.get("source", "")
        c = item.get("classification") or {}
        if SKIP.search(title) or c.get("event_status") in ("expired", "recap") or activity_expired(item, local):
            continue
        body = item.get("body") or ""
        if (
            source.startswith(peer_unions.PREFIX)
            and source[len(peer_unions.PREFIX):] in peer_unions.SHARED_OFFICES
            and not peer_unions.UNION_MENTION.search("\n".join((title, body, item.get("digest") or "")))
        ):
            continue
        event, deadline = c.get("event_date"), c.get("deadline")
        kind = source[len(CAREER):] if source.startswith(CAREER) else ""
        is_job = kind in JOB_SOURCES
        verified = bool(c.get("verified_activity"))
        if kind in EVENT_SOURCES:
            match = START.search(body)
            event = match.group(1) if match else None
            verified = bool(event)
            status = STATUS.search(body)
            if status and re.search(r"已结束|已取消|暂停|延期", status.group(1)):
                verified = False
                c = dict(c, planning_note="官网日期在未来，但状态标为" + status.group(1).strip() + "，须向主办方核实")
        if verified and event and not deadline:
            cutoff = registration_cutoff(body, event)
            if cutoff and cutoff.get("ambiguous"):
                verified = False
            elif cutoff:
                deadline = cutoff["day"]
                if cutoff["clock"] and cutoff["clock"] <= local:
                    continue
                c = dict(c, deadline_evidence=cutoff["evidence"], deadline_year_inferred=cutoff["year_inferred"])
        if is_job:
            # A six-month list validity field is not a verified application deadline.
            verified = bool(
                verified
                and deadline
                and item.get("body_status") == "available"
                and "官网字段" not in (c.get("deadline_evidence") or "")
            )
        dates = [day for day in (event, deadline) if day]
        if not verified or not dates:
            end_match = END.search(body)
            official_end = end_match.group(1) if end_match else ""
            useful_title = is_job or kind in EVENT_SOURCES or bool(
                re.search(r"讲座|报名|征稿|论坛|竞赛|大赛|招募|申请|奖学金|宣讲|双选", title)
            )
            years = [int(y) for y in re.findall(r"(?<!\d)20\d{2}(?!\d)", title)]
            if re.search(r"公示|通过名单|评选结果|档案", title) or years and max(years) < local.year:
                useful_title = False
            if useful_title:
                entry = _entry(item, c, "", event, deadline, False)
                entry["_published_at"] = int(item.get("published_at") or 0)
                entry["_official_end"] = official_end
                pool.append(entry)
            continue
        due = min(dates)
        pool.append(_entry(item, c, due, event, deadline, True))
    return pool


def select_from_pool(pool, now, cfg=None, start_date=None, end_date=None):
    local = datetime.fromtimestamp(now, TZ)
    today = local.date().isoformat()
    span = window(now, cfg=cfg, start_date=start_date, end_date=end_date)
    groups = {key: [] for key in LABELS if key != "additional"}
    for raw in pool:
        entry = {k: v for k, v in raw.items() if not k.startswith("_")}
        if not entry.get("date_verified"):
            published_at = int(raw.get("_published_at") or 0)
            official_end = raw.get("_official_end") or ""
            recent = published_at >= now - 21 * 86400
            closing_soon = bool(official_end and today <= official_end <= span["end"])
            if recent or closing_soon:
                groups["leads"].append(entry)
            continue
        dates = [day for day in (entry.get("event_date"), entry.get("deadline")) if day]
        if not dates or any(day < today for day in dates):
            continue
        due = min(dates)
        entry["fact"] = due
        group = "urgent" if due < span["start"] else "later"
        for week in span["weeks"]:
            if week["start"] <= due <= week["end"]:
                group = week["key"]
                break
        groups[group].append(entry)

    # Permanent article id first; a later edition deliberately carries an
    # opportunity again while it remains open. Post age never filters main groups.
    seen = set()
    for key, rows in groups.items():
        rows.sort(key=lambda a: (a["fact"] or "9999", a["title"]))
        unique = []
        for item in rows:
            if item["id"] not in seen:
                unique.append(item)
                seen.add(item["id"])
        groups[key] = unique
    return span, groups


def select(items, now, cfg=None, start_date=None, end_date=None):
    return select_from_pool(
        collect_pool(items, now),
        now,
        cfg=cfg if cfg is not None else DEFAULTS,
        start_date=start_date,
        end_date=end_date,
    )


def weekly_selection(groups):
    """Keep 100% of verified items within the biweekly main window ('所有内容').

    Only items beyond the biweekly window ('later') are filtered to major
    early-preparation opportunities, with the remaining distant items preserved
    in the Word attachment's supplement ('additional').
    """
    result = {key: list(rows) for key, rows in groups.items()}
    result["additional"] = []
    preparation = re.compile(r"征稿|投稿|竞赛|大赛|比赛|揭榜|申报|申请|交换|选调|奖学金|实习")
    chosen = []
    for item in result.get("later", []):
        score = float(item.get("score") or 0)
        academic = item.get("category") in ("academic", "competition")
        needs_preparation = bool(preparation.search(item["title"]))
        worthwhile = needs_preparation and score >= 80 or academic and score >= 85 or score >= 92
        if worthwhile:
            chosen.append(item)
        else:
            result["additional"].append(dict(item, original_section=LABELS["later"]))
    result["later"] = chosen
    return result


def _entry(item, c, due, event, deadline, verified):
    body = item.get("body") or ""
    parts = []
    if event:
        parts.append("活动 " + event)
    if deadline:
        parts.append("报名/申请截止 " + deadline)
    if item["source"].startswith(peer_unions.PREFIX):
        parts.append(peer_unions.eligibility(body))
    is_job = item.get("source", "")[len(CAREER):] in JOB_SOURCES if item.get("source", "").startswith(CAREER) else False
    location = _field(body, "活动地点（官网字段）", "讲座地点", "活动地点", "地点")
    audience = _field(body, "面向对象", "参与对象", "参赛对象", "面向年级", "学历要求")
    note = "时间与报名条件仍待核实，不计入已核验预告" if not verified else (
        "具体投递要求以招聘正文为准" if is_job else "报名资格、名额及临时调整请核对原文"
    )
    if c.get("deadline_year_inferred"):
        note += "；截止日期原文未写年份，按同文活动日期归年，见截止原文"
    if c.get("planning_note"):
        note += "；" + c["planning_note"]
    return {
        "id": item["id"],
        "title": item["title"],
        "url": item["url"],
        "source": item["source"],
        "fact": due,
        "when": "；".join(parts),
        "event_date": event,
        "deadline": deadline,
        "event_evidence": c.get("event_date_evidence") or _field(body, "活动开始（官网字段）"),
        "deadline_evidence": c.get("deadline_evidence") or "",
        "date_verified": verified,
        "deadline_year_inferred": bool(c.get("deadline_year_inferred")),
        "category": c.get("category") or "other",
        "score": c.get("score") or 0,
        "summary": c.get("summary") or "",
        "next_step": c.get("next_step") or "打开原文核对报名和参与要求。",
        "location": location,
        "audience": audience,
        "scope_note": note,
    }
