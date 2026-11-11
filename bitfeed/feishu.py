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


def _webhook(prefix):
    webhook = os.getenv(prefix + "_WEBHOOK", "")
    parts = urlsplit(webhook)
    if parts.scheme!="https" or parts.hostname!="open.feishu.cn" or not parts.path.startswith("/open-apis/bot/v2/hook/") or parts.query or parts.fragment or parts.username or parts.password or parts.port not in (None,443):
        raise ValueError("configure an official Feishu custom-bot webhook")
    return webhook


def _card(title, markdown, template="blue"):
    return {"msg_type":"interactive","card":{"schema":"2.0","header":{"template":template,"title":{"tag":"plain_text","content":title}},"body":{"elements":[{"tag":"markdown","content":markdown[:12000]}]}}}


def _sign(payload, prefix):
    secret=os.getenv(prefix+"_SIGNING_SECRET","")
    if secret:
        stamp=str(int(time.time()))
        payload.update(timestamp=stamp,sign=base64.b64encode(hmac.new((stamp+"\n"+secret).encode(),b"",hashlib.sha256).digest()).decode())
    return payload


def _destination(mode):
    if mode not in ("test","production"):
        raise ValueError("invalid delivery mode")
    if mode=="production" and os.getenv("BITFEED_PRODUCTION_SEND","false").lower()!="true":
        raise ValueError("production delivery is disabled")
    prefix="FEISHU_TEST" if mode=="test" else "FEISHU_PRODUCTION"
    webhook=_webhook(prefix)
    target=hashlib.sha256(webhook.encode()).hexdigest()
    return prefix, webhook, target


def _week_receipt(conn, target, batch, start):
    row = conn.execute("SELECT state FROM deliveries WHERE target_hash=? AND batch_id=?",
                       (target, batch)).fetchone()
    if row:
        return row[0]
    # Older releases used the list of article IDs as the batch key, but also
    # recorded a dated weekly marker in the same transaction as a successful POST.
    old = conn.execute("""SELECT 1 FROM delivered_articles WHERE target_hash=?
        AND article_id GLOB 'weekly:????-??-??' AND julianday(sent_at) >= julianday(?)
        AND julianday(sent_at) < julianday(?, '+7 days') LIMIT 1""",
        (target, start, start)).fetchone()
    return "sent" if old else None


def week_receipt(store, mode, start):
    _, _, target = _destination(mode)
    with store.connection() as conn:
        return _week_receipt(conn, target, "weekly:" + start, start)


def send(store, markdown, article_ids, mode="test", session=None, title=None, *,
         week_start=None, publish_attachment=None, window_end=None):
    prefix, webhook, target = _destination(mode)
    batch = ("weekly:" + week_start if week_start else
             hashlib.sha256(json.dumps([mode,sorted(article_ids)],ensure_ascii=False).encode()).hexdigest())
    payload=_sign(_card(title or ("北理消息汇总"+(" · 测试" if mode=="test" else "")), markdown), prefix)
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if week_start:
            previous = _week_receipt(conn, target, batch, week_start)
        else:
            row = conn.execute("SELECT state FROM deliveries WHERE target_hash=? AND batch_id=?",
                               (target, batch)).fetchone()
            previous = row[0] if row else None
        if previous and not (week_start and previous == "rejected"):
            return {"state":previous,"duplicate":True}
        if publish_attachment:
            publish_attachment()
        conn.execute("INSERT OR REPLACE INTO deliveries VALUES(?,?,?,?,?)",(target,batch,"sending",now_iso(),""))
    try:
        response=(session or requests.Session()).post(webhook,json=payload,timeout=(10,30),allow_redirects=False)
        result=response.json()
        if isinstance(result, dict) and type(result.get("code")) is int:
            state="sent" if response.status_code==200 and result["code"]==0 else (
                "rejected" if result["code"] != 0 and response.status_code < 500 else "unknown")
        else:
            state="unknown"
    except (requests.RequestException,ValueError):
        state="unknown"  # The remote bot might have received it. Never blindly resend.
    with store.connection() as conn:
        conn.execute("UPDATE deliveries SET state=?,updated_at=? WHERE target_hash=? AND batch_id=?",(state,now_iso(),target,batch))
        if state=="sent":
            conn.executemany("INSERT OR IGNORE INTO delivered_articles VALUES(?,?,?)",[(target,x,now_iso()) for x in article_ids])
            if window_end is not None:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('digest:window_end', ?)", (str(window_end),))
    return {"state":state,"duplicate":False,"batch_id":batch}


def alert(title, markdown, template="orange", session=None):
    """Operations notice to the ops group (``FEISHU_OPS_WEBHOOK``, else the test group)."""
    prefix="FEISHU_OPS" if os.getenv("FEISHU_OPS_WEBHOOK") else "FEISHU_TEST"
    webhook=_webhook(prefix)
    try:
        response=(session or requests.Session()).post(webhook,json=_sign(_card(title,markdown,template),prefix),
                                                     timeout=(10,30),allow_redirects=False)
        result=response.json()
    except (requests.RequestException,ValueError):
        return "unknown"
    return "sent" if response.status_code==200 and result.get("code")==0 else "rejected"
