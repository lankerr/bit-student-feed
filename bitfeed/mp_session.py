"""Keep the operator's public-platform login alive and use it for account search.

Opening the platform home page re-issues the login cookies with a fresh
four-day expiry (observed 2026-11-10), so a keepalive every few hours keeps
the session indefinitely. The cross-account article list (``appmsgpublish``)
has been refused for every third party since 2026-07-30 and is not used;
``searchbiz`` still resolves account names to fakeids.
"""
import json
from pathlib import Path
import re
import time

import requests

from .weread import private_json

BASE = "https://mp.weixin.qq.com"
KEY_COOKIES = ("slave_sid", "slave_user", "data_ticket", "bizuin")


class MpError(RuntimeError):
    def __init__(self, code, state="failed"):
        super().__init__(code)
        self.code, self.state = code, state


class MpSession:
    def __init__(self, path, http=None):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not self.data.get("token") or not isinstance(self.data.get("cookies"), list):
                raise ValueError()
        except (OSError, ValueError):
            raise MpError("session_unavailable", "auth_missing") from None
        self.http = http or requests.Session()
        self.http.trust_env = False

    def expires_at(self):
        stamps = [int(c["expires"]) for c in self.data["cookies"]
                  if c.get("name") in KEY_COOKIES and isinstance(c.get("expires"), (int, float))]
        return min(stamps) if stamps else None

    def _cookie_header(self):
        latest = {}
        for cookie in self.data["cookies"]:
            if cookie.get("name") and cookie.get("value") is not None:
                latest[cookie["name"]] = cookie["value"]
        return "; ".join(f"{k}={v}" for k, v in latest.items())

    def _absorb(self, response):
        changed = False
        index = {c["name"]: c for c in self.data["cookies"] if c.get("name")}
        for cookie in response.cookies:
            entry = index.get(cookie.name)
            fresh = {"name": cookie.name, "value": cookie.value, "domain": cookie.domain or "mp.weixin.qq.com",
                     "path": cookie.path or "/", "expires": cookie.expires}
            if entry is None:
                self.data["cookies"].append(fresh)
                changed = True
            elif entry.get("value") != cookie.value or entry.get("expires") != cookie.expires:
                entry.update(fresh)
                changed = True
        if changed:
            self.data["refreshed_at"] = int(time.time())
            private_json(self.path, self.data)
        return changed

    def _get(self, path, params):
        # Keep the browser identity the login was made with.
        agent = self.data.get("user_agent") or "Mozilla/5.0"
        try:
            response = self.http.get(BASE + path, params={**params, "token": self.data["token"], "lang": "zh_CN"},
                headers={"User-Agent": agent, "Referer": BASE + "/", "Cookie": self._cookie_header()},
                timeout=(10, 30), allow_redirects=False)
        except requests.RequestException as exc:
            raise MpError(type(exc).__name__) from None
        self._absorb(response)
        if response.status_code in (301, 302, 303, 307, 308):
            raise MpError("redirect_to_login", "auth_expired")
        if response.status_code != 200:
            raise MpError("http_" + str(response.status_code))
        return response

    def keepalive(self):
        """Load the home page; returns the new key-cookie expiry timestamp."""
        page = self._get("/cgi-bin/home", {"t": "home/index"}).text
        if "wx.cgiData" not in page or re.search(r"bizlogin\?action=startlogin|请重新登录", page[:4000]):
            raise MpError("not_logged_in", "auth_expired")
        self.data["kept_alive_at"] = int(time.time())
        private_json(self.path, self.data)
        return self.expires_at()

    def search(self, query, count=5):
        response = self._get("/cgi-bin/searchbiz", {"action": "search_biz", "begin": 0, "count": count,
                                                    "query": query, "f": "json", "ajax": 1})
        try:
            data = response.json()
        except ValueError:
            raise MpError("non_json", "blocked") from None
        ret = (data.get("base_resp") or {}).get("ret")
        if ret in (200003, "200003"):
            raise MpError("200003", "auth_expired")
        if ret in (200013, "200013"):
            raise MpError("200013", "rate_limited")
        if ret not in (0, "0"):
            raise MpError("ret_" + str(ret))
        rows = data.get("list") if isinstance(data.get("list"), list) else []
        return [{"fakeid": r.get("fakeid"), "nickname": r.get("nickname"), "alias": r.get("alias"),
                 "verify_status": r.get("verify_status"), "service_type": r.get("service_type"),
                 "signature": (r.get("signature") or "")[:120]} for r in rows if isinstance(r, dict)]

    def close(self):
        self.http.close()


def keepalive(directory):
    """Run one keepalive; record the outcome for alerts and the ops page."""
    root = Path(directory)
    status = {"checked_at": int(time.time())}
    try:
        session = MpSession(root / "session.json")
    except MpError as exc:
        status.update(state=exc.state, error=exc.code)
    else:
        try:
            status.update(state="ok", expires_at=session.keepalive())
        except MpError as exc:
            status.update(state=exc.state, error=exc.code, expires_at=session.expires_at())
        finally:
            session.close()
    private_json(root / "keepalive.json", status)
    return status
