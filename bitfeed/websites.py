"""Read configured public BIT list pages without cookies or guessed pagination.

Dates must belong to an individual row, never an ancestor containing several
articles. Only links actually present on the page can advance pagination.
``complete`` refers to an explicitly observed pagination end, and never claims
that every historical article in the school or on the site has been covered.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests

TZ = timezone(timedelta(hours=8))
USER_AGENT = "bit-student-feed/0.2 (+https://github.com/lankerr/bit-student-feed)"
DATE_RE = re.compile(r"(?<!\d)(20\d{2})[\s年./-]+(\d{1,2})[\s月./-]+(\d{1,2})(?:日)?(?!\d)")
EDGE_DATE_RE = re.compile(r"^(20\d{2}[-./]\d{1,2}[-./]\d{1,2})\s+(.+)$|^(.+?)\s+(20\d{2}[-./]\d{1,2}[-./]\d{1,2})$")
DOC_RE = re.compile(r"\.s?html?$", re.I)
LIST_FILE_RE = re.compile(r"^(?:index|list|default|channel|more)(?:[_-]?\d+)?\.s?html?$", re.I)
NEXT_TEXT_RE = re.compile(r"^(?:下[一]?页|下一頁|下頁|next(?:\s+page)?|[›»>]+)$", re.I)


def _now():
    return datetime.now(TZ)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _official_url(value, base=None):
    parts = urlsplit(urljoin(base or "", str(value or "").strip()))
    host = (parts.hostname or "").lower()
    if (parts.scheme not in ("http", "https") or (host != "bit.edu.cn" and not host.endswith(".bit.edu.cn"))
            or parts.username or parts.password or parts.port not in (None, 80, 443)):
        raise ValueError("public bit.edu.cn HTTP(S) URL required")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _https_upgrade_target(current_url, response):
    """Allow one same-host HTTP→HTTPS upgrade; never follow other redirects."""
    if response.status_code not in (301, 302, 303, 307, 308):
        return None
    location = response.headers.get("Location") if getattr(response, "headers", None) else None
    if not location:
        return None
    try:
        current, target = urlsplit(current_url), urlsplit(_official_url(location, current_url))
    except ValueError:
        return None
    if (current.scheme != "http" or target.scheme != "https"
            or (current.hostname or "").lower() != (target.hostname or "").lower()
            or current.path != target.path or current.query != target.query):
        return None
    return urlunsplit(("https", target.netloc, target.path, target.query, ""))


def _get_list_page(http, url):
    response = http.get(url, headers={"User-Agent": USER_AGENT, "Cookie": ""}, timeout=(10, 25), allow_redirects=False)
    upgrade = _https_upgrade_target(url, response)
    if upgrade:
        response = http.get(upgrade, headers={"User-Agent": USER_AGENT, "Cookie": ""}, timeout=(10, 25), allow_redirects=False)
        url = upgrade
    return response, url


def _is_article_url(value, base):
    try:
        url = _official_url(value, base)
    except ValueError:
        return None
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    if not DOC_RE.search(filename) or LIST_FILE_RE.match(filename):
        return None
    if urlsplit(url).path == urlsplit(base).path:
        return None
    return url


def _load_config(config_path=None):
    if config_path is None:
        candidates = (Path(__file__).parent.parent/"config/official-websites.json", Path.cwd()/"config/official-websites.json")
        config_path = next((p for p in candidates if p.is_file()), candidates[0])
    data = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    sources = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(sources, list) or len(sources) > 100:
        raise ValueError("invalid official website configuration")
    result, identifiers = [], set()
    for source in sources:
        if not isinstance(source, dict) or not all(source.get(k) for k in ("id", "name", "url")):
            raise ValueError("source id/name/url required")
        if source["id"] in identifiers:
            raise ValueError("duplicate official website source ID")
        identifiers.add(source["id"])
        result.append({"id": str(source["id"]), "name": str(source["name"]), "url": _official_url(source["url"])})
    return result


def _dates(text):
    found = {}
    for match in DATE_RE.finditer(text or ""):
        try:
            day = datetime(*(int(v) for v in match.groups()), tzinfo=TZ)
        except ValueError:
            continue
        found.setdefault(day.strftime("%Y-%m-%d"), match.group(0))
    return found


def _single_date(text, current):
    values = _dates(text)
    if len(values) != 1:
        return None
    day, evidence = next(iter(values.items()))
    date = datetime.fromisoformat(day).replace(tzinfo=TZ)
    if date > current + timedelta(days=1):
        return None
    return day, evidence


def _in_navigation(anchor):
    for parent in [anchor, *list(anchor.parents)[:5]]:
        if getattr(parent, "name", "") in ("nav", "header", "footer") or parent.get("role") == "navigation":
            return True
        classes = " ".join(parent.get("class", []))
        if re.search(r"(?:^|[-_\s])(?:nav|menu|footer|header|breadcrumb)(?:$|[-_\s])", classes, re.I):
            return True
    return False


def _row_date(anchor, base, title, current):
    """Find a date in one item without using a multi-item list ancestor."""
    node = anchor
    for _ in range(5):
        if node is None or getattr(node, "name", "") in ("body", "html", "[document]"):
            break
        urls = {_is_article_url(a.get("href"), base) for a in node.find_all("a", href=True)}
        urls.discard(None)
        # The anchor itself is not returned by find_all().
        if node is anchor:
            urls.add(_is_article_url(anchor.get("href"), base))
            urls.discard(None)
        if len(urls) > 1:
            break
        if len(node.get_text(" ", strip=True)) > 1400:
            break
        times = node.find_all("time")
        if node.name == "time":
            times.insert(0, node)
        explicit = []
        for item in times:
            value = _single_date(item.get("datetime", "") or item.get_text(" ", strip=True), current)
            if value:
                explicit.append(value)
        if explicit and len({x[0] for x in explicit}) == 1:
            return explicit[0]
        dated = node.find_all(class_=lambda value: bool(value and re.search(r"date|time|publish|\brq\b", str(value), re.I)))
        if node.get("class") and any(re.search(r"date|time|publish|\brq\b", value, re.I) for value in node.get("class", [])):
            dated.insert(0, node)
        value = _single_date(" ".join(item.get_text(" ", strip=True) for item in dated), current)
        if value:
            return value
        # A headline can itself contain an event date. Exclude all of its text
        # before considering plain neighbouring date text.
        text = node.get_text(" ", strip=True)
        anchor_text = anchor.get_text(" ", strip=True)
        if node is not anchor and anchor_text:
            text = text.replace(anchor_text, "", 1)
            value = _single_date(text, current)
            if value:
                return value
        # Some list anchors contain a title attribute and a separate date span.
        if anchor.get("title") and node is anchor:
            value = _single_date(anchor_text.replace(anchor["title"], "", 1), current)
            if value:
                return value
        if node.name in ("li", "tr", "article"):
            # Even a list with only one article can have an unrelated date in
            # its heading. Once the item boundary is reached, stop searching.
            return None
        node = node.parent
    return None


def _title(anchor):
    title = _text(anchor.get("title") or anchor.get_text(" ", strip=True))
    # Remove explicit list date sub-elements while retaining dates in the title.
    if not anchor.get("title"):
        for part in anchor.find_all(["time", "span"], class_=lambda value: bool(value and re.search(r"date|time|publish", str(value), re.I))):
            label = _text(part.get_text(" ", strip=True))
            if label and _dates(label):
                title = _text(title.replace(label, "", 1))
    return title[:500]


def _next_link(soup, current_url, visited):
    for anchor in soup.find_all("a", href=True):
        label = _text(anchor.get_text(" ", strip=True))
        is_next = "next" in (anchor.get("rel") or []) or any(NEXT_TEXT_RE.fullmatch(_text(value)) for value in
                    (label, anchor.get("title"), anchor.get("aria-label")))
        if not is_next:
            continue
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        try:
            target = _official_url(href, current_url)
        except ValueError:
            continue
        parts, current = urlsplit(target), urlsplit(current_url)
        if parts.hostname != current.hostname or parts.path.rsplit("/", 1)[0] != current.path.rsplit("/", 1)[0]:
            continue
        filename = parts.path.rsplit("/", 1)[-1]
        if not LIST_FILE_RE.match(filename) and parts.path != current.path:
            continue
        return target, target in visited
    return None, False


def _confirmed_last_page(soup):
    # End-of-pagination evidence must be explicit. Absence of a next link alone
    # says nothing about whether a website exposes its complete archive.
    for node in soup.find_all(["div", "span", "nav", "p"], class_=lambda value: bool(value and re.search(r"page|pagination|fenye", str(value), re.I))):
        text = node.get_text(" ", strip=True)
        patterns = (r"第\s*(\d+)\s*页\s*[/／]\s*共?\s*(\d+)\s*页", r"第\s*(\d+)\s*页.*?共\s*(\d+)\s*页")
        for pattern in patterns:
            match = re.search(pattern, text)
            if match and int(match[1]) == int(match[2]):
                return True
    return False


def _parse_page(content, source, url, current):
    soup = BeautifulSoup(content, "html.parser")
    articles, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        if _in_navigation(anchor):
            continue
        link = _is_article_url(anchor["href"], url)
        if not link or link in seen:
            continue
        title = _title(anchor)
        if len(title) < 6 or NEXT_TEXT_RE.fullmatch(title):
            continue
        seen.add(link)
        when = _row_date(anchor, url, title, current)
        if not when and not anchor.get("title"):
            # Lists that print the date inside the link without a date class put a bare
            # numeric date at one edge; a headline date ("12月1日讲座") is an event date.
            edge = EDGE_DATE_RE.match(title)
            if edge:
                stamp, rest = (edge[1], edge[2]) if edge[1] else (edge[4], edge[3])
                when = _single_date(stamp, current)
                if when and len(rest.strip()) >= 6:
                    title = rest.strip()
                else:
                    when = None
        day, evidence = when if when else (None, "")
        published = int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp()) if day else int(current.timestamp())
        body = title + (("\n官网列表发布日期（非活动日期）：" + evidence) if evidence else "\n官网列表未提供可核验的完整发布日期。")
        articles.append({"source": source["name"], "source_id": source["id"], "title": title, "url": link,
                         "published_at": published, "publication_status": "known" if day else "unknown",
                         "first_seen": int(current.timestamp()), "origin": "official_server", "kind": "official",
                         "body": body, "body_status": "title_and_publication_date", "digest": "",
                         "source_date_evidence": evidence, "source_publication_date": day})
    return soup, articles


def collect(config_path=None, session=None, max_pages=3):
    """Read observed public list pages; return per-source errors and coverage.

    The result is suitable for incremental upsert. A failed source retains its
    already collected pages and does not prevent other sources from running.
    ``history_complete`` is always false: this reader does not establish that a
    site's visible pagination contains every article it has ever published.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("max_pages must be an integer in 1..100")
    sources = _load_config(config_path)
    current = _now()
    checked = current.isoformat(timespec="seconds")
    http = session or requests.Session()
    all_articles, audits = {}, []
    try:
        for source in sources:
            url, visited = source["url"], set()
            audit = {"id": source["id"], "name": source["name"], "source": source["name"], "url": url,
                     "origin": "official_server", "state": "ok", "pages": 0, "fetched": 0,
                     "dated": 0, "unknown_dates": 0, "exhausted": False, "truncated": False,
                     "complete": False, "history_complete": False, "scope": "observed_list_pages", "checked_at": checked}
            source_links = set()
            for _ in range(max_pages):
                try:
                    if url in visited:
                        raise ValueError("pagination cycle")
                    response, resolved = _get_list_page(http, url)
                    visited.add(url)
                    visited.add(resolved)
                    url = resolved
                    # Some BIT list pages keep a "next" link past the last page.
                    # A 404 after successful pages means pagination ended, not a source outage.
                    if response.status_code == 404 and audit["pages"]:
                        audit["exhausted"] = True
                        audit["pagination_status"] = "next_not_found"
                        audit.pop("next_url", None)
                        audit["url"] = audit.get("url") or source["url"]
                        break
                    audit["url"] = url
                    if response.status_code != 200:
                        raise RuntimeError("HTTP " + str(response.status_code))
                    if len(response.content) > 5_000_000:
                        raise ValueError("list page exceeds size limit")
                    soup, articles = _parse_page(response.content, source, url, current)
                    audit["pages"] += 1
                    for article in articles:
                        if article["url"] not in source_links:
                            source_links.add(article["url"])
                            audit["fetched"] += 1
                            audit["dated" if article["publication_status"] == "known" else "unknown_dates"] += 1
                        existing = all_articles.get(article["url"])
                        if existing is None or (existing["publication_status"] == "unknown" and article["publication_status"] == "known"):
                            all_articles[article["url"]] = article
                    next_url, cycle = _next_link(soup, url, visited)
                    if cycle:
                        raise ValueError("pagination cycle")
                    if next_url is None:
                        audit["exhausted"] = True
                        audit["pagination_status"] = "no_next_link"
                        audit["complete"] = bool(articles) and _confirmed_last_page(soup)
                        break
                    url = next_url
                    audit["next_url"] = url
                    audit["pagination_status"] = "next_link"
                except (requests.RequestException, ValueError, TypeError, AttributeError, OverflowError) as exc:
                    audit["state"] = "partial" if audit["fetched"] else "failed"
                    audit["error"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                    audit["pagination_status"] = "request_or_parse_failed"
                    audit["truncated"] = True
                    break
                except RuntimeError as exc:
                    audit["state"] = "partial" if audit["fetched"] else "failed"
                    audit["error"] = str(exc)
                    audit["pagination_status"] = "request_failed"
                    audit["truncated"] = True
                    break
            if not audit["exhausted"] and "error" not in audit:
                audit["state"] = "partial"
                audit["truncated"] = True
                audit["pagination_status"] = "cap_reached"
            if audit["pages"] and not audit["fetched"] and "error" not in audit:
                audit["state"] = "empty"
                audit["complete"] = False
            audits.append(audit)
    finally:
        if session is None:
            http.close()
    return {"origin": "official_server", "collected_at": checked, "articles": list(all_articles.values()), "sources": audits}
