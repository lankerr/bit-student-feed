"""Operations alerts: login expiry, quota back-off and stalled collection.

Each condition is sent at most every 12 hours while it lasts and a single
recovery notice is sent when it clears. Alerts never contain credentials.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import time

from . import feishu
from .domain import TZ

REPEAT = 12 * 3600
LABELS = {"weread_login": "微信读书登录", "weread_quota": "微信读书频率限制", "weread_stalled": "公众号订阅采集",
          "mp_login": "公众号后台登录", "mp_expiring": "公众号后台登录有效期"}


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _when(ts):
    return datetime.fromtimestamp(float(ts), TZ).strftime("%m月%d日 %H:%M") if ts else "未知"


def conditions(weread_root, wechat_root, now=None):
    now = now or time.time()
    found = {}
    link = os.getenv("BITFEED_OPS_URL", "")
    scan = f"[打开扫码页面]({link})" if link else "在服务器上运行 `python -m bitfeed weread-login`"
    scheduler = _read(Path(weread_root) / "scheduler.json")
    session = (scheduler.get("session") or {}).get("state")
    if session in ("auth_missing", "auth_expired"):
        found["weread_login"] = ("微信读书登录已失效", f"公众号文章订阅已暂停。请用微信扫码重新登录微信读书：{scan}。")
    limits = scheduler.get("limits") or {}
    if float(limits.get("cooldown_until", 0)) > now and int(limits.get("level", 0)) >= 3:
        found["weread_quota"] = ("微信读书频率限制", f"连续触发限流，订阅暂停至 {_when(limits['cooldown_until'])}，之后自动恢复，无需操作。")
    last_ok = max([float(r.get("last_ok") or 0) for r in (scheduler.get("accounts") or {}).values()] or [0])
    if session == "ok" and last_ok and now - last_ok > 24 * 3600:
        found["weread_stalled"] = ("公众号订阅停滞", f"最近一次成功采集在 {_when(last_ok)}，请检查服务器任务 bitfeed-wechat。")
    keep = _read(Path(wechat_root) / "keepalive.json")
    if keep.get("state") in ("auth_missing", "auth_expired"):
        found["mp_login"] = ("公众号后台登录已失效",
                             f"用于搜索和核验公众号的后台登录（格拉摩根的指挥官）已失效，文章订阅不受影响。重新扫码：{scan}。")
    elif keep.get("expires_at") and float(keep["expires_at"]) - now < 36 * 3600:
        found["mp_expiring"] = ("公众号后台登录即将过期",
                                f"自动续期未能延长有效期，将于 {_when(keep['expires_at'])} 过期。可提前扫码：{scan}。")
    return found


def run(store, weread_root, wechat_root, now=None, send=feishu.alert):
    now = now or time.time()
    active = conditions(weread_root, wechat_root, now)
    meta = store.metadata()
    sent = []
    for key, (title, text) in active.items():
        last = float(meta.get("alert:" + key) or 0)
        if now - last >= REPEAT:
            if send("北理消息站 · " + title, text) == "sent":
                store.set_meta("alert:" + key, now)
                sent.append(key)
    for key in [k[6:] for k in meta if k.startswith("alert:") and float(meta[k] or 0) > 0]:
        if key not in active:
            if send("北理消息站 · 已恢复", f"{LABELS.get(key, key)}已恢复正常。", template="green") == "sent":
                store.set_meta("alert:" + key, 0)
                sent.append(key + ":recovered")
    return {"active": sorted(active), "sent": sent}
