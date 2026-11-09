"""Collect explicitly configured, public WeChat article albums without login.

The public query/cursor shape is documented by the upstream implementation:
https://github.com/wechat-article/wechat-article-exporter/blob/master/server/api/web/misc/appmsgalbum.get.ts
An exhausted album is only an album: it is not the account's whole history.
"""
import hashlib
import html
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from .domain import article_id, normalize_url, now_iso
from datetime import datetime, timezone


ENDPOINT = "https://mp.weixin.qq.com/mp/appmsgalbum"
ORIGIN = "server_public_album"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "public-albums.json"
USER_AGENT = "bit-student-feed/0.2 (+https://github.com/lankerr/bit-student-feed)"
BLOCKED_TEXT = re.compile(r"当前环境异常|完成验证|验证码|访问过于频繁|antispider|captcha", re.I)


class AlbumError(ValueError):
    def __init__(self, code, blocked=False):
        super().__init__(code)
        self.blocked = blocked


def _number(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,24}", str(value)):
        raise AlbumError("invalid_number")
    return int(value)


def _text(value, limit=500):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _album_url(raw):
    parts = urlsplit(html.unescape(str(raw or "")))
    if (parts.scheme not in {"http", "https"} or parts.hostname != "mp.weixin.qq.com"
            or parts.path != "/mp/appmsgalbum" or parts.username or parts.password
            or parts.port not in (None, 80, 443)):
        raise AlbumError("invalid_public_album_url")
    query = parse_qs(parts.query, keep_blank_values=True)
    # Never accept an input carrying login credentials, even though unknown
    # tracking parameters would otherwise be discarded during canonicalization.
    if any(any(value for value in query.get(key, [])) for key in
           ("cookie", "token", "appmsg_token", "key", "uin", "pass_ticket", "wxtoken")):
        raise AlbumError("credentials_not_allowed")
    biz = query.get("__biz", query.get("fakeid", [""]))
    album = query.get("album_id", [""])
    if len(biz) != 1 or len(album) != 1 or not re.fullmatch(r"[A-Za-z0-9_+\-/=]{1,150}", biz[0]):
        raise AlbumError("invalid_album_identity")
    if "__biz" in query and "fakeid" in query and query["__biz"] != query["fakeid"]:
        raise AlbumError("conflicting_account_identity")
    if _number(album[0]) <= 0:
        raise AlbumError("invalid_album_identity")
    reverse = query.get("is_reverse", ["0"])
    if len(reverse) != 1 or reverse[0] not in {"0", "1"}:
        raise AlbumError("invalid_album_order")
    canonical = ENDPOINT + "?" + urlencode({"__biz": biz[0], "action": "getalbum", "album_id": album[0]})
    return biz[0], album[0], reverse[0], canonical


def _row_identity(row):
    if not isinstance(row, dict):
        raise AlbumError("invalid_article_row")
    msgid, itemidx = str(row.get("msgid", "")), str(row.get("itemidx", ""))
    if _number(msgid) <= 0:
        raise AlbumError("invalid_article_cursor")
    _number(itemidx)
    return msgid, itemidx


def _article(row, name, album_id):
    title = _text(row.get("title"))
    if not title:
        raise AlbumError("missing_article_title")
    raw_url = html.unescape(str(row.get("url") or ""))
    parts = urlsplit(raw_url)
    if parts.hostname != "mp.weixin.qq.com" or not (parts.path == "/s" or parts.path.startswith("/s/")):
        raise AlbumError("invalid_public_article_url")
    url = normalize_url(raw_url)
    published_at = _number(row.get("create_time"))
    if published_at < 946684800 or published_at > int(datetime.now(timezone.utc).timestamp()) + 86400:
        raise AlbumError("invalid_publication_time")
    return {"id": article_id(url), "title": title, "url": url, "source": name,
            "source_id": "album:" + album_id, "published_at": published_at,
            "publication_status": "known", "kind": "wechat", "origin": ORIGIN,
            "body": "", "digest": "", "body_status": "not_fetched"}


def _page(response):
    if response.status_code != 200:
        raise AlbumError("http_" + str(response.status_code),
                         blocked=response.status_code in (301, 302, 303, 307, 308, 401, 403, 429))
    text = str(getattr(response, "text", ""))
    if len(getattr(response, "content", b"")) > 3000000:
        raise AlbumError("response_too_large")
    try:
        data = response.json()
    except ValueError:
        if BLOCKED_TEXT.search(text[:20000]):
            raise AlbumError("verification_page", blocked=True) from None
        raise AlbumError("unexpected_non_json") from None
    if not isinstance(data, dict) or not isinstance(data.get("base_resp"), dict):
        raise AlbumError("invalid_public_response")
    ret = data["base_resp"].get("ret")
    if isinstance(ret, bool) or ret not in (0, "0"):
        blocked = (ret in (200003, 200013, "200003", "200013") or
                   bool(BLOCKED_TEXT.search(str(data["base_resp"].get("err_msg", "")))))
        raise AlbumError("public_api_error", blocked=blocked)
    page = data.get("getalbum_resp")
    if not isinstance(page, dict) or not isinstance(page.get("base_info"), dict):
        raise AlbumError("invalid_album_response")
    rows = page.get("article_list")
    if isinstance(rows, dict):
        rows = [rows]
    if rows is None and str(page["base_info"].get("article_count")) == "0":
        rows = []
    if not isinstance(rows, list):
        raise AlbumError("invalid_article_list")
    continued = page.get("continue_flag")
    if isinstance(continued, bool) or continued not in (0, 1, "0", "1"):
        raise AlbumError("invalid_continue_flag")
    count = page["base_info"].get("article_count")
    return page, rows, str(continued) == "1", _number(count) if count is not None else None


def collect(config_path=None, session=None, max_pages=100):
    """Return public article metadata and a separate audit for every album.

    Config: ``{"albums":[{"id":"...","name":"...","url":"..."}]}``.
    No browser credentials, tokens, automatic redirects or proxies are used.
    ``complete`` is scoped to the configured album and additionally requires a
    stable, matching article count, a terminal page, and no invalid rows.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 1000:
        raise ValueError("max_pages must be an integer in 1..1000")
    checked = now_iso()
    result = {"origin": ORIGIN, "collected_at": checked, "articles": [], "sources": [],
              "state": "unconfigured", "scope": "album_only"}
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG
    if not path.exists():
        return result
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        specs = config.get("albums") if isinstance(config, dict) else None
        if not isinstance(specs, list):
            raise ValueError()
    except (OSError, ValueError):
        result.update(state="failed", error="invalid_album_configuration")
        return result
    if not specs:
        return result
    # Construct a session only when a source can actually be configured.
    http = session
    all_articles, configured = {}, set()
    try:
        for index, spec in enumerate(specs):
            name = _text(spec.get("name"), 120) if isinstance(spec, dict) else ""
            name = name or "公开公众号合集"
            identifier = _text(spec.get("id"), 120) if isinstance(spec, dict) else ""
            audit = {"id": identifier or "public-album-" + str(index + 1), "name": name,
                     "source": name, "origin": ORIGIN, "scope": "album_only", "checked_at": checked,
                     "state": "ok", "count": None, "pages": 0, "fetched": 0, "accepted": 0,
                     "invalid": 0, "exhausted": False, "complete": False, "truncated": False,
                     "count_changed": False, "account_complete": False}
            result["sources"].append(audit)
            try:
                if not isinstance(spec, dict):
                    raise AlbumError("invalid_source_configuration")
                if spec.get("enabled") is False:
                    audit["state"] = "disabled"
                    continue
                if not spec.get("url"):
                    audit["state"] = "unconfigured"
                    continue
                biz, album, reverse, canonical = _album_url(spec["url"])
                audit["url"] = canonical
                if (biz, album) in configured:
                    raise AlbumError("duplicate_album_configuration")
                configured.add((biz, album))
                if http is None:
                    http = requests.Session()
                    http.trust_env = False
                params = {"action": "getalbum", "__biz": biz, "album_id": album,
                          "count": 20, "is_reverse": reverse, "f": "json"}
                identities, page_fingerprints, cursors = set(), set(), set()
                for _ in range(max_pages):
                    response = http.get(ENDPOINT, params=dict(params),
                        headers={"User-Agent": USER_AGENT, "Cookie": "", "Authorization": ""},
                        auth=(), proxies={"http": "", "https": "", "all": ""},
                        verify=True, timeout=(10, 30), allow_redirects=False)
                    page, rows, continued, count = _page(response)
                    audit["pages"] += 1
                    if audit["count"] is None:
                        audit["count"] = count
                    elif count is not None and count != audit["count"]:
                        audit["count_changed"] = True
                    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    if rows and fingerprint in page_fingerprints:
                        raise AlbumError("repeated_page")
                    page_fingerprints.add(fingerprint)
                    previous = len(identities)
                    for row in rows:
                        try:
                            identity = _row_identity(row)
                            if identity in identities:
                                continue
                            identities.add(identity)
                            article = _article(row, name, album)
                            all_articles.setdefault(article["id"], article)
                            audit["accepted"] += 1
                        except (ValueError, TypeError, OverflowError):
                            audit["invalid"] += 1
                    audit["fetched"] = len(identities)
                    if not continued:
                        audit["exhausted"] = True
                        break
                    if not rows or len(identities) == previous:
                        raise AlbumError("pagination_without_progress")
                    cursor = _row_identity(rows[-1])
                    if cursor in cursors:
                        raise AlbumError("repeated_cursor")
                    cursors.add(cursor)
                    params.update(begin_msgid=cursor[0], begin_itemidx=cursor[1])
                audit["truncated"] = not audit["exhausted"]
                audit["count_matches"] = audit["count"] is not None and audit["count"] == audit["fetched"]
                audit["complete"] = (audit["exhausted"] and audit["count_matches"] and
                                     not audit["count_changed"] and not audit["invalid"])
                if not audit["complete"]:
                    audit["state"] = "partial"
            except AlbumError as exc:
                audit.update(state="blocked" if exc.blocked else "partial" if audit["accepted"] else "failed",
                             error=str(exc), complete=False, truncated=not audit["exhausted"])
            except requests.RequestException:
                # Exception text can embed URL/query values, so do not expose it.
                audit.update(state="partial" if audit["accepted"] else "failed", error="request_failed",
                             complete=False, truncated=not audit["exhausted"])
            except (ValueError, TypeError, KeyError):
                audit.update(state="partial" if audit["accepted"] else "failed", error="invalid_source_data",
                             complete=False, truncated=not audit["exhausted"])
    finally:
        if session is None and http is not None:
            http.close()
    result["articles"] = list(all_articles.values())
    states = {source["state"] for source in result["sources"]}
    result["state"] = ("unconfigured" if states <= {"unconfigured", "disabled"} else
                       "ok" if states <= {"ok", "disabled"} else "partial" if all_articles else
                       "blocked" if "blocked" in states else "failed")
    return result
