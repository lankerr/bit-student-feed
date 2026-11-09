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


def send(store, markdown, article_ids, mode="test", session=None):
    if mode not in ("test","production"):
        raise ValueError("invalid delivery mode")
    if mode=="production" and os.getenv("BITFEED_PRODUCTION_SEND","false").lower()!="true":
        raise ValueError("production delivery is disabled")
    prefix="FEISHU_TEST" if mode=="test" else "FEISHU_PRODUCTION"
    webhook=os.getenv(prefix+"_WEBHOOK","")
    parts=urlsplit(webhook)
    if parts.scheme!="https" or parts.hostname!="open.feishu.cn" or not parts.path.startswith("/open-apis/bot/v2/hook/") or parts.query or parts.fragment or parts.username or parts.password or parts.port not in (None,443):
        raise ValueError("configure an official Feishu custom-bot webhook")
    target=hashlib.sha256(webhook.encode()).hexdigest()
    batch=hashlib.sha256(json.dumps([mode,sorted(article_ids)],ensure_ascii=False).encode()).hexdigest()
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT state FROM deliveries WHERE target_hash=? AND batch_id=?",(target,batch)).fetchone()
        if row:
            return {"state":row[0],"duplicate":True}
        conn.execute("INSERT INTO deliveries VALUES(?,?,?,?,?)",(target,batch,"sending",now_iso(),""))
    payload={"msg_type":"interactive","card":{"schema":"2.0","header":{"template":"blue","title":{"tag":"plain_text","content":"北理消息汇总"+(" · 测试" if mode=="test" else "")}},"body":{"elements":[{"tag":"markdown","content":markdown[:12000]}]}}}
    secret=os.getenv(prefix+"_SIGNING_SECRET","")
    if secret:
        stamp=str(int(time.time()))
        payload.update(timestamp=stamp,sign=base64.b64encode(hmac.new((stamp+"\n"+secret).encode(),b"",hashlib.sha256).digest()).decode())
    try:
        response=(session or requests.Session()).post(webhook,json=payload,timeout=(10,30),allow_redirects=False)
        result=response.json()
        state="sent" if response.status_code==200 and result.get("code")==0 else "rejected"
    except (requests.RequestException,ValueError):
        state="unknown"  # The remote bot might have received it. Never blindly resend.
    with store.connection() as conn:
        conn.execute("UPDATE deliveries SET state=?,updated_at=? WHERE target_hash=? AND batch_id=?",(state,now_iso(),target,batch))
        if state=="sent":
            conn.executemany("INSERT OR IGNORE INTO delivered_articles VALUES(?,?,?)",[(target,x,now_iso()) for x in article_ids])
    return {"state":state,"duplicate":False,"batch_id":batch}
