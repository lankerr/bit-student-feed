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
    if location.startswith(("http://", "https://")):
        response = requests.get(location, timeout=(10, 60), allow_redirects=False)
        response.raise_for_status()
        if len(response.content) > 12000000:
            raise ValueError("source export too large")
        return response.json()
    return json.loads(Path(location).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description="BIT student public feed")
    parser.add_argument("--data", default=os.getenv("BITFEED_DATA", "data"))
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("import")
    ingest.add_argument("source")
    sub.add_parser("classify")
    server = sub.add_parser("server-daily")
    server.add_argument("--output", default="dist")
    server.add_argument("--max-batches", default=3, type=int)
    server.add_argument("--career-pages", default=100, type=int)
    server.add_argument("--web-pages", default=3, type=int)
    server.add_argument("--with-preview", action="store_true")
    daily = sub.add_parser("daily")
    daily.add_argument("--source", required=True)
    daily.add_argument("--output", default="dist")
    export = sub.add_parser("export")
    export.add_argument("--output", default="dist")
    refresh = sub.add_parser("refresh", help="classify new items and re-export")
    refresh.add_argument("--output", default="dist")
    refresh.add_argument("--max-batches", default=3, type=int)
    refresh.add_argument("--with-preview", action="store_true")
    site = sub.add_parser("build-site")
    site.add_argument("--source", required=True)
    site.add_argument("--output", default="dist")
    sync = sub.add_parser("wechat-sync", help="one paced WeRead subscription run")
    sync.add_argument("--calls", default=3, type=int)
    sync.add_argument("--bodies", default=2, type=int)
    sync.add_argument("--follows", default=2, type=int)
    sub.add_parser("weread-login", help="server-side WeRead QR login")
    sub.add_parser("session-keepalive", help="extend WeRead and public-platform logins")
    resolve = sub.add_parser("wechat-resolve", help="resolve official accounts via public-platform search")
    resolve.add_argument("--limit", default=40, type=int)
    sub.add_parser("alerts", help="send login/collection alerts to Feishu")
    digest = sub.add_parser("feishu-digest", help="biweekly digest to Feishu with a Word file")
    digest.add_argument("--mode", choices=("test", "production"))
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--force", action="store_true")
    preview = sub.add_parser("weekly-preview", help="generate the biweekly Friday-to-Friday preview without sending")
    preview.add_argument("--no-model", action="store_true")
    preview.add_argument("--sync-github-config", action="store_true")
    preview.add_argument("--output", default="/var/lib/bitfeed-pipeline/site")
    weekly = sub.add_parser("weekly-send", help="generate and send biweekly Tuesday edition, with deduplication")
    weekly.add_argument("--no-model", action="store_true")
    weekly.add_argument("--sync-github-config", action="store_true")
    weekly.add_argument("--force", action="store_true")
    weekly.add_argument("--output", default="/var/lib/bitfeed-pipeline/site")
    weekly.add_argument("--mode", choices=("test", "production"))
    gh_push = sub.add_parser("github-push", help="validate push.toml, build Word preview, or send from GitHub Actions")
    gh_push.add_argument("--config", default=None)
    gh_push.add_argument("--source", default=os.getenv("BITFEED_SOURCE_URL", ""))
    gh_push.add_argument("--action", choices=("preview_only", "send_feishu", "auto_schedule"), default="preview_only")
    gh_push.add_argument("--start-date", default="")
    gh_push.add_argument("--end-date", default="")
    gh_push.add_argument("--output", default="dist-pioneer")
    gh_push.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    if args.command == "github-push":
        from .github_ops import run_github_push
        outcome = run_github_push(
            config_path=args.config,
            source_url=args.source,
            action=args.action,
            start_date=args.start_date,
            end_date=args.end_date,
            output_dir=args.output,
            webhook=os.getenv("FEISHU_WEBHOOK", ""),
            signing_secret=os.getenv("FEISHU_SIGNING_SECRET", ""),
            check_only=args.check_only,
        )
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome.get("ok") else 1
    if args.command == "build-site":
        print(json.dumps(build(read_json(args.source), args.output), ensure_ascii=False))
        return 0
    if args.command == "weread-login":
        from .weread_auth import login
        return 0 if login(WEREAD_DIR) else 1
    if args.command == "session-keepalive":
        from .mp_session import keepalive as mp_keepalive
        from .subscribe import ping_session
        result = {"mp": mp_keepalive(WECHAT_DIR), "weread": ping_session(WEREAD_DIR)}
        print(json.dumps(result, ensure_ascii=False))
        ok = lambda s: s.get("state") in ("ok", "busy", "cooldown", "daily_cap")
        return 0 if ok(result["mp"]) and ok(result["weread"]) else 1
    if args.command == "wechat-resolve":
        from .discover import resolve as run_resolve
        print(
            json.dumps(
                run_resolve(Path(__file__).parent.parent / "config" / "wechat-discovery.json", WECHAT_DIR, args.limit),
                ensure_ascii=False,
            )
        )
        return 0

    store = Store(args.data)
    result = {}
    if args.command == "weekly-send":
        from . import digest as weekly_digest, feishu
        from .push_config import load_config, schedule_due
        cfg, cfg_source = load_config(sync_remote=args.sync_github_config)
        sched = schedule_due(cfg)
        mode = args.mode or os.getenv("BITFEED_DIGEST_MODE", "test")
        if not args.force and not sched["due"]:
            out = {"state": "skipped_schedule", "reason": sched["reason"], "config_source": cfg_source}
            print(json.dumps(out, ensure_ascii=False))
            return 0
        if not args.force:
            prior = feishu.week_receipt(store, mode, sched["cycle_key"])
            if prior and prior != "rejected":
                out = {"state": prior, "duplicate": True, "week_start": sched["cycle_key"], "config_source": cfg_source}
                print(json.dumps(out, ensure_ascii=False))
                return 0
        report = weekly_digest.build(store, use_model=not args.no_model, cfg=cfg)
        preview_result = weekly_digest.write_preview(
            store, report=report, directory=Path(args.output) / "weekly", cfg=cfg
        )
        build(public_data(store), args.output)
        delivery = weekly_digest.send(
            store, args.mode, use_model=False, report=report, cfg=cfg, force=args.force, enforce_schedule=True
        )
        result = {"preview": preview_result, "delivery": delivery, "config_source": cfg_source}
        store.set_meta("weekly_delivery", json.dumps(delivery, ensure_ascii=False))
        build(public_data(store), args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if delivery["state"] in ("sent", "skipped_schedule") or delivery.get("duplicate") else 1
    if args.command == "weekly-preview":
        from .digest import write_preview
        from .push_config import load_config
        cfg, cfg_source = load_config(sync_remote=args.sync_github_config)
        result = write_preview(store, use_model=not args.no_model, directory=Path(args.output) / "weekly", cfg=cfg)
        result["config_source"] = cfg_source
        build(public_data(store), args.output)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "wechat-sync":
        from .subscribe import Worker
        print(json.dumps(Worker(store, WEREAD_DIR).run(args.calls, args.bodies, args.follows), ensure_ascii=False))
        return 0
    if args.command == "alerts":
        from .alerts import run as run_alerts
        print(json.dumps(run_alerts(store, WEREAD_DIR, WECHAT_DIR), ensure_ascii=False))
        return 0
    if args.command == "feishu-digest":
        from . import digest as feishu_digest
        if args.dry_run:
            built = feishu_digest.build(store, use_model=False)
            print(
                json.dumps(
                    {
                        "title": built["title"],
                        "period": built["period"],
                        "groups": {k: len(v) for k, v in built["groups"].items()},
                    },
                    ensure_ascii=False,
                )
            )
            print(feishu_digest.card(built, "(Word 链接)"))
            return 0
        result = feishu_digest.send(store, args.mode, force=args.force)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["state"] in ("sent", "skipped_schedule") else 1
    if args.command == "server-daily":
        from .career import collect as career_collect
        from .websites import collect as website_collect
        from .albums import collect as album_collect
        audits = []
        result["collection"] = {}
        # Official-account articles come from the WeRead worker (wechat-sync); the
        # public-platform list answers 200013 for every third party since 2026-07-30.
        for name, raw in (
            ("career", career_collect(max_pages=args.career_pages)),
            ("websites", website_collect(max_pages=args.web_pages)),
            ("albums", album_collect()),
        ):
            if any(a.get("pages", 0) > 0 for a in raw["sources"]):
                result["collection"][name] = store.import_articles(
                    raw["articles"], raw["collected_at"], raw["collected_at"]
                )
            else:
                result["collection"][name] = {"imported": 0, "state": "no_successful_source"}
            audits.extend(raw["sources"])
        store.set_source_audit(audits)
        result["source_states"] = {a.get("id", a.get("name")): a["state"] for a in audits}
    if args.command in ("import", "daily"):
        raw = read_json(args.source)
        meta = raw.get("collection", {})
        result["collection"] = store.import_articles(
            raw["articles"],
            raw.get("collected_at", meta.get("last_collection")),
            raw.get("source_refreshed_at", meta.get("last_source_refresh")),
        )
    if args.command in ("classify", "daily", "server-daily", "refresh"):
        try:
            result["classification"] = classify(store, max_batches=getattr(args, "max_batches", None))
            store.record_job(
                "classify",
                result["classification"]["state"],
                result["classification"].get("reason") or "completed",
            )
        except Exception as exc:
            # Do not log request/response bodies, authorization headers or API keys.
            store.record_job("classify", "failed", type(exc).__name__)
            result["classification"] = {
                "state": "failed",
                "error_type": type(exc).__name__,
                "detail": str(exc)[:180] if isinstance(exc, RuntimeError) else "details omitted",
            }
            if args.command == "classify":
                print(json.dumps(result, ensure_ascii=False))
                return 1
    if args.command in ("server-daily", "refresh") and getattr(args, "with_preview", False):
        from .digest import write_preview
        from .push_config import load_config
        cfg, _ = load_config(sync_remote=True)
        result["preview"] = write_preview(store, use_model=False, directory=Path(args.output) / "weekly", cfg=cfg)
    if args.command in ("daily", "server-daily", "export", "refresh"):
        result["export"] = build(public_data(store), args.output)
    if args.command in ("daily", "server-daily") and result["classification"]["state"] == "failed":
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
