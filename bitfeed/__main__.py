import argparse
import json
import os
from pathlib import Path
import sys

import requests

from .classifier import classify
from .export import build, public_data
from .store import Store

WEREAD_DIR = os.getenv("BITFEED_WEREAD_DIR", "/var/lib/bitfeed-weread")
WECHAT_DIR = os.getenv("BITFEED_WECHAT_DIR", "/var/lib/bitfeed-wechat")


def read_json(location):
    if location.startswith(('http://','https://')):
        response = requests.get(location,timeout=(10,60),allow_redirects=False)
        response.raise_for_status()
        if len(response.content)>12000000:
            raise ValueError("source export too large")
        return response.json()
    return json.loads(Path(location).read_text(encoding="utf-8-sig"))


def main():
    parser=argparse.ArgumentParser(description="BIT student public feed")
    parser.add_argument("--data",default=os.getenv("BITFEED_DATA","data"))
    sub=parser.add_subparsers(dest="command",required=True)
    ingest=sub.add_parser("import")
    ingest.add_argument("source")
    sub.add_parser("classify")
    server=sub.add_parser("server-daily")
    server.add_argument("--output",default="dist")
    server.add_argument("--max-batches",default=3,type=int)
    server.add_argument("--career-pages",default=100,type=int)
    server.add_argument("--web-pages",default=3,type=int)
    daily=sub.add_parser("daily")
    daily.add_argument("--source",required=True)
    daily.add_argument("--output",default="dist")
    export=sub.add_parser("export")
    export.add_argument("--output",default="dist")
    refresh=sub.add_parser("refresh",help="classify new items and re-export")
    refresh.add_argument("--output",default="dist")
    refresh.add_argument("--max-batches",default=3,type=int)
    site=sub.add_parser("build-site")
    site.add_argument("--source",required=True)
    site.add_argument("--output",default="dist")
    sync=sub.add_parser("wechat-sync",help="one paced WeRead subscription run")
    sync.add_argument("--calls",default=3,type=int)
    sync.add_argument("--bodies",default=2,type=int)
    sync.add_argument("--follows",default=2,type=int)
    sub.add_parser("weread-login",help="server-side WeRead QR login")
    sub.add_parser("session-keepalive",help="extend WeRead and public-platform logins")
    resolve=sub.add_parser("wechat-resolve",help="resolve official accounts via public-platform search")
    resolve.add_argument("--limit",default=40,type=int)
    sub.add_parser("alerts",help="send login/collection alerts to Feishu")
    digest=sub.add_parser("feishu-digest",help="Thu/Sun digest to Feishu")
    digest.add_argument("--mode",choices=("test","production"))
    digest.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    if args.command=="build-site":
        print(json.dumps(build(read_json(args.source),args.output),ensure_ascii=False)); return 0
    if args.command=="weread-login":
        from .weread_auth import login
        return 0 if login(WEREAD_DIR) else 1
    if args.command=="session-keepalive":
        from .mp_session import keepalive as mp_keepalive
        from .subscribe import ping_session
        result={"mp":mp_keepalive(WECHAT_DIR),"weread":ping_session(WEREAD_DIR)}
        print(json.dumps(result,ensure_ascii=False))
        ok=lambda s:s.get("state") in ("ok","busy","cooldown","daily_cap")
        return 0 if ok(result["mp"]) and ok(result["weread"]) else 1
    if args.command=="wechat-resolve":
        from .discover import resolve as run_resolve
        print(json.dumps(run_resolve(Path(__file__).parent.parent/"config"/"wechat-discovery.json",WECHAT_DIR,args.limit),ensure_ascii=False))
        return 0
    store=Store(args.data)
    result={}
    if args.command=="wechat-sync":
        from .subscribe import Worker
        print(json.dumps(Worker(store,WEREAD_DIR).run(args.calls,args.bodies,args.follows),ensure_ascii=False)); return 0
    if args.command=="alerts":
        from .alerts import run as run_alerts
        print(json.dumps(run_alerts(store,WEREAD_DIR,WECHAT_DIR),ensure_ascii=False)); return 0
    if args.command=="feishu-digest":
        from . import digest as feishu_digest
        if args.dry_run:
            print(json.dumps(feishu_digest.build(store),ensure_ascii=False,indent=1)); return 0
        result=feishu_digest.send(store,args.mode)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result["state"]=="sent" or result.get("duplicate") else 1
    if args.command=="server-daily":
        from .career import collect as career_collect
        from .websites import collect as website_collect
        from .albums import collect as album_collect
        audits=[]
        result["collection"]={}
        # Official-account articles come from the WeRead worker (wechat-sync); the
        # public-platform list answers 200013 for every third party since 2026-07-30.
        for name,raw in (("career",career_collect(max_pages=args.career_pages)),
                         ("websites",website_collect(max_pages=args.web_pages)),
                         ("albums",album_collect())):
            if any(a.get("pages",0)>0 for a in raw["sources"]):
                result["collection"][name]=store.import_articles(raw["articles"],raw["collected_at"],raw["collected_at"])
            else:
                result["collection"][name]={"imported":0,"state":"no_successful_source"}
            audits.extend(raw["sources"])
        store.set_source_audit(audits)
        result["source_states"]={a.get("id",a.get("name")):a["state"] for a in audits}
    if args.command in ("import","daily"):
        raw=read_json(args.source)
        meta=raw.get("collection",{})
        result["collection"]=store.import_articles(raw["articles"],
            raw.get("collected_at",meta.get("last_collection")),
            raw.get("source_refreshed_at",meta.get("last_source_refresh")))
    if args.command in ("classify","daily","server-daily","refresh"):
        try:
            result["classification"]=classify(store,max_batches=getattr(args,"max_batches",None))
            store.record_job("classify",result["classification"]["state"],
                             result["classification"].get("reason") or "completed")
        except Exception as exc:
            # Do not log request/response bodies, authorization headers or API keys.
            store.record_job("classify","failed",type(exc).__name__)
            result["classification"]={"state":"failed","error_type":type(exc).__name__,
                "detail":str(exc)[:180] if isinstance(exc,RuntimeError) else "details omitted"}
            if args.command=="classify":
                print(json.dumps(result,ensure_ascii=False)); return 1
    if args.command in ("daily","server-daily","export","refresh"):
        result["export"]=build(public_data(store),args.output)
    if args.command in ("daily","server-daily") and result["classification"]["state"]=="failed":
        print(json.dumps(result,ensure_ascii=False)); return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    sys.exit(main())
