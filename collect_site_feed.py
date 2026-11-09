# -*- coding: utf-8 -*-
"""站点+公众号搜索采集入口：产出 site-feed.json 供发布流合并。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from bitfeed.site_sources import collect


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    env = collect(ROOT / "config" / "site_sources.yaml")
    out = ROOT / "site-data"
    out.mkdir(exist_ok=True)
    (out / "site-feed.json").write_text(
        json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "articles": len(env["articles"]),
                      "errors": env["errors"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
