"""Explicit sending only; ambiguous delivery is held for manual review."""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlsplit

import requests

from .domain import now_iso


def validate_webhook_url(webhook: str) -> str:
    webhook = (webhook or "").strip()
    parts = urlsplit(webhook)
    if (
        parts.scheme != "https"
        or parts.hostname != "open.feishu.cn"
        or not parts.path.startswith("/open-apis/bot/v2/hook/")
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise ValueError("请配置正确的飞书自定义机器人 Webhook 地址（以 https://open.feishu.cn/open-apis/bot/v2/hook/ 开头）")
    return webhook


def _webhook(prefix):
    webhook = os.getenv(prefix + "_WEBHOOK", "")
    return validate_webhook_url(webhook)


def _card(title, markdown, template="blue"):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
            "body": {"elements": [{"tag": "markdown", "content": markdown[:12000]}]},
        },
    }


def _sign_with_secret(payload, secret):
    secret = (secret or "").strip()
    if secret:
        stamp = str(int(time.time()))
        payload.update(
            timestamp=stamp,
            sign=base64.b64encode(hmac.new((stamp + "\n" + secret).encode(), b"", hashlib.sha256).digest()).decode(),
        )
    return payload


def _sign(payload, prefix):
    return _sign_with_secret(payload, os.getenv(prefix + "_SIGNING_SECRET", ""))


def _destination(mode):
    if mode not in ("test", "production"):
        raise ValueError("invalid delivery mode")
    if mode == "production" and os.getenv("BITFEED_PRODUCTION_SEND", "false").lower() != "true":
        raise ValueError("production delivery is disabled")
    prefix = "FEISHU_TEST" if mode == "test" else "FEISHU_PRODUCTION"
    webhook = _webhook(prefix)
    target = hashlib.sha256(webhook.encode()).hexdigest()
    return prefix, webhook, target


def _week_receipt(conn, target, batch, start, cycle_days=14):
    row = conn.execute("SELECT state FROM deliveries WHERE target_hash=? AND batch_id=?", (target, batch)).fetchone()
    if row:
        return row[0]
    # Older releases used the list of article IDs as the batch key, but also
    # recorded a dated weekly marker in the same transaction as a successful POST.
    old = conn.execute(
        """SELECT 1 FROM delivered_articles WHERE target_hash=?
        AND article_id GLOB 'weekly:????-??-??' AND julianday(sent_at) >= julianday(?)
        AND julianday(sent_at) < julianday(?, ?) LIMIT 1""",
        (target, start, start, f"+{int(cycle_days)} days"),
    ).fetchone()
    return "sent" if old else None


def week_receipt(store, mode, start, cycle_days=14):
    _, _, target = _destination(mode)
    with store.connection() as conn:
        return _week_receipt(conn, target, "weekly:" + start, start, cycle_days=cycle_days)


def send(
    store,
    markdown,
    article_ids,
    mode="test",
    session=None,
    title=None,
    *,
    week_start=None,
    cycle_days=14,
    publish_attachment=None,
    window_end=None,
):
    prefix, webhook, target = _destination(mode)
    batch = (
        "weekly:" + week_start
        if week_start
        else hashlib.sha256(json.dumps([mode, sorted(article_ids)], ensure_ascii=False).encode()).hexdigest()
    )
    payload = _sign(_card(title or ("北理消息汇总" + (" · 测试" if mode == "test" else "")), markdown), prefix)
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if week_start:
            previous = _week_receipt(conn, target, batch, week_start, cycle_days=cycle_days)
        else:
            row = conn.execute(
                "SELECT state FROM deliveries WHERE target_hash=? AND batch_id=?",
                (target, batch),
            ).fetchone()
            previous = row[0] if row else None
        if previous and not (week_start and previous == "rejected"):
            return {"state": previous, "duplicate": True}
        if publish_attachment:
            publish_attachment()
        conn.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?)", (target, batch, "sending", now_iso(), ""))
    detail = ""
    try:
        response = (session or requests.Session()).post(
            webhook, json=payload, timeout=(10, 30), allow_redirects=False
        )
        result = response.json()
        if isinstance(result, dict) and type(result.get("code")) is int:
            state = (
                "sent"
                if response.status_code == 200 and result["code"] == 0
                else ("rejected" if result["code"] != 0 and response.status_code < 500 else "unknown")
            )
        else:
            state = "unknown"
        if state != "sent":
            code = result.get("code") if isinstance(result, dict) else None
            detail = f"HTTP {response.status_code}, Feishu code {code if type(code) is int else 'unavailable'}"
    except (requests.RequestException, ValueError):
        state = "unknown"  # The remote bot might have received it. Never blindly resend.
    with store.connection() as conn:
        conn.execute(
            "UPDATE deliveries SET state=?,updated_at=?,detail=? WHERE target_hash=? AND batch_id=?",
            (state, now_iso(), detail, target, batch),
        )
        if state == "sent":
            conn.executemany(
                "INSERT OR IGNORE INTO delivered_articles VALUES(?,?,?)",
                [(target, x, now_iso()) for x in article_ids],
            )
            if window_end is not None:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('digest:window_end', ?)", (str(window_end),))
    return {"state": state, "duplicate": False, "batch_id": batch, "detail": detail}


def send_direct(webhook, markdown, title, *, signing_secret="", session=None):
    """Send a stateless card via explicit webhook (used by GitHub Actions workflows)."""
    valid_hook = validate_webhook_url(webhook)
    payload = _sign_with_secret(_card(title, markdown), signing_secret)
    try:
        response = (session or requests.Session()).post(
            valid_hook, json=payload, timeout=(10, 30), allow_redirects=False
        )
        result = response.json()
        if isinstance(result, dict) and type(result.get("code")) is int:
            if response.status_code == 200 and result["code"] == 0:
                return {"state": "sent", "detail": ""}
            state = "rejected" if response.status_code < 500 else "unknown"
            return {"state": state, "detail": f"HTTP {response.status_code}, Feishu code {result['code']}"}
        return {"state": "unknown", "detail": f"HTTP {response.status_code}"}
    except (requests.RequestException, ValueError) as exc:
        return {"state": "unknown", "detail": type(exc).__name__}


def alert(title, markdown, template="orange", session=None):
    """Operations notice to the ops group (``FEISHU_OPS_WEBHOOK``, else the test group)."""
    prefix = "FEISHU_OPS" if os.getenv("FEISHU_OPS_WEBHOOK") else "FEISHU_TEST"
    webhook = _webhook(prefix)
    try:
        response = (session or requests.Session()).post(
            webhook,
            json=_sign(_card(title, markdown, template), prefix),
            timeout=(10, 30),
            allow_redirects=False,
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        return "unknown"
    return "sent" if response.status_code == 200 and result.get("code") == 0 else "rejected"
