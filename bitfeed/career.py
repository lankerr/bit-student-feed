"""Read the BIT careers site's public, paginated AJAX feeds without login.

The routes and field paths below come from the site's shipped list pages and
were checked with unauthenticated requests. Event dates are never presented as
publication dates. A partial run returns its collected rows and a per-source
audit record; consumers should upsert these rows, never replace a source with
an empty result after an error.
"""
from datetime import datetime, timedelta, timezone
import math
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

BASE = "https://job.bit.edu.cn"
TZ = timezone(timedelta(hours=8))
USER_AGENT = "bit-student-feed/0.2 (+https://github.com/lankerr/bit-student-feed)"

# These are public data-reading endpoints, despite their use of HTTP POST.
SOURCES = (
    {"id": "career-jobs", "name": "北理就业官网·招聘职位", "type": "jobs",
     "path": "/f/recruitmentinfo/ajax_frontRecruitinfo", "params": {"positionType": 1}, "container": "object"},
    {"id": "career-internships", "name": "北理就业官网·实习信息", "type": "jobs",
     "path": "/f/recruitmentinfo/ajax_frontRecruitinfo", "params": {"positionType": 2}, "container": "object"},
    {"id": "career-talks", "name": "北理就业官网·宣讲会", "type": "event",
     "path": "/f/recruitmentFair/ajax_frontRecruitfair", "params": {}, "container": "object"},
    {"id": "career-fairs", "name": "北理就业官网·双选会", "type": "event",
     "path": "/f/bilateralchosefair/ajax_frontBilateralchosefair", "params": {}, "container": "object"},
    {"id": "career-guidance", "name": "北理就业官网·指导活动", "type": "event",
     "path": "/f/common/activity/ajax_activity", "params": {"searchType": "1"}, "container": "data"},
    {"id": "career-notices", "name": "北理就业官网·通知公告", "type": "news",
     "path": "/f/newsCenter/ajax_list", "params": {"categoryId": "3fcc824cecbc42aea3dce3700e5a4839"}, "container": "object.newsPage"},
    {"id": "career-news", "name": "北理就业官网·新闻资讯", "type": "news",
     "path": "/f/newsCenter/ajax_list", "params": {"categoryId": "6ebab28e72ba46da99a0f2c372b129d7"}, "container": "object.newsPage"},
    {"id": "career-advice", "name": "北理就业官网·求职秘籍", "type": "news",
     "path": "/f/newsCenter/ajax_list", "params": {"categoryId": "13"}, "container": "object.newsPage"},
)

# Individual lost-document announcements can contain student IDs. They do not
# belong in a general activity/recruitment digest or model input.
PERSONAL_NOTICE = re.compile(r"(?:丢失|遗失).*(?:三方协议|就业推荐表|报到证|就业协议)|(?:三方协议|就业推荐表|报到证|就业协议).*(?:丢失|遗失)")


def _now():
    return datetime.now(TZ)


def _text(value, limit=1000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid pagination number")
    number = int(value)
    if number < 0:
        raise ValueError("negative pagination number")
    return number


def _published(value, current):
    if not value:
        return int(current.timestamp()), "unknown"
    try:
        parsed = datetime.fromisoformat(str(value).strip()).replace(tzinfo=TZ)
    except (TypeError, ValueError):
        return int(current.timestamp()), "unknown"
    # releaseDate is the only explicit publication field used by this reader.
    if parsed > current + timedelta(days=1) or parsed.year < 2000:
        return int(current.timestamp()), "unknown"
    return int(parsed.timestamp()), "known"


def _public_url(value):
    parts = urlsplit(urljoin(BASE + "/", str(value or "")))
    if (parts.hostname != "job.bit.edu.cn" or parts.scheme not in ("http", "https")
            or parts.username or parts.password or parts.port not in (None, 80, 443)
            or not parts.path.startswith(("/f/", "/frontpage/bit/html/"))):
        raise ValueError("non-public careers URL")
    return urlunsplit(("https", "job.bit.edu.cn", parts.path, parts.query, ""))


def _article(row, spec, current):
    title = _text(row.get("title") or row.get("name"), 500)
    if not title or not row.get("url"):
        raise ValueError("missing title/URL")
    if PERSONAL_NOTICE.search(title):
        return None
    url = _public_url(row["url"])
    # A job post goes live at startTime, so that is its publication date. Talks and
    # fairs only carry event times, which must not pose as publication dates.
    published_field = {"news": row.get("releaseDate"), "jobs": row.get("startTime")}.get(spec["type"])
    published_at, publication_status = _published(published_field, current)
    lines = [title]
    event_start = event_end = ""
    if spec["type"] == "event":
        event_start, event_end = _text(row.get("startTime"), 40), _text(row.get("endTime"), 40)
        if event_start:
            lines.append("活动开始（官网字段）：" + event_start)
        if event_end:
            lines.append("活动结束（官网字段）：" + event_end)
        place = row.get("realPlace") or row.get("place") or row.get("address") or row.get("fieldExport")
        if not place and isinstance(row.get("field"), dict):
            place = row["field"].get("name")
        if place:
            lines.append("活动地点（官网字段）：" + _text(place, 300))
        if row.get("holdStatus"):
            lines.append("官网显示状态（须结合日期核对）：" + _text(row["holdStatus"], 80))
        if row.get("activityTypeValue"):
            lines.append("活动类型：" + _text(row["activityTypeValue"], 100))
    elif spec["type"] == "jobs":
        for field, label in (("startTime", "招聘有效开始（官网字段）"), ("endTime", "招聘有效截止（官网字段）"),
                             ("education", "学历要求"), ("majorName", "专业要求"), ("corporationName", "用人单位")):
            if row.get(field):
                lines.append(label + "：" + _text(row[field], 1000))
    elif row.get("description"):
        # The list contains an excerpt, not a verified full article body.
        lines.append("官网列表摘要：" + _text(row["description"], 1600))
    result = {"source": spec["name"], "source_id": spec["id"], "source_article_id": _text(row.get("id"), 150),
              "title": title, "url": url, "published_at": published_at,
              "publication_status": publication_status, "first_seen": int(current.timestamp()),
              "kind": "official", "origin": "official_server", "body": "\n".join(lines),
              "body_status": "structured_list_metadata", "digest": _text(row.get("description"), 1000)}
    if event_start:
        result["source_event_start"] = event_start
    if event_end:
        result["source_event_end"] = event_end
    if row.get("updateDate"):
        result["source_updated_at"] = _text(row["updateDate"], 40)
    if spec["type"] == "jobs":
        if row.get("startTime"):
            result["source_available_from"] = _text(row["startTime"], 40)
        if row.get("endTime"):
            result["source_application_deadline"] = _text(row["endTime"], 40)
    return result


def _container(data, path):
    for key in path.split("."):
        data = data[key]
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise ValueError("invalid public list shape")
    return data


def collect(max_pages=100, page_size=20, session=None):
    """Return articles and honest coverage for all eight verified public lists.

    ``max_pages`` is a per-source cap. ``exhausted`` means the API's pagination
    boundary was reached; ``complete`` additionally requires a matching unique
    row count and no parsing/request errors. A capped or failed source never
    claims completeness. Publication timestamps absent in the source remain
    explicitly unknown; ``published_at`` then carries the first-seen timestamp
    solely for compatibility with the existing article store.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be an integer in 1..1000")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise ValueError("page_size must be an integer in 1..100")
    current = _now()
    checked = current.isoformat(timespec="seconds")
    http = session or requests.Session()
    all_articles, audits = {}, []
    try:
        for spec in SOURCES:
            audit = {"id": spec["id"], "name": spec["name"], "source": spec["name"], "origin": "official_server",
                     "endpoint": BASE + spec["path"], "state": "ok", "count": None, "pages": 0, "fetched": 0,
                     "accepted": 0, "excluded": 0, "invalid": 0, "exhausted": False, "truncated": False,
                     "complete": False, "checked_at": checked, "count_changed": False}
            identities, page_fingerprints = set(), set()
            for requested_page in range(1, max_pages + 1):
                try:
                    response = http.post(BASE + spec["path"], data={**spec["params"], "pageNo": requested_page, "pageSize": page_size},
                        headers={"User-Agent": USER_AGENT, "Cookie": ""}, timeout=(10, 30), allow_redirects=False)
                    if response.status_code != 200:
                        raise RuntimeError("HTTP " + str(response.status_code))
                    data = response.json()
                    if not isinstance(data, dict) or data.get("state") != 1:
                        raise ValueError("public API did not report success")
                    page = _container(data, spec["container"])
                    if page.get("pageNo") is not None and _number(page["pageNo"]) != requested_page:
                        raise ValueError("unexpected page number")
                    count = _number(page.get("count"))
                    if audit["count"] is None:
                        audit["count"] = count
                    elif count is not None and count != audit["count"]:
                        audit["count_changed"] = True
                    audit["pages"] += 1
                    rows = page["list"]
                    fingerprint = tuple(str(row.get("id") or row.get("url") or "") for row in rows if isinstance(row, dict))
                    if fingerprint and fingerprint in page_fingerprints:
                        raise ValueError("pagination repeated an earlier page")
                    page_fingerprints.add(fingerprint)
                    for index, row in enumerate(rows):
                        identity = str(row.get("id") or row.get("url") or f"invalid:{requested_page}:{index}") if isinstance(row, dict) else f"invalid:{requested_page}:{index}"
                        if identity in identities:
                            continue
                        identities.add(identity)
                        try:
                            if not isinstance(row, dict):
                                raise ValueError("invalid list row")
                            article = _article(row, spec, current)
                            if article is None:
                                audit["excluded"] += 1
                            else:
                                # URL identity deduplicates articles repeated across public categories.
                                all_articles.setdefault(article["url"], article)
                                audit["accepted"] += 1
                        except (ValueError, TypeError, OverflowError):
                            audit["invalid"] += 1
                    audit["fetched"] = len(identities)
                    total_pages = _number(page.get("totalPage"))
                    actual_page_size = _number(page.get("pageSize")) or page_size
                    if total_pages is None and count is not None:
                        total_pages = math.ceil(count / actual_page_size)
                    if page.get("lastPage") is True or (total_pages is not None and requested_page >= total_pages) or (not rows):
                        audit["exhausted"] = True
                        break
                    if count is None and len(rows) < actual_page_size:
                        audit["exhausted"] = True
                        break
                except (requests.RequestException, ValueError, TypeError, KeyError, RuntimeError) as exc:
                    # Only our own generic error text is exposed; server bodies may include personal fields.
                    audit["state"] = "partial" if audit["fetched"] else "failed"
                    audit["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                    break
            audit["truncated"] = not audit["exhausted"]
            audit["count_matches"] = audit["count"] is None or audit["count"] == audit["fetched"]
            audit["complete"] = audit["exhausted"] and audit["count_matches"] and not audit["count_changed"] and not audit["invalid"] and "error" not in audit
            if not audit["complete"] and audit["state"] == "ok":
                audit["state"] = "partial"
            audits.append(audit)
    finally:
        if session is None:
            http.close()
    return {"origin": "official_server", "collected_at": checked, "articles": list(all_articles.values()), "sources": audits}
