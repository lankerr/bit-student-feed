"""Resolve official account names to fakeids through the public-platform search."""
import json
from pathlib import Path
import random
import time

from .mp_session import MpError, MpSession
from .weread import private_json

SCHOOL = ("北京理工", "北理", "BIT")


def accept(row, keys):
    name = str(row.get("nickname") or "")
    return (row.get("verify_status") == 2 and any(s in name for s in SCHOOL)
            and any(k in name for k in keys) and "珠海" not in name and "北理珠" not in name)


def resolve(config_path, wechat_dir, limit=40, gap=(80, 120), sleep=time.sleep):
    root = Path(wechat_dir)
    out_path = root / "resolved.json"
    try:
        done = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        done = {}
    queries = json.loads(Path(config_path).read_text(encoding="utf-8"))["queries"]
    session = MpSession(root / "session.json")
    searched = 0
    try:
        for spec in queries:
            if spec["query"] in done or searched >= limit:
                continue
            if searched:
                sleep(random.uniform(*gap))
            try:
                rows = session.search(spec["query"])
            except MpError as exc:
                done[spec["query"]] = {"at": int(time.time()), "error": exc.code}
                private_json(out_path, done)
                if exc.state in ("rate_limited", "auth_expired", "blocked"):
                    return {"searched": searched, "stopped": exc.state}
                continue
            searched += 1
            accepted = [r for r in rows if accept(r, spec["keys"])]
            done[spec["query"]] = {"at": int(time.time()), "unit": spec["unit"], "tier": spec["tier"],
                                   "accepted": accepted, "others": [r for r in rows if r not in accepted]}
            private_json(out_path, done)
    finally:
        session.close()
    return {"searched": searched, "stopped": None}
