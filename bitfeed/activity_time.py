"""Expire activities using clock times quoted from their original date evidence."""
import re
from datetime import datetime, timedelta

from .domain import TZ


def evidence_end(day, evidence, deadline=False):
    if not day or not evidence:
        return None
    try:
        date = datetime.fromisoformat(day).replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None
    year, month, number = date.year, date.month, date.day
    pattern = rf"{year}(?:年|[-/.])0?{month}(?:月|[-/.])0?{number}(?:日)?"
    match = re.search(pattern, evidence)
    if not match:
        return None
    tail = evidence[match.end():]
    # A multi-day range needs its own end date; never reuse the first day's date.
    if re.search(r"\d{4}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}", tail):
        return None
    times = re.findall(r"(?<!\d)([0-2]?\d)[:：]([0-5]\d)(?!\d)", tail)
    if not times or (not deadline and len(times) < 2):
        return None
    if not deadline and not re.search(r"[:：][0-5]\d\s*[-—–~～至]\s*[0-2]?\d[:：]", tail):
        return None
    hour, minute = map(int, times[-1])
    if hour > 24 or (hour == 24 and minute):
        return None
    if hour == 24:
        return date + timedelta(days=1)
    result = date.replace(hour=hour, minute=minute)
    # A range across midnight is ambiguous without an explicit next-day label.
    if not deadline and tuple(map(int, times[0])) > (hour, minute):
        return None
    return result


def activity_expired(item, now=None):
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    expiry = int(item.get("source_expiry") or 0)
    if expiry and expiry < now.timestamp():
        return True
    classification = item.get("classification") or {}
    for field in ("deadline", "event_date"):
        day = classification.get(field)
        if day and day < now.date().isoformat():
            return True
        end = evidence_end(day, classification.get(field + "_evidence", ""), deadline=field == "deadline")
        if end is not None and end <= now:
            return True
    return False
