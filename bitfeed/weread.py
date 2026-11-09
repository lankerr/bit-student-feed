"""WeRead web client for WeChat official-account articles (``MP_WXS_*`` books).

Since 2026-07-30 the public-platform cross-account list (``appmsgpublish``)
answers every third-party request with ``200013``; WeRead web is the remaining
open route. Endpoints follow rachelos/we-mp-rss (commit 9fd4edb):

* ``GET  /web/shelf/sync?userVid=&synckey=0``   session check and shelf
* ``POST /web/shelf/add``                        follow an account
* ``GET  /web/mp/articles?bookId=&offset=``      pushes, newest first
* ``GET  /api/mp/cover?bookId=``                 latest article only (fallback)
* ``POST /web/login/renewal``                    refresh ``wr_skey`` with ``wr_rt``

Cookies are sent as one explicit header so a refreshed value can never be
shadowed by a stale duplicate in a cookie jar.
"""
import base64
import binascii
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote

import requests

WEREAD = "https://weread.qq.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
AUTH_CODES = {-2010, -2012, -2013}
QUOTA_CODES = {-2014, -2041}


class WeReadError(RuntimeError):
    """``state`` is one of auth_missing, auth_expired, rate_limited, failed, not_found."""
    def __init__(self, code, state="failed"):
        super().__init__(str(code))
        self.code, self.state = str(code), state


def private_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def browser_headers():
    return {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9", "Origin": WEREAD, "Referer": WEREAD + "/"}


def book_id_from_fakeid(fakeid):
    """``__biz`` is base64 of the account's numeric id; WeRead uses ``MP_WXS_<id>``."""
    try:
        digits = base64.b64decode(str(fakeid), validate=True).decode("ascii")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("fakeid is not base64") from None
    if not re.fullmatch(r"\d{5,20}", digits):
        raise ValueError("fakeid does not encode a numeric account id")
    return "MP_WXS_" + digits


def fakeid_from_book_id(book_id):
    match = re.fullmatch(r"MP_WXS_(\d{5,20})", str(book_id))
    if not match:
        raise ValueError("not an MP_WXS book id")
    return base64.b64encode(match.group(1).encode()).decode()


def article_url(original_id="", review_id="", book_id=""):
    """Permanent ``mp.weixin.qq.com/s/<token>`` link; WeRead writes ``_`` as ``~``."""
    token = str(original_id or "").strip()
    if not token and review_id:
        token = str(review_id)
        if book_id and token.startswith(book_id + "_"):
            token = token[len(book_id) + 1:]
        elif "_" in token:
            token = token.split("_")[-1]
    token = token.replace("~", "_")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,80}", token):
        raise ValueError("article token unverified")
    return "https://mp.weixin.qq.com/s/" + quote(token, safe="")


def _code(payload):
    if not isinstance(payload, dict):
        return 0
    value = payload.get("errCode", payload.get("errcode", 0))
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return -1


class WeReadSession:
    def __init__(self, path, cookies, vid="", meta=None, http=None):
        self.path = Path(path)
        self.cookies = {str(k): str(v) for k, v in cookies.items()
                        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}", str(k))
                        and v is not None and not re.search(r"[\r\n;]", str(v))}
        self.vid = str(vid or self.cookies.get("wr_vid", ""))
        if self.vid and not self.cookies.get("wr_vid"):
            self.cookies["wr_vid"] = self.vid
        self.meta = dict(meta or {})
        self._http = http or requests.Session()
        self._http.trust_env = False

    @classmethod
    def load(cls, path, http=None):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            cookies = data["cookies"]
            if not isinstance(cookies, dict) or not cookies.get("wr_vid") or not (
                    cookies.get("wr_skey") or cookies.get("wr_rt")):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise WeReadError("session_unavailable", "auth_missing") from None
        meta = {k: v for k, v in data.items() if k not in ("cookies", "vid")}
        return cls(path, cookies, data.get("vid", ""), meta, http)

    @classmethod
    def from_cookies(cls, path, cookies, vid="", http=None):
        return cls(path, cookies, vid, {}, http)

    def save(self, **extra):
        self.meta.update(extra)
        self.meta["saved_at"] = int(time.time())
        private_json(self.path, {"cookies": self.cookies, "vid": self.vid, **self.meta})

    def _headers(self, content_type=False):
        headers = browser_headers()
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if content_type:
            headers["Content-Type"] = "application/json;charset=UTF-8"
        return headers

    def _absorb(self, response):
        changed = False
        for cookie in response.cookies:
            if cookie.value and self.cookies.get(cookie.name) != cookie.value:
                self.cookies[cookie.name] = cookie.value
                changed = True
        return changed

    def _request(self, method, path, params=None, body=None, raw=False):
        try:
            response = self._http.request(method, WEREAD + path, params=params,
                data=None if body is None else json.dumps(body, separators=(",", ":")),
                headers=self._headers(content_type=body is not None),
                timeout=(10, 30), allow_redirects=False)
        except requests.RequestException as exc:
            raise WeReadError(type(exc).__name__) from None
        if self._absorb(response):
            self.save()
        if response.status_code == 499:
            raise WeReadError("http_499", "rate_limited")
        if response.status_code in (401, 403):
            raise WeReadError("http_" + str(response.status_code), "auth_expired")
        if response.status_code != 200:
            raise WeReadError("http_" + str(response.status_code))
        if raw:
            return response.text
        if len(response.content) > 8_000_000:
            raise WeReadError("response_too_large")
        try:
            payload = response.json()
        except ValueError:
            raise WeReadError("non_json", "blocked") from None
        code = _code(payload)
        if code in AUTH_CODES:
            raise WeReadError(code, "auth_expired")
        if code in QUOTA_CODES:
            raise WeReadError(code, "rate_limited")
        if code:
            raise WeReadError(code)
        return payload

    def renew(self, failed_path="/web/shelf/sync"):
        """Mirror the web client: after -2012/-2010 post the failed path to renewal.

        WeRead answers ``succ=1`` with a new ``wr_skey`` cookie, or ``-12013`` when
        the key is still accepted; both mean the original request may be replayed.
        Calling renewal while the key is valid returns ``-2013``.
        """
        before = self.cookies.get("wr_skey")
        try:
            payload = self._request("POST", "/web/login/renewal",
                body={"rq": quote(failed_path, safe=""), "ql": self.cookies.get("wr_ql") == "1"})
            accepted = payload.get("succ") == 1
        except WeReadError as exc:
            accepted = exc.code == "-12013"
        if accepted or self.cookies.get("wr_skey") != before:
            self.save(renewed_at=int(time.time()))
            return True
        return False

    def call(self, method, path, params=None, body=None, raw=False):
        """One API call; an expired key triggers exactly one renewal and replay."""
        try:
            return self._request(method, path, params, body, raw)
        except WeReadError as exc:
            if exc.state != "auth_expired" or exc.code not in ("-2012", "-2010"):
                raise
            if not self.renew(path):
                raise
            try:
                return self._request(method, path, params, body, raw)
            except WeReadError as again:
                # WeRead answers an exhausted quota with the same code as a dead
                # session; a key that was just renewed and is still refused is the
                # quota case. The scheduler escalates if it persists.
                if again.state == "auth_expired":
                    raise WeReadError(again.code, "rate_limited") from None
                raise

    def verify(self, renew_on_failure=False):
        try:
            payload = self._request("GET", "/web/shelf/sync", params={"userVid": "", "synckey": 0})
        except WeReadError as exc:
            if not renew_on_failure or exc.state != "auth_expired":
                raise
            self.renew()
            payload = self._request("GET", "/web/shelf/sync", params={"userVid": "", "synckey": 0})
        if not any(key in payload for key in ("books", "bookCount", "synckey")):
            raise WeReadError("unexpected_shelf", "auth_expired")
        return payload

    def shelf_books(self):
        payload = self.call("GET", "/web/shelf/sync", params={"userVid": "", "synckey": 0})
        books = payload.get("books") if isinstance(payload.get("books"), list) else []
        return {str(b.get("bookId")) for b in books if isinstance(b, dict) and b.get("bookId")}

    def follow(self, book_id):
        return self.call("POST", "/web/shelf/add", body={"bookIds": [book_id]})

    def articles(self, book_id, offset=0):
        """Full push list; WeRead retired it (constant -2041) but it is probed rarely."""
        try:
            payload = self.call("GET", "/web/mp/articles", params={"bookId": book_id, "offset": int(offset)})
        except WeReadError as exc:
            if exc.code == "-2041":
                raise WeReadError(exc.code, "unsupported") from None
            raise
        return parse_articles(payload, book_id)

    def cover(self, book_id):
        payload = self.call("GET", "/api/mp/cover", params={"bookId": book_id})
        if not payload.get("reviewId"):
            raise WeReadError("empty_cover", "not_found")
        return payload

    def content(self, review_id):
        return self.call("GET", "/web/mp/content", params={"reviewId": review_id}, raw=True)

    def close(self):
        self._http.close()


def parse_articles(payload, book_id):
    """Return ``(articles, group_count)``; one group is one push with 1-8 articles."""
    groups = payload.get("reviews")
    if not isinstance(groups, list):
        raise WeReadError("missing_reviews")
    articles = []
    for group in groups:
        if not isinstance(group, dict):
            raise WeReadError("invalid_group")
        subs = group.get("subReviews")
        subs = subs if isinstance(subs, list) and subs else [group]
        for sub in subs:
            review = sub.get("review") if isinstance(sub.get("review"), dict) else sub
            info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
            review_id = str(review.get("reviewId") or sub.get("reviewId") or "")
            title = re.sub(r"\s+", " ", html.unescape(str(info.get("title") or ""))).strip()
            if not review_id or not title:
                continue
            try:
                url = article_url(info.get("originalId"), review_id, book_id)
            except ValueError:
                continue
            published = info.get("time") or review.get("createTime") or group.get("createTime")
            try:
                published = int(published)
            except (TypeError, ValueError):
                published = 0
            articles.append({"review_id": review_id, "title": title[:500], "url": url,
                             "published_at": published, "digest": str(info.get("content") or "")[:1000],
                             "cover": str(info.get("pic_url") or "")[:500]})
    return articles, len(groups)
