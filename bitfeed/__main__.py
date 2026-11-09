import argparse
import json
import os
from pathlib import Path
import sys

import requests

from .classifier import classify
from .export import build, public_data
from .store import Store


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
    server.add_argument("--career-pages",default=100,type=int)
    server.add_argument("--web-pages",default=3,type=int)
    daily=sub.add_parser("daily")
    daily.add_argument("--source",required=True)
    daily.add_argument("--output",default="dist")
    export=sub.add_parser("export")
    export.add_argument("--output",default="dist")
    site=sub.add_parser("build-site")
    site.add_argument("--source",required=True)
    site.add_argument("--output",default="dist")
    args=parser.parse_args()
    if args.command=="build-site":
        result=build(read_json(args.source),args.output)
    else:
        store=Store(args.data)
        result={}
        if args.command=="server-daily":
            from .career import collect as career_collect
            from .websites import collect as website_collect
            from .albums import collect as album_collect
            from .wechat_platform import collect as platform_collect
            audits=[]
            result["collection"]={}
            for name,raw in (("career",career_collect(max_pages=args.career_pages)),
                             ("websites",website_collect(max_pages=args.web_pages)),
                             ("albums",album_collect()),
                             ("wechat",platform_collect(Path("config/wechat-accounts.json"),
                                 os.getenv("BITFEED_WECHAT_AUTH","/var/lib/bitfeed-wechat/session.json"),max_pages=2))):
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
        if args.command in ("classify","daily","server-daily"):
            try:
                result["classification"]=classify(store)
                store.record_job("classify",result["classification"]["state"],"completed")
            except Exception as exc:
                # Do not log request/response bodies, authorization headers or API keys.
                store.record_job("classify","failed",type(exc).__name__)
                result["classification"]={"state":"failed","error_type":type(exc).__name__,
                    "detail":str(exc)[:180] if isinstance(exc,RuntimeError) else "details omitted"}
                if args.command=="classify":
                    print(json.dumps(result,ensure_ascii=False)); return 1
        if args.command in ("daily","server-daily","export"):
            result["export"]=build(public_data(store),args.output)
        if args.command in ("daily","server-daily") and result["classification"]["state"]=="failed":
            print(json.dumps(result,ensure_ascii=False)); return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    sys.exit(main())
