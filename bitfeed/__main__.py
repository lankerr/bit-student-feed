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
        if args.command in ("import","daily"):
            raw=read_json(args.source)
            meta=raw.get("collection",{})
            result["collection"]=store.import_articles(raw["articles"],
                raw.get("collected_at",meta.get("last_collection")),
                raw.get("source_refreshed_at",meta.get("last_source_refresh")))
        if args.command in ("classify","daily"):
            try:
                result["classification"]=classify(store)
                store.record_job("classify",result["classification"]["state"],"completed")
            except Exception as exc:
                # Do not log request/response bodies, authorization headers or API keys.
                store.record_job("classify","failed",type(exc).__name__)
                result["classification"]={"state":"failed","error_type":type(exc).__name__}
                if args.command=="classify":
                    print(json.dumps(result,ensure_ascii=False)); return 1
        if args.command in ("daily","export"):
            result["export"]=build(public_data(store),args.output)
        if args.command=="daily" and result["classification"]["state"]=="failed":
            print(json.dumps(result,ensure_ascii=False)); return 1
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=="__main__":
    sys.exit(main())
