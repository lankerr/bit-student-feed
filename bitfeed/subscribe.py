"""Paced WeRead subscription worker, run every few minutes by a systemd timer.

Each run makes at most a handful of WeRead calls spaced 50-80 s apart (the pace
community WeRead collectors report as safe), tracked in a persistent state
file so restarts cannot burst. Quota answers back off exponentially; an
expired login stops the worker until the operator scans again.

WeRead's ``/api/mp/cover`` returns only the newest article of an account, so
each poll can discover at most one new article per account; the second and
later articles of a multi-article push are not visible through this route.
"""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import random
import time

from . import accounts as registry
from . import article_page
from .domain import TZ
from .weread import WeReadError, WeReadSession, article_url, private_json

GAP = (50, 80)
BODY_GAP = (25, 45)
CALLS_PER_RUN = 3
BODIES_PER_RUN = 2
DAILY_CALLS = 600
DAILY_BODIES = 240
BACKOFF = (1800, 3600, 7200, 14400, 28800, 43200)
BODY_BACKOFF = 2 * 3600
SHELF_REFRESH = 6 * 3600
FOLLOWS_PER_RUN = 2
QUIET_AFTER = 14 * 86400


def iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds") if ts else None


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


@contextmanager
def exclusive(path):
    handle = open(path, "a+")
    try:
        try:
            import fcntl
        except ImportError:  # Windows test runs
            fcntl = None
        if fcntl:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("another subscription run is active") from None
        yield
    finally:
        handle.close()


class Stop(Exception):
    """Ends the run early; the state already records why."""


class Worker:
    def __init__(self, store, root, registry_path=registry.DEFAULT_PATH, http=None, page_session=None,
                 clock=time.time, sleep=time.sleep, gap=GAP, body_gap=BODY_GAP):
        self.store, self.root = store, Path(root)
        self.registry_path, self.http, self.page_session = registry_path, http, page_session
        self.clock, self.sleep, self.gap, self.body_gap = clock, sleep, gap, body_gap
        self.state_path = self.root / "scheduler.json"

    # ---------- persistent state ----------
    def _load_state(self):
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state.setdefault("session", {"state": "unknown"})
        state.setdefault("limits", {})
        state.setdefault("shelf", {})
        state.setdefault("accounts", {})
        state.setdefault("body_attempts", {})
        return state

    def _save_state(self):
        private_json(self.state_path, self.state)

    # ---------- pacing ----------
    def _pace(self, kind):
        limits, now = self.state["limits"], self.clock()
        key = "last_" + kind + "_at"
        gap = self.gap if kind == "call" else self.body_gap
        wait = float(limits.get(key, 0)) + random.uniform(*gap) - now
        if wait > 0:
            self.sleep(wait)
        counter = limits.setdefault(kind + "s", {})
        day = today()
        for old in [d for d in counter if d != day]:
            counter.pop(old)
        cap = DAILY_CALLS if kind == "call" else DAILY_BODIES
        if counter.get(day, 0) >= cap:
            raise Stop("daily_cap")
        counter[day] = counter.get(day, 0) + 1
        limits[key] = self.clock()

    def _call(self, function, *args):
        limits = self.state["limits"]
        if float(limits.get("cooldown_until", 0)) > self.clock():
            raise Stop("cooldown")
        if self.calls_left <= 0:
            raise Stop("run_budget")
        self._pace("call")
        self.calls_left -= 1
        try:
            result = function(*args)
        except WeReadError as exc:
            if exc.state == "rate_limited":
                level = min(int(limits.get("level", 0)), len(BACKOFF) - 1)
                limits.update(cooldown_until=self.clock() + BACKOFF[level], level=level + 1,
                              last_limited_at=self.clock(), last_limited_code=exc.code)
                self.state["session"].update(state="ok", checked_at=self.clock())
                raise Stop("rate_limited") from None
            if exc.state in ("auth_expired", "auth_missing"):
                self.state["session"].update(state="auth_expired", checked_at=self.clock(), error=exc.code)
                raise Stop("auth_expired") from None
            raise
        self.state["session"].update(state="ok", checked_at=self.clock(), error="")
        if limits.get("level") and self.clock() - float(limits.get("last_limited_at", 0)) > 86400:
            limits["level"] = max(0, int(limits["level"]) - 1)
        return result

    # ---------- run ----------
    def run(self, calls=CALLS_PER_RUN, bodies=BODIES_PER_RUN, follows=FOLLOWS_PER_RUN):
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive(self.root / ".scheduler.lock"):
            self.state = self._load_state()
            self.calls_left = calls
            self.follows = follows
            summary = {"new": 0, "checked": 0, "followed": 0, "bodies": 0, "stopped": None}
            try:
                self.session = WeReadSession.load(self.root / "session.json", http=self.http)
            except WeReadError:
                self.state["session"].update(state="auth_missing", checked_at=self.clock())
                summary["stopped"] = "auth_missing"
            else:
                try:
                    self._subscribe(summary)
                except Stop as stop:
                    summary["stopped"] = str(stop)
                finally:
                    self.session.close()
            try:
                self._fetch_bodies(bodies, summary)
            except Stop as stop:
                summary["stopped"] = summary["stopped"] or str(stop)
            self._save_state()
            self._publish_audit()
            return summary

    def _subscribe(self, summary):
        accounts = registry.load(self.registry_path)
        records = self.state["accounts"]
        now = self.clock()
        for account in accounts:
            record = records.setdefault(account["book_id"], {"next_due": 0})
            record.update(name=account["name"], tier=account["tier"])
        shelf = self.state["shelf"]
        unknown = [a for a in accounts if records[a["book_id"]].get("followed") is None]
        if now - float(shelf.get("synced_at", 0)) > SHELF_REFRESH or unknown:
            books = self._call(self.session.shelf_books)
            shelf.update(synced_at=self.clock(), size=len(books))
            for account in accounts:
                records[account["book_id"]]["followed"] = account["book_id"] in books
        pending = [a for a in accounts if not records[a["book_id"]].get("followed")
                   and float(records[a["book_id"]].get("follow_retry_at", 0)) <= self.clock()]
        for account in pending[:self.follows]:
            record = records[account["book_id"]]
            try:
                self._call(self.session.follow, account["book_id"])
                record["followed"] = True
                summary["followed"] += 1
            except WeReadError as exc:
                attempts = int(record.get("follow_attempts", 0)) + 1
                record.update(state="follow_failed", error=exc.code, follow_attempts=attempts,
                              follow_retry_at=self.clock() + (86400 if attempts >= 3 else 3600))
        # After three refused follows the cover is still tried: WeRead may serve it unfollowed.
        due = sorted((a for a in accounts if (records[a["book_id"]].get("followed")
                      or int(records[a["book_id"]].get("follow_attempts", 0)) >= 3)
                      and float(records[a["book_id"]].get("next_due", 0)) <= self.clock()),
                     key=lambda a: (float(records[a["book_id"]].get("next_due", 0)), a["tier"]))
        for account in due:
            self._poll(account, records[account["book_id"]], summary)

    def _poll(self, account, record, summary):
        interval = account["interval"]
        try:
            cover = self._call(self.session.cover, account["book_id"])
        except WeReadError as exc:
            record.update(state="empty" if exc.state == "not_found" else "failed", error=exc.code,
                          last_checked=self.clock(), errors=int(record.get("errors", 0)) + 1,
                          next_due=self.clock() + (12 * 3600 if exc.state == "not_found" else min(interval, 7200)))
            return
        now = self.clock()
        summary["checked"] += 1
        quiet = now - float(record.get("last_new") or now) > QUIET_AFTER
        record.update(state="ok", error="", errors=0, last_checked=now, last_ok=now,
                      next_due=now + min(86400, interval * (2 if quiet else 1)) * random.uniform(0.9, 1.1))
        review = str(cover.get("reviewId") or "")
        title = str(cover.get("title") or "").strip()
        if not review or review == record.get("last_review") or not title:
            return
        try:
            url = article_url(review_id=review, book_id=account["book_id"])
            added = self.store.import_articles([{"url": url, "title": title, "source": account["name"],
                "published_at": int(now), "publication_status": "unknown", "kind": "wechat",
                "origin": "weread", "digest": str(cover.get("digest") or cover.get("content") or "")[:1000],
                "body_status": "not_fetched"}])["added"]
        except ValueError as exc:
            record.update(state="failed", error="unverified_article:" + str(exc)[:40])
            return
        record.update(last_review=review, last_title=title[:120])
        if added:
            record.update(last_new=now, new_total=int(record.get("new_total", 0)) + added)
            summary["new"] += added

    def _fetch_bodies(self, limit, summary):
        limits = self.state["limits"]
        if float(limits.get("body_cooldown_until", 0)) > self.clock() or limit <= 0:
            return
        attempts = self.state["body_attempts"]
        for article in self.store.missing_bodies("weread", limit=limit * 3):
            if summary["bodies"] >= limit:
                break
            if attempts.get(article["id"], 0) >= 3:
                continue
            self._pace("body")
            try:
                page = article_page.fetch(article["url"], session=self.page_session)
            except article_page.PageError as exc:
                attempts[article["id"]] = attempts.get(article["id"], 0) + 1
                if exc.state == "blocked":
                    limits["body_cooldown_until"] = self.clock() + BODY_BACKOFF
                    raise Stop("body_blocked") from None
                if exc.state == "deleted":
                    self.store.attach_body(article["id"], "", None, "deleted")
                continue
            self.store.attach_body(article["id"], page["body"], page["published_at"],
                                   "available" if page["body"] else "title_only")
            attempts.pop(article["id"], None)
            summary["bodies"] += 1
        for key in [k for k in attempts if attempts[k] >= 3][:-200]:
            attempts.pop(key)

    # ---------- public audit ----------
    def _publish_audit(self):
        audits = []
        for book_id, record in sorted(self.state["accounts"].items(), key=lambda kv: (kv[1].get("tier", 9), kv[1].get("name", ""))):
            state = record.get("state") or ("pending_follow" if not record.get("followed") else "pending")
            audits.append({"id": "weread-" + book_id, "name": "公众号·" + record.get("name", book_id),
                "origin": "weread", "scope": "weread_latest", "checked_at": iso(record.get("last_checked")),
                "last_successful_at": iso(record.get("last_ok")), "state": state,
                "pages": 1 if record.get("last_checked") else 0, "fetched": int(record.get("new_total", 0)),
                "count": None, "complete": False, "truncated": False, "error": record.get("error") or ""})
        limits = self.state["limits"]
        summary = {"session": self.state["session"].get("state"), "checked_at": iso(self.state["session"].get("checked_at")),
                   "accounts": len(audits), "followed": sum(1 for r in self.state["accounts"].values() if r.get("followed")),
                   "healthy": sum(1 for a in audits if a["state"] == "ok"),
                   "cooldown_until": iso(float(limits.get("cooldown_until", 0))) if float(limits.get("cooldown_until", 0)) > self.clock() else None,
                   "calls_today": (limits.get("calls") or {}).get(today(), 0),
                   "bodies_today": (limits.get("bodies") or {}).get(today(), 0),
                   "last_call_at": iso(limits.get("last_call_at"))}
        self.store.set_meta("weread_audit", json.dumps(audits, ensure_ascii=False))
        self.store.set_meta("weread_status", json.dumps(summary, ensure_ascii=False))
