"""WeRead (微信读书) web QR login for the VPS subscription worker.

The login is performed by the server itself: the server asks WeRead for a login
uid, renders ``https://weread.qq.com/web/confirm?uid=...`` as a QR image, and
long-polls until the operator scans it with WeChat. Only the resulting WeRead
web cookies are stored (0600, outside the repository); nothing is read from a
desktop WeChat client or browser profile.

Protocol follows rachelos/we-mp-rss ``driver/weread_qr.py`` (commit 9fd4edb).
"""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import requests

from .weread import WEREAD, WeReadError, WeReadSession, browser_headers, private_json

QR_LIFETIME = 240


def _qr_png(text, path):
    import qrcode
    from qrcode.image.pure import PyPNGImage
    image = qrcode.make(text, image_factory=PyPNGImage, box_size=8, border=3)
    temporary = path.with_suffix(".next")
    with open(temporary, "wb") as file:
        image.save(file)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _succeeded(data):
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    if not (data.get("succeed") or inner.get("succeed")):
        return None
    pick = lambda *names: next((str(src[n]) for src in (data, inner) for n in names if src.get(n)), "")
    return {"vid": pick("webLoginVid", "vid", "userVid"),
            "access_token": pick("accessToken", "access_token"),
            "refresh_token": pick("refreshToken", "refresh_token"),
            "name": pick("name", "nickName")}


def _candidates(jar, login):
    """Cookie combinations observed to work after a fresh scan, most faithful first."""
    raw = dict(jar)
    if login["vid"] and not raw.get("wr_vid"):
        raw["wr_vid"] = login["vid"]
    out = [raw]
    rt = login["refresh_token"]
    for skey in (login["access_token"], rt):
        for wr_rt in (raw.get("wr_rt"), quote(rt, safe="") if rt else ""):
            if skey:
                variant = {**raw, "wr_skey": skey}
                if wr_rt:
                    variant["wr_rt"] = wr_rt
                if variant not in out:
                    out.append(variant)
    return out


def login(directory, wait_seconds=900, http=None):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    status_path = root / "login-status.json"

    def status(state, **details):
        private_json(status_path, {"state": state, "updated_at": int(time.time()), **details})
        print(json.dumps({"state": state, **details}, ensure_ascii=False), flush=True)

    deadline = time.monotonic() + wait_seconds
    status("starting")
    while time.monotonic() < deadline:
        client = http or requests.Session()
        client.trust_env = False
        client.headers.update(browser_headers())
        try:
            response = client.get(WEREAD + "/api/auth/getLoginUid", timeout=(10, 20))
            response.raise_for_status()
            data = response.json()
            uid = data.get("uid") or (data.get("data") or {}).get("uid")
            if not uid:
                status("unavailable", reason="getLoginUid returned no uid")
                return False
            confirm = WEREAD + "/web/confirm?uid=" + quote(str(uid), safe="")
            _qr_png(confirm, root / "weread-qr.png")
            issued = time.monotonic()
            status("ready", expires_in=QR_LIFETIME, qr="weread-qr.png")
            while time.monotonic() - issued < QR_LIFETIME and time.monotonic() < deadline:
                try:
                    answer = client.get(WEREAD + "/api/auth/getLoginInfo",
                                        params={"uid": uid, "otp": ""}, timeout=(10, 75))
                except requests.Timeout:
                    continue
                if answer.status_code != 200:
                    time.sleep(3)
                    continue
                try:
                    data = answer.json()
                except ValueError:
                    time.sleep(3)
                    continue
                result = _succeeded(data)
                if result is None:
                    code = str(data.get("logicCode", ""))
                    if code == "NEED_OTP":
                        status("otp_required")
                        return False
                    if code not in ("", "0", "LOGIN_TIMEOUT", "WAITING"):
                        status("waiting", logic_code=code[:40])
                    continue
                jar = {c.name: c.value for c in client.cookies if c.value}
                for cookies in _candidates(jar, result):
                    session = WeReadSession.from_cookies(root / "session.json", cookies, vid=result["vid"])
                    try:
                        session.verify(renew_on_failure=True)
                    except WeReadError:
                        continue
                    session.save(scanned_at=int(time.time()), name=result["name"][:40])
                    status("authorized", vid_suffix=result["vid"][-4:])
                    return True
                status("verification_failed", reason="scan confirmed but cookies were rejected")
                return False
            status("qr_expired_regenerating")
        except (requests.RequestException, ValueError) as exc:
            status("failed", error_type=type(exc).__name__)
            time.sleep(10)
        finally:
            if http is None:
                client.close()
    status("expired")
    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WeRead QR login (server side)")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--wait-seconds", type=int, default=900)
    args = parser.parse_args()
    raise SystemExit(0 if login(args.directory, args.wait_seconds) else 1)
