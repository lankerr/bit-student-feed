import html
import json
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from shutil import copyfile
from xml.etree import ElementTree as ET

from .domain import TZ, now_iso, normalize_article, normalize_url

LABELS = {"academic":"学术讲座", "competition":"竞赛创新", "career":"升学就业",
          "volunteer":"实践志愿", "campus_service":"校园服务", "other":"其他"}


def public_data(store):
    articles = store.public_articles(days=90)
    articles.sort(key=lambda a: (a["classification"]["score"] if a["classification"] else -1,
                                 a["published_at"]), reverse=True)
    metadata = store.metadata()
    return {"schema_version":2, "generated_at":now_iso(),
        "collection": {k:v for k,v in metadata.items() if k in ("last_collection","source_refreshed_at")},
        "coverage":"仅包含本人设备已同步并选定的公众号公开推送，以及已配置的官网来源。",
        "classifier": {"configured":any(a["classification"] for a in articles),
                       "pending":sum(not a["classification"] for a in articles)},
        "schedule":{"site":"每日更新", "feishu":"周四、周日 12:00（北京时间），接收配置待完成"},
        "articles":articles}


def validate_public_data(data):
    if data.get("schema_version") != 2 or not isinstance(data.get("articles"), list):
        raise ValueError("unsupported public export")
    if len(data["articles"]) > 10000:
        raise ValueError("public export too large")
    # The build accepts only known public fields and source domains.
    allowed = {"id","title","url","source","published_at","kind","source_hash","model","date","classification"}
    for a in data["articles"]:
        if set(a)-allowed:
            raise ValueError("unexpected fields in public article")
        normalized = normalize_article(a)
        if normalized["id"] != a["id"]:
            raise ValueError("public article ID mismatch")
        a["url"] = normalize_url(a["url"])
    return data


def guidance(data):
    today = datetime.now(TZ).date().isoformat()
    active = [a for a in data["articles"] if a.get("classification") and
              a["classification"].get("verified_activity") and
              all(not a["classification"].get(k) or a["classification"][k] >= today for k in ("deadline","event_date"))]
    lines = [f"# 北理每日活动指引 · {today}", "", f"生成时间：{data['generated_at']}",
             "原文日期、报名资格及临时调整以主办方为准。", ""]
    if not active:
        lines += ["当前没有通过日期与来源核验的可参加活动。请先查看今日新消息和待核验事项。", ""]
    for a in active[:12]:
        c = a["classification"]
        lines += [f"## {a['title']}", f"来源：{a['source']}；截止：{c.get('deadline') or '未注明'}；活动日期：{c.get('event_date') or '未注明'}。",
                  c["summary"], f"建议：{c['next_step']}", f"原文：{a['url']}", ""]
    return "\n".join(lines), active


def rss(path, articles, title, base):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root,"channel")
    for k,v in (("title",title),("link",base),("description","北理公开通知索引；活动安排请核对原文。"),("language","zh-CN")):
        ET.SubElement(channel,k).text = v
    for a in articles:
        item = ET.SubElement(channel,"item")
        c = a.get("classification") or {}
        text = c.get("summary") or "新消息，等待学校模型分拣。请打开原文核实。"
        for k,v in (("title",a["title"]),("link",a["url"]),("guid",a["id"]),
                    ("pubDate",format_datetime(datetime.fromtimestamp(a["published_at"],TZ))),
                    ("description",text + " 来源：" + a["source"])):
            ET.SubElement(item,k).text = str(v)
    ET.ElementTree(root).write(path, encoding="utf-8",xml_declaration=True)


def build(data, destination, base_url="https://lankerr.github.io/bit-student-feed/"):
    data = validate_public_data(data)
    dest = Path(destination)
    dest.mkdir(parents=True,exist_ok=True)
    for name in ("index.html","app.js","style.css"):
        copyfile(Path(__file__).parent/"web"/name, dest/name)
    (dest/".nojekyll").write_text("",encoding="utf-8")
    (dest/"data.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
    report, active = guidance(data)
    (dest/"daily.md").write_text(report,encoding="utf-8")
    rss(dest/"feed.xml", sorted(data["articles"],key=lambda a:a["published_at"],reverse=True), "北理新消息",base_url)
    rss(dest/"activities.xml", active,"北理可参加活动",base_url)
    return {"articles":len(data["articles"]),"verified_activities":len(active),"output":str(dest)}
