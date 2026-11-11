"""Normalize sources and validate model claims against the source, not the model."""
import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

TZ = timezone(timedelta(hours=8))
PROMPT_VERSION = "campus-rank-v1"
CATEGORIES = {"academic", "competition", "career", "volunteer", "campus_service", "other"}
STATES = {"actionable", "needs_verification", "expired", "recap", "not_event"}
RECAP = re.compile(r"圆满收官|圆满落幕|活动回顾|精彩回顾|赛事回顾|回顾[｜|丨]|总结表彰")


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def normalize_url(raw):
    parts = urlsplit(html.unescape(str(raw or "")).strip())
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError("invalid article URL")
    if host != "mp.weixin.qq.com" and host != "bit.edu.cn" and not host.endswith(".bit.edu.cn"):
        raise ValueError("article source must be an official public source")
    if parts.scheme not in ("http", "https"):
        raise ValueError("HTTP(S) article required")
    query = parse_qs(parts.query)
    if host == "mp.weixin.qq.com" and "__biz" in query and "mid" in query:
        query = {k: query[k][0] for k in ("__biz", "mid", "idx", "sn") if k in query}
        return urlunsplit(("https", host, "/s", urlencode(query), ""))
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def article_id(url):
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    identity = "|".join(query.get(k, ["1" if k == "idx" else ""])[0]
                        for k in ("__biz", "mid", "idx")) if "__biz" in query and "mid" in query else url
    return hashlib.sha256(identity.encode()).hexdigest()


def _source_expiry(item):
    expiry = str(item.get("source_event_end") or item.get("source_application_deadline") or "").strip()
    start_only = not expiry
    if start_only:
        # Many careers-site fairs list only a start time; they are over after that day.
        expiry = str(item.get("source_event_start") or "").strip()
    if not expiry:
        return int(item.get("source_expiry") or 0)
    try:
        parsed = datetime.fromisoformat(expiry.replace(" ", "T", 1))
    except (ValueError, TypeError):
        return int(item.get("source_expiry") or 0)
    parsed = parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    # The careers site writes a deadline as midnight at the start of the last day.
    if start_only or (parsed.hour, parsed.minute, parsed.second, parsed.microsecond) == (0, 0, 0, 0):
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(parsed.timestamp())


def normalize_article(item):
    url = normalize_url(item.get("url"))
    title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()[:500]
    if not title:
        raise ValueError("article title required")
    published = item.get("published_at", item.get("ts"))
    if published is None and item.get("date"):
        published = int(datetime.fromisoformat(item["date"]).replace(tzinfo=TZ).timestamp())
    published = int(published or 0)
    if published <= 0 or published > int(datetime.now(TZ).timestamp()) + 86400:
        raise ValueError("invalid publication date")
    body = str(item.get("body") or "")[:18000]
    digest = str(item.get("digest") or "")[:1000]
    source = str(item.get("source") or item.get("group") or "未注明来源")[:120]
    source_hash = hashlib.sha256(json.dumps([title, published, body, digest], ensure_ascii=False).encode()).hexdigest()
    source_expiry = _source_expiry(item)
    return {"id": article_id(url), "title": title, "url": url, "source": source,
        "published_at": published, "kind": item.get("kind", "wechat"), "body": body,
        "digest": digest, "source_hash": source_hash,
        "origin": str(item.get("origin") or "unknown")[:40],
        "publication_status": "unknown" if item.get("publication_status") == "unknown" else "known",
        "source_expiry": source_expiry,
        "body_status": item.get("body_status", "available" if body else "not_fetched")}


def clean_model_json(value):
    if isinstance(value, dict):
        parsed = value
    else:
        value = str(value).strip()
        if value.startswith("```"):
            value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value)
        parsed = json.loads(value)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        raise ValueError("classifier must return an object with items")
    return parsed


def _verified_day(value, evidence, source):
    if value is None:
        return None
    try:
        day = date.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if not evidence or evidence not in source:
        return None
    variants = (day.isoformat(), f"{day.year}年{day.month}月{day.day}日",
                f"{day.year}/{day.month}/{day.day}", f"{day.year}.{day.month}.{day.day}")
    return value if any(token in evidence for token in variants) else None


def validate_classification(raw, article):
    allowed = {"article_id", "source_hash", "category", "event_status", "score", "confidence",
               "summary", "next_step", "reason", "deadline", "deadline_evidence",
               "event_date", "event_date_evidence"}
    if set(raw) - allowed:
        raise ValueError("unexpected model fields")
    if raw.get("article_id") != article["id"] or raw.get("source_hash") != article["source_hash"]:
        raise ValueError("classification does not match the source fingerprint")
    if raw.get("category") not in CATEGORIES or raw.get("event_status") not in STATES:
        raise ValueError("invalid classifier category/state")
    score, confidence = raw.get("score"), raw.get("confidence")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
        raise ValueError("score must be 0..100")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be 0..1")
    source = "\n".join([article["title"], article["digest"], article["body"]])
    invented_contact = False
    for field in ("summary", "next_step", "reason"):
        statement = str(raw.get(field) or "")
        contacts = re.findall(
            r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}[-－ ]?\d{7,8}(?!\d)",
            statement)
        for contact in contacts:
            if contact not in source and contact != article["url"]:
                statement = statement.replace(contact, "")
                invented_contact = True
        raw[field] = re.sub(r"\s{2,}", " ", statement).strip(" ，,;；")
    deadline = _verified_day(raw.get("deadline"), raw.get("deadline_evidence"), source)
    event_date = _verified_day(raw.get("event_date"), raw.get("event_date_evidence"), source)
    state = raw["event_status"]
    today = datetime.now(TZ).date().isoformat()
    if invented_contact:
        # Keep the rest of the batch; never recommend a model-invented phone/link.
        state, score, confidence = "needs_verification", min(score, 40), min(confidence, 0.5)
    elif RECAP.search(article["title"]):
        state, score = "recap", min(score, 15)
    elif (deadline and deadline < today) or (event_date and event_date < today):
        state, score = "expired", min(score, 10)
    elif state == "actionable" and (not (deadline or event_date) or confidence < .8):
        state, score = "needs_verification", min(score, 40)
    if state == "actionable" and (article["body_status"] in {"title_and_publication_date", "not_fetched"} or
            re.search(r"官网显示状态[^\n]*[：:][^\n]*(已结束|已取消|已过期)", source)):
        state, score = "needs_verification", min(score, 40)
    verified = state == "actionable" and bool(deadline or event_date) and confidence >= .8
    return {"article_id": article["id"], "source_hash": article["source_hash"],
        "category": raw["category"], "event_status": state, "score": round(score, 1),
        "confidence": confidence, "summary": str(raw.get("summary") or "")[:180],
        "next_step": str(raw.get("next_step") or "打开原文核实具体安排。")[:160],
        "reason": str(raw.get("reason") or "")[:180], "deadline": deadline,
        "deadline_evidence": str(raw.get("deadline_evidence") or "")[:160] if deadline else "",
        "event_date": event_date, "event_date_evidence": str(raw.get("event_date_evidence") or "")[:160] if event_date else "",
        "verified_activity": verified, "prompt_version": PROMPT_VERSION, "validated_at": now_iso()}
