"""Public export endpoint plus a token-protected page for renewing logins.

``/site-data.json`` feeds the GitHub Pages build and ``/feed.json`` keeps the
school-platform workflow contract. ``/ops/<token>/`` lets the operator start a
WeRead or public-platform QR login from any browser; the token comes from
``BITFEED_OPS_TOKEN`` and every other path under ``/ops`` answers 404.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .domain import TZ
from .store import Store

SITE = Path(os.getenv("BITFEED_SITE_DIR", "/var/lib/bitfeed-pipeline/site"))
LIVE = Path(os.getenv("BITFEED_DATA", "/var/lib/bitfeed-live"))
WEREAD = Path(os.getenv("BITFEED_WEREAD_DIR", "/var/lib/bitfeed-weread"))
WECHAT = Path(os.getenv("BITFEED_WECHAT_DIR", "/var/lib/bitfeed-wechat"))
RECEIPTS = Path(os.getenv("BITFEED_RECEIPTS", "/var/lib/bitfeed-pipeline/receipts.sqlite"))
LOGINS = {"weread": ("bitfeed.weread_auth", WEREAD, "login-status.json", "weread-qr.png", "900"),
          "mp": ("bitfeed.wechat_auth", WECHAT, "status.json", "qr.png", "300")}
_running = {}
_lock = threading.Lock()


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _when(ts):
    try:
        return datetime.fromtimestamp(float(ts), TZ).strftime("%m-%d %H:%M") if ts else None
    except (TypeError, ValueError):
        return None


def status():
    scheduler = _read(WEREAD / "scheduler.json")
    records = (scheduler.get("accounts") or {}).values()
    keep = _read(WECHAT / "keepalive.json")
    with _lock:
        running = {k: p.poll() is None for k, p in _running.items()}
    return {
        "weread": {"session": (scheduler.get("session") or {}).get("state", "unknown"),
                   "checked_at": _when((scheduler.get("session") or {}).get("checked_at")),
                   "accounts": len(scheduler.get("accounts") or {}),
                   "healthy": sum(1 for r in records if r.get("state") == "ok"),
                   "cooldown_until": _when((scheduler.get("limits") or {}).get("cooldown_until"))
                       if float((scheduler.get("limits") or {}).get("cooldown_until", 0)) > time.time() else None,
                   "login": _read(WEREAD / "login-status.json").get("state"), "login_running": running.get("weread", False)},
        "mp": {"session": keep.get("state", "unknown"), "checked_at": _when(keep.get("checked_at")),
               "expires_at": _when(keep.get("expires_at")),
               "login": _read(WECHAT / "status.json").get("state"), "login_running": running.get("mp", False)}}


def start_login(kind):
    module, root, status_name, qr_name, wait = LOGINS[kind]
    with _lock:
        process = _running.get(kind)
        if process is not None and process.poll() is None:
            return False
        for name in (status_name, qr_name):
            try:
                (root / name).unlink()
            except OSError:
                pass
        log = open(root / ("login-" + kind + ".log"), "ab")
        _running[kind] = subprocess.Popen([sys.executable, "-m", module, "--directory", str(root), "--wait-seconds", wait],
                                          cwd=str(Path(__file__).parent.parent), stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=log, start_new_session=True)
        log.close()
    return True


def feed(days, limit):
    articles = Store(LIVE).public_articles(days=days, limit=limit) if (LIVE / "bitfeed.sqlite").exists() else []
    rows = [{k: a[k] for k in ("id", "title", "url", "source", "published_at")} for a in articles]
    return {"schema_version": 1, "mode": "test", "production_delivery_enabled": False,
            "batch_id": hashlib.sha256("|".join(r["id"] for r in rows).encode()).hexdigest(),
            "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
            "source": "VPS 采集的北理工公开来源：官网、就业网与公众号（经微信读书）", "days": days,
            "count": len(rows), "articles": rows,
            "notice": "仅为新发布消息索引；发布时间不等于活动时间，报名条件请核对原文。"}


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>北理消息站 · 登录维护</title><style>
body{font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#1f2329}
main{max-width:720px;margin:32px auto;padding:0 16px}h1{font-size:22px;margin:0 0 4px}p.sub{color:#646a73;margin:0 0 20px}
section{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
h2{font-size:17px;margin:0 0 8px}dl{display:grid;grid-template-columns:8em 1fr;gap:4px 12px;margin:0 0 12px}dt{color:#646a73}
button{font:inherit;padding:7px 16px;border:0;border-radius:6px;background:#3370ff;color:#fff;cursor:pointer}button:disabled{background:#a6bdf7}
.qr{margin-top:12px;text-align:center}.qr img{width:240px;height:240px;image-rendering:pixelated}.note{color:#646a73;font-size:13px}
.ok{color:#1f8f4e}.bad{color:#d14343}</style></head><body><main>
<h1>登录维护</h1><p class="sub">服务器保存的两个登录。平时自动续期；失效后在这里扫码恢复，不需要另外的电脑。</p>
<section><h2>微信读书（订阅公众号文章）</h2><dl id="weread"></dl><button data-kind="weread">扫码登录微信读书</button><div class="qr" id="qr-weread"></div></section>
<section><h2>公众号后台（搜号与核验）</h2><dl id="mp"></dl><button data-kind="mp">扫码登录公众号后台</button><div class="qr" id="qr-mp"></div>
<p class="note">需要用对该公众号有管理权限的微信扫码。</p></section>
<p class="note">手机上打开本页时：截图二维码，再在微信“扫一扫”里从相册选图即可。</p></main><script>
const base=location.pathname.replace(/\\/?$/,'/');const names={ok:'正常',authorized:'已登录',unknown:'未检查',auth_missing:'未登录',auth_expired:'已失效',ready:'请扫码',scanned:'已扫码，请在手机确认',expired:'二维码已过期',starting:'正在获取二维码',qr_expired_regenerating:'二维码已刷新',failed:'失败',verification_failed:'登录未通过校验',no_platform_account:'该微信没有可登录的公众号'};
function row(dl,k,v){const t=document.createElement('dt');t.textContent=k;const d=document.createElement('dd');d.textContent=v??'—';d.style.margin=0;dl.append(t,d)}
async function refresh(){const s=await (await fetch(base+'status.json',{cache:'no-store'})).json();
 const w=document.getElementById('weread');w.replaceChildren();row(w,'会话',names[s.weread.session]||s.weread.session);row(w,'最近检查',s.weread.checked_at);row(w,'订阅',s.weread.healthy+' / '+s.weread.accounts+' 个号正常');if(s.weread.cooldown_until)row(w,'限流暂停至',s.weread.cooldown_until);
 const m=document.getElementById('mp');m.replaceChildren();row(m,'会话',names[s.mp.session]||s.mp.session);row(m,'最近续期',s.mp.checked_at);row(m,'有效期至',s.mp.expires_at);
 for(const kind of ['weread','mp']){const box=document.getElementById('qr-'+kind);const st=s[kind].login;
  if(s[kind].login_running&&['ready','qr_expired_regenerating','scanned','starting'].includes(st)){box.innerHTML='<img alt="登录二维码" src="'+base+'qr/'+kind+'.png?'+Date.now()+'"><div>'+(names[st]||st)+'</div>'}
  else if(st&&box.dataset.started){box.innerHTML='<div class="'+(st==='authorized'?'ok':'bad')+'">'+(names[st]||st)+'</div>'}}}
document.querySelectorAll('button[data-kind]').forEach(b=>b.onclick=async()=>{b.disabled=true;document.getElementById('qr-'+b.dataset.kind).dataset.started=1;
 await fetch(base+'login/'+b.dataset.kind,{method:'POST'});setTimeout(()=>b.disabled=false,20000)});
refresh();setInterval(refresh,3000);</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "bitfeed"

    def log_message(self, *_):
        pass  # no URLs (they contain the ops token) or visitor addresses in logs

    def _send(self, status_code, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if content_type.startswith("text/html"):
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(body)

    def _ops(self, path):
        token = os.getenv("BITFEED_OPS_TOKEN", "")
        match = re.fullmatch(r"/ops/([A-Za-z0-9_-]{20,128})(/.*)?", path)
        if not token or not match or not hmac.compare_digest(match.group(1), token):
            return None
        return match.group(2) or "/"

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path == "/health":
            return self._send(200, {"ok": True, "role": "public-article-index", "schedule": "Thu,Sun 12:00 Asia/Shanghai"})
        if parts.path == "/site-data.json":
            try:
                return self._send(200, (SITE / "data.json").read_bytes())
            except OSError:
                return self._send(503, {"error": "daily export not yet available"})
        if parts.path == "/feed.json":
            query = parse_qs(parts.query)
            try:
                days = max(1, min(90, int(query.get("days", [7])[0])))
                limit = max(1, min(5000, int(query.get("limit", [60])[0])))
            except ValueError:
                return self._send(400, {"error": "invalid days/limit"})
            return self._send(200, feed(days, limit))
        sub = self._ops(parts.path)
        if sub is None:
            return self._send(404, {"error": "not found"})
        if sub == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if sub == "/status.json":
            return self._send(200, status())
        if sub in ("/qr/weread.png", "/qr/mp.png"):
            _, root, _, qr_name, _ = LOGINS[sub[4:-4]]
            try:
                return self._send(200, (root / qr_name).read_bytes(), "image/png")
            except OSError:
                return self._send(404, {"error": "no QR yet"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path == "/test-receipts":
            return self._receipt()
        sub = self._ops(parts.path)
        if sub in ("/login/weread", "/login/mp"):
            return self._send(200, {"started": start_login(sub[7:])})
        return self._send(404, {"error": "not found"})

    def _receipt(self):
        size = int(self.headers.get("Content-Length", "0") or 0)
        if not 0 < size <= 24000:
            return self._send(413, {"error": "test report must be <=24KB"})
        try:
            payload = json.loads(self.rfile.read(size))
            if payload.get("mode") != "test" or not isinstance(payload.get("report"), str):
                raise ValueError()
        except (ValueError, AttributeError):
            return self._send(400, {"error": "expected test report"})
        serialized = json.dumps({"mode": "test", "report": payload["report"][:18000]}, ensure_ascii=False, sort_keys=True)
        identifier = hashlib.sha256(serialized.encode()).hexdigest()
        with closing(sqlite3.connect(RECEIPTS, timeout=15)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS test_receipts(id TEXT PRIMARY KEY, created_at INTEGER, payload TEXT)")
            new = conn.execute("INSERT OR IGNORE INTO test_receipts VALUES(?,?,?)", (identifier, int(time.time()), serialized)).rowcount
            conn.execute("DELETE FROM test_receipts WHERE id NOT IN (SELECT id FROM test_receipts ORDER BY created_at DESC LIMIT 50)")
        return self._send(200, {"ok": True, "receipt_id": identifier, "duplicate": not bool(new), "feishu_sent": False, "mode": "test"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18780, type=int)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
