# -*- coding: utf-8 -*-
"""站点与公众号搜索采集器（自 bit-feed 聚合器移植）

来源两类（均无需任何登录/授权）：
- boda : bit.edu.cn 站群静态列表页（20 个二级单位 32 个栏目）
- sogou: 搜狗微信文章搜索，用账号名/关键词等效订阅公众号（免扫码）

输出与 collect_public_feed.py 相同的 schema_version=1 包络，
kind 字段区分 site / mp-search，可直接并入同一 store 与发布流。
"""
import json
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests

try:
    import yaml
except ImportError:  # Actions 环境装了 pyyaml；本地缺省时给空配置
    yaml = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
DATE_RE = re.compile(r"(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})")
DATE_RE2 = re.compile(r"(20\d{2})\s+(\d{1,2})\.(\d{1,2})")
TRAIL_DATE = re.compile(r"\s*20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?\s*$")
LEAD_DATE = re.compile(r"^20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}日?\s*")


def _clean(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    t = TRAIL_DATE.sub("", t)
    return LEAD_DATE.sub("", t).strip()


def _http(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    if r.encoding in (None, "ISO-8859-1"):
        r.encoding = r.apparent_encoding or "utf-8"
    r.raise_for_status()
    return r.text


def parse_boda(url, allow_no_date=False):
    soup = BeautifulSoup(_http(url), "html.parser")
    items, seen = [], set()
    for a in soup.find_all("a", href=re.compile(r"\.s?html?$")):
        href = a.get("href", "")
        fname = href.rsplit("/", 1)[-1]
        if not fname or fname.startswith("index"):
            continue
        link = urljoin(url, href.split("?")[0])
        if link in seen:
            continue
        title = _clean(a.get("title") or a.get_text(" ", strip=True))
        if len(title) < 6:
            continue
        date, node = None, a
        for _ in range(4):
            if node is None:
                break
            m = DATE_RE.search(node.get_text(" ", strip=True)) or \
                DATE_RE2.search(node.get_text(" ", strip=True))
            if m:
                date = "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
                break
            node = node.parent
        if not date and not allow_no_date:
            continue
        seen.add(link)
        items.append({"title": title, "url": link, "date": date or ""})
    return items


def parse_sogou(query):
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get("https://weixin.sogou.com/", timeout=10)
    except Exception:
        pass
    time.sleep(1.5)
    r = s.get("https://weixin.sogou.com/weixin",
              params={"type": 2, "query": query}, timeout=20)
    h = r.text
    if "antispider" in h or "验证码" in h[:2000]:
        raise RuntimeError("sogou anti-spider")
    boxes = re.findall(r'<li id="sogou_vr_[^"]*".*?</li>', h, re.S)
    items = []
    for b in boxes:
        t = re.search(r'<h3>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not t:
            continue
        link = urljoin("https://weixin.sogou.com", t.group(1))
        title = _clean(re.sub(r"<[^>]+>", "", t.group(2)))
        if len(title) < 6:
            continue
        m = re.search(r"timeConvert\('(\d{10})'\)", b)
        date = time.strftime("%Y-%m-%d", time.localtime(int(m.group(1)))) if m else ""
        acc = re.search(r'class="all-time-y2">([^<]+)<', b)
        group = f"公众号·{acc.group(1).strip()}" if acc else f"公众号搜索·{query}"
        items.append({"title": title, "url": link, "date": date, "group": group,
                      "dedupe": f"{date}|{title}"})
    return items


def load_sources(cfg_path):
    p = Path(cfg_path)
    if not p.exists() or yaml is None:
        return []
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg.get("sources", [])


def collect(cfg_path, log=print):
    """返回 schema_version=1 包络"""
    sources = load_sources(cfg_path)
    articles, errors = [], []
    for s in sources:
        try:
            if s["type"] == "boda":
                got = parse_boda(s["url"], allow_no_date=s.get("allow_no_date", False))
                kind = "site"
            elif s["type"] == "sogou":
                got = parse_sogou(s["query"])
                kind = "mp-search"
                time.sleep(2)
            else:
                continue
            for it in got:
                it.setdefault("group", s.get("name", ""))
                d = it.get("date") or ""
                try:
                    ts = int(datetime.strptime(d, "%Y-%m-%d").replace(
                        tzinfo=timezone(timedelta(hours=8))).timestamp())
                except ValueError:
                    ts = 0
                articles.append({"title": it["title"], "url": it["url"],
                                 "group": it.get("group", ""), "date": d,
                                 "ts": ts, "kind": kind})
            log(f"[{s.get('name')}] {len(got)}")
        except Exception as ex:
            errors.append({"source": s.get("name"), "error": str(ex)[:120]})
            log(f"[{s.get('name')}] FAIL {ex}")
        time.sleep(0.4)
    # 按 标题+日期 去重（多关键词搜同一文章）
    uniq, seen = [], set()
    for a in articles:
        k = a["date"] + "|" + a["title"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return {"schema_version": 1,
            "collected_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "source_refreshed_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "articles": uniq, "errors": errors}
