"""Read authorized WeChat platform public lists; never rotate endpoints/accounts.

The private auth file is supplied by the operator's server-side QR login. This
module does not access a desktop WeChat process, database or browser profile.
"""
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import requests

from .domain import article_id, normalize_url, now_iso


BASE = "https://mp.weixin.qq.com"
ORIGIN = "server_platform"
COOLDOWN_SECONDS = 30 * 60
ENDPOINTS = {
    "free_publish": {"action": "list"},
    "publish": {"action": "publish_list"},
    "appmsgpublish": {"sub": "list", "sub_action": "list_ex"},
    "appmsg": {"action": "list_ex", "type": "9"},
}


class PlatformError(RuntimeError):
    """Only a fixed error code and public state are safe to put in logs."""
    def __init__(self, code, state="failed", until=None):
        super().__init__(code)
        self.code, self.state, self.until = code, state, until


def _text(value, limit=500):
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _integer(value, minimum=0, maximum=10**15):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,24}", str(value)):
        raise PlatformError("invalid_number")
    number = int(value)
    if not minimum <= number <= maximum:
        raise PlatformError("invalid_number")
    return number


def _fakeid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_+\-/=]{1,150}", value):
        raise PlatformError("invalid_public_account_id")
    return value


def _mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise PlatformError("invalid_nested_json") from None
    if not isinstance(value, dict):
        raise PlatformError("invalid_public_list")
    return value


class Platform:
    def __init__(self, auth_path, session=None):
        self.auth_path = Path(auth_path)
        self.cooldown_path = self.auth_path.with_name("cooldown.json")
        self._owned_session = session is None
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._source_names = {}
        self._local_stop = None
        try:
            auth = json.loads(self.auth_path.read_text(encoding="utf-8-sig"))
            token = auth.get("token")
            cookies = auth.get("cookies")
            if (not isinstance(token, (str, int)) or isinstance(token, bool) or
                    not str(token) or len(str(token)) > 512 or re.search(r"[\r\n]", str(token))
                    or not isinstance(cookies, list) or not cookies):
                raise ValueError()
            self._token = str(token)
            self._session.cookies.clear()
            accepted = 0
            for cookie in cookies:
                if not isinstance(cookie, dict):
                    raise ValueError()
                name, value = cookie.get("name"), cookie.get("value")
                domain = cookie.get("domain") or "mp.weixin.qq.com"
                if domain not in {"mp.weixin.qq.com", ".mp.weixin.qq.com", "weixin.qq.com", ".weixin.qq.com", "qq.com", ".qq.com"}:
                    continue
                if (not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                        or not isinstance(value, str) or re.search(r"[\r\n]", value)):
                    raise ValueError()
                expires = cookie.get("expires")
                if expires is not None:
                    expires = int(expires)
                    if expires < 0:
                        expires = None
                self._session.cookies.set(name, value, domain=domain,
                                          path=cookie.get("path") or "/", secure=True, expires=expires)
                accepted += 1
            if not accepted:
                raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            self.close()
            raise PlatformError("auth_unavailable", "auth_missing") from None

    def close(self):
        if self._owned_session:
            self._session.close()

    def _read_stop(self):
        try:
            value = json.loads(self.cooldown_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            raise PlatformError("cooldown_unreadable", "blocked") from None
        if (not isinstance(value, dict) or set(value) != {"until", "state"}
                or value.get("state") not in {"rate_limited", "auth_expired"}
                or isinstance(value.get("until"), bool) or not isinstance(value.get("until"), (int, float))
                or not math.isfinite(value["until"]) or value["until"] < 0):
            raise PlatformError("cooldown_invalid", "blocked")
        return value

    def _guard(self):
        # Consult shared state before every request, including calls made through
        # another Platform instance after a scheduled process restart.
        states = [value for value in (self._read_stop(), self._local_stop) if value]
        for value in states:
            if value["state"] == "auth_expired" or value["until"] > time.time():
                raise PlatformError(value["state"], value["state"], value["until"])

    def _stop(self, state):
        value = {"until": int(time.time()) + COOLDOWN_SECONDS if state == "rate_limited" else 0,
                 "state": state}
        self._local_stop = value
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".cooldown-", suffix=".json", dir=self.cooldown_path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(value, file, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.cooldown_path)
        except OSError:
            raise PlatformError("cooldown_persist_failed", "blocked") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        raise PlatformError(state, state, value["until"])

    def _get(self, endpoint, params):
        self._guard()
        try:
            response = self._session.get(BASE + "/cgi-bin/" + endpoint,
                params={**params, "token": self._token, "lang": "zh_CN", "f": "json", "ajax": "1"},
                headers={"User-Agent": "bit-student-feed/0.2", "Referer": BASE + "/",
                         "Cookie": None, "Authorization": ""},
                auth=(), proxies={"http": "", "https": "", "all": ""},
                verify=True, timeout=(10, 30), allow_redirects=False)
        except requests.RequestException:
            raise PlatformError("request_failed") from None
        if response.status_code == 429:
            self._stop("rate_limited")
        if response.status_code != 200:
            raise PlatformError("http_" + str(response.status_code),
                                "blocked" if response.status_code in (301,302,303,307,308,401,403) else "failed")
        if len(getattr(response, "content", b"")) > 5000000:
            raise PlatformError("response_too_large")
        try:
            data = response.json()
        except ValueError:
            raise PlatformError("unexpected_non_json", "blocked") from None
        if not isinstance(data, dict) or not isinstance(data.get("base_resp"), dict):
            raise PlatformError("invalid_public_response")
        ret = data["base_resp"].get("ret")
        if ret in (200013, "200013"):
            self._stop("rate_limited")
        if ret in (200003, "200003"):
            self._stop("auth_expired")
        if isinstance(ret, bool) or ret not in (0, "0"):
            raise PlatformError("api_rejected")
        return data

    def search(self, query, count=5):
        query = _text(query, 121)
        if not query or len(query) > 120:
            raise PlatformError("invalid_search_query")
        count = _integer(count, 1, 20)
        data = self._get("searchbiz", {"action":"search_biz", "begin":0, "count":count, "query":query})
        rows = data.get("list")
        if not isinstance(rows, list):
            raise PlatformError("invalid_search_result")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise PlatformError("invalid_search_result")
            identifier = _fakeid(row.get("fakeid"))
            if identifier in seen:
                continue
            seen.add(identifier)
            item = {"fakeid":identifier, "nickname":_text(row.get("nickname"),120),
                    "alias":_text(row.get("alias"),120)}
            if isinstance(row.get("verify_status"), int) and not isinstance(row["verify_status"], bool):
                item["verify_status"] = row["verify_status"]
            self._source_names[identifier] = item["nickname"] or identifier
            result.append(item)
        return result

    def _article(self, raw, group, fakeid):
        if not isinstance(raw, dict):
            raise PlatformError("invalid_article_row")
        title = _text(raw.get("title"))
        parts = urlsplit(html.unescape(str(raw.get("link") or raw.get("url") or "")))
        query = parse_qs(parts.query)
        if (parts.hostname != "mp.weixin.qq.com" or parts.scheme not in {"http","https"}
                or parts.path != "/s" and not parts.path.startswith("/s/")):
            raise PlatformError("invalid_article_url", "source_mismatch")
        if query.get("__biz") != [fakeid]:
            raise PlatformError("article_account_mismatch", "source_mismatch")
        if len(query.get("mid", [])) != 1 or len(query.get("idx", [])) != 1:
            raise PlatformError("article_identity_unverified", "source_mismatch")
        _integer(query["mid"][0], 1)
        _integer(query["idx"][0], 1, 100)
        try:
            url = normalize_url(parts.geturl())
        except ValueError:
            raise PlatformError("invalid_article_url", "source_mismatch") from None
        if not title:
            raise PlatformError("missing_article_title")
        published = next((value for value in (raw.get("create_time"), raw.get("publish_time"),
                         group.get("publish_time"), group.get("create_time")) if value not in (None,"",0,"0")), None)
        published = _integer(published, 946684800, int(time.time())+86400)
        return {"id":article_id(url), "source_article_id":_text(str(raw.get("aid") or ""),100),
                "source":self._source_names.get(fakeid) or fakeid, "source_id":fakeid,
                "title":title, "url":url, "published_at":published, "publication_status":"known",
                "body":"", "digest":_text(raw.get("digest"),1000), "body_status":"not_fetched",
                "kind":"wechat", "origin":ORIGIN}

    def list_page(self, fakeid, begin=0, count=5, endpoint="appmsgpublish"):
        fakeid = _fakeid(fakeid)
        begin, count = _integer(begin,0,1000000), _integer(count,1,20)
        if endpoint not in ENDPOINTS:
            raise PlatformError("unsupported_endpoint")
        data = self._get(endpoint, {**ENDPOINTS[endpoint],"begin":begin,"count":count,"fakeid":fakeid})
        container, prefix = data, ""
        if "publish_page" in data:
            container, prefix = _mapping(data["publish_page"]), "publish_page."
        rows = None
        for key in ("free_publish_list", "publish_list", "app_msg_list"):
            if key in container:
                rows = container[key]
                break
        if not isinstance(rows, list):
            raise PlatformError("missing_article_list")
        articles, group_ids = {}, []
        for group in rows:
            group = _mapping(group)
            info = _mapping(group["publish_info"]) if "publish_info" in group else group
            raw_articles = info.get("appmsgex", [info])
            if not isinstance(raw_articles, list) or not raw_articles:
                raise PlatformError("invalid_publication_group")
            ids = []
            for raw in raw_articles:
                article = self._article(raw, group, fakeid)
                articles.setdefault(article["id"], article)
                ids.append(article["id"])
            group_ids.append(hashlib.sha256("|".join(sorted(ids)).encode()).hexdigest())
        result = {"state":"ok", "origin":ORIGIN, "endpoint":endpoint, "begin":begin,
                  "requested_count":count, "groups":len(rows), "group_ids":group_ids,
                  "article_count":len(articles), "articles":list(articles.values())}
        for target, candidates in (("total",("total_count","total")), ("count",("count",))):
            for key in candidates:
                if container.get(key) is not None:
                    result[target] = _integer(container[key])
                    result[target+"_field"] = prefix+key
                    break
        return result


def collect(config_path, auth_path, max_pages=100, session=None):
    """Page only explicitly configured account/endpoint pairs; never fall back.

    Config is ``{"accounts":[{"fakeid":"...","name":"...","endpoint":"appmsgpublish"}]}``.
    A successful manual reauthorization must clear an ``auth_expired`` cooldown
    file; session expiry is never cleared merely because time has passed.
    """
    max_pages = _integer(max_pages,1,1000)
    checked = now_iso()
    result = {"origin":ORIGIN,"collected_at":checked,"articles":[],"sources":[],"state":"unconfigured"}
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        accounts = config.get("accounts") if isinstance(config,dict) else None
        if not isinstance(accounts,list):
            raise ValueError()
    except (OSError,ValueError):
        result.update(state="failed",error="invalid_account_configuration")
        return result
    if not accounts:
        return result
    platform, fatal, all_articles = None, None, {}
    try:
        try:
            platform = Platform(auth_path,session)
        except PlatformError as exc:
            fatal = exc
        for index,spec in enumerate(accounts):
            spec = spec if isinstance(spec,dict) else {}
            audit = {"id":_text(spec.get("id"),150) or "platform-"+str(index+1),
                "name":_text(spec.get("name") or spec.get("nickname"),120) or "已配置公众号",
                "origin":ORIGIN,"scope":"authorized_platform_list","checked_at":checked,
                "endpoint":spec.get("endpoint","appmsgpublish"),"state":"ok","pages":0,
                "groups":0,"fetched":0,"accepted":0,"complete":False,"exhausted":False,
                "count_changed":False,"truncated":False}
            result["sources"].append(audit)
            try:
                if spec.get("enabled") is False:
                    audit["state"] = "disabled"
                    continue
                if fatal is not None:
                    raise fatal
                fakeid = _fakeid(spec.get("fakeid"))
                platform._source_names[fakeid] = audit["name"]
                groups, identities, seen_pages, total = set(), set(), set(), None
                for page_number in range(max_pages):
                    page = platform.list_page(fakeid,begin=page_number*5,count=5,endpoint=audit["endpoint"])
                    audit["pages"] += 1
                    fingerprint = tuple(page["group_ids"])
                    if fingerprint and fingerprint in seen_pages:
                        raise PlatformError("repeated_page")
                    seen_pages.add(fingerprint)
                    old_count = len(groups)
                    groups.update(page["group_ids"])
                    for article in page["articles"]:
                        identities.add(article["id"])
                        all_articles.setdefault(article["id"],article)
                    audit.update(groups=len(groups),fetched=len(identities),accepted=len(identities))
                    if "total" in page:
                        if total is None:
                            total=page["total"]
                            audit["total_field"]=page["total_field"]
                        elif total != page["total"]:
                            audit["count_changed"]=True
                        audit["count"]=total
                    if page["groups"]==0 or (total is not None and page_number*5+page["groups"]>=total) or page["groups"]<5:
                        audit["exhausted"]=True
                        break
                    if len(groups)==old_count:
                        raise PlatformError("pagination_without_progress")
                audit["truncated"]=not audit["exhausted"]
                audit["count_matches"]=total is not None and total==len(groups)
                audit["complete"]=audit["exhausted"] and audit["count_matches"] and not audit["count_changed"]
                if not audit["complete"]:
                    audit["state"]="partial"
            except PlatformError as exc:
                audit.update(state=exc.state,error=exc.code,complete=False,truncated=True)
                if exc.until is not None:
                    audit["retry_after"]=exc.until
                if exc.state=="auth_expired":
                    audit["reauthorization_required"]=True
                if exc.state in {"rate_limited","auth_expired","blocked","auth_missing"}:
                    fatal=exc
    finally:
        if platform is not None:
            platform.close()
    result["articles"]=list(all_articles.values())
    states={item["state"] for item in result["sources"]}
    result["state"]=("ok" if states<={"ok","disabled"} else
        "partial" if all_articles else fatal.state if fatal else "failed")
    return result
