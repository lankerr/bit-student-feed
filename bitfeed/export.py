import html
import json
import os
from datetime import datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from shutil import copyfile
from xml.etree import ElementTree as ET

from .domain import TZ, now_iso, normalize_article, normalize_url
from .activity_time import activity_expired

LABELS = {"academic":"学术讲座", "competition":"竞赛创新", "career":"升学就业",
          "volunteer":"实践志愿", "campus_service":"校园服务", "other":"其他"}


def atomic_text(path, value):
    stage=path.with_name('.'+path.name+'.next')
    with stage.open('w',encoding='utf-8') as file:
        file.write(value)
        file.flush()
        os.fsync(file.fileno())
    os.replace(stage,path)


def public_data(store):
    articles = store.public_articles(days=90)
    articles.sort(key=lambda a: (a["classification"]["score"] if a["classification"] else -1,
                                 a["published_at"]), reverse=True)
    metadata = store.metadata()
    candidate_path = Path(__file__).parent.parent/"config"/"wechat-candidates.json"
    registry = json.loads(candidate_path.read_text(encoding="utf-8")) if candidate_path.exists() else {}
    candidates = registry.get("accounts",registry.get("candidates",[]))
    status = json.loads(metadata.get("weread_status") or "{}")
    return {"schema_version":2, "generated_at":now_iso(),
        "collection": {k:v for k,v in metadata.items() if k in ("last_collection","source_refreshed_at")},
        "coverage":"VPS 采集的就业网、学校与学院官网栏目，以及经微信读书订阅的公众号（每次只能看到各号最新一篇）。",
        "sources":json.loads(metadata.get("source_audit","[]"))+json.loads(metadata.get("weread_audit","[]")),
        "wechat_registry":{"historical_candidates":len(candidates),"current_complete":False,
                           "subscribed":status.get("accounts",0),"healthy":status.get("healthy",0),
                           "session":status.get("session"),"checked_at":status.get("checked_at"),
                           "notice":"公众号经微信读书订阅，接口每次只返回各号最新一篇；多图文推送的第二篇及以后可能遗漏。"},
        "classifier": {"configured":any(a["classification"] for a in articles),
                       "pending":sum(not a["classification"] for a in articles)},
        "schedule":{"site":"每日更新", "wechat":"每 5 分钟分批轮询，各号 1～8 小时一次",
                    "feishu":"每周日 12:00（北京时间），附 Word 完整版"},
        "articles":articles}


def validate_public_data(data):
    if set(data)-{"schema_version","generated_at","collection","coverage","sources","wechat_registry",
                  "classifier","schedule","articles"}:
        raise ValueError("unexpected top-level fields in public export")
    if data.get("schema_version") != 2 or not isinstance(data.get("articles"), list):
        raise ValueError("unsupported public export")
    if len(data["articles"]) > 10000:
        raise ValueError("public export too large")
    # The build accepts only known public fields and source domains.
    allowed = {"id","title","url","source","published_at","kind","source_hash","model","date","classification",
               "origin","publication_status","body_status","source_expiry"}
    for a in data["articles"]:
        if set(a)-allowed:
            raise ValueError("unexpected fields in public article")
        normalized = normalize_article(a)
        if normalized["id"] != a["id"]:
            raise ValueError("public article ID mismatch")
        a["url"] = normalize_url(a["url"])
    return data


def _marked_days(article):
    classification = article.get("classification") or {}
    days = [classification.get("deadline"), classification.get("event_date")]
    expiry = int(article.get("source_expiry") or 0)
    if expiry > 0:
        days.append(datetime.fromtimestamp(expiry, TZ).date().isoformat())
    return [day for day in days if day]


def _soonest(article):
    days = _marked_days(article)
    return min(days) if days else "9999-12-31"


def _chinese_day(day):
    if not day or len(str(day)) < 10:
        return "未注明"
    day = str(day)
    return f"{day[:4]}年{int(day[5:7])}月{int(day[8:10])}日"


def _guide_block(article):
    classification = article["classification"]
    when = _chinese_day(_soonest(article)) if _marked_days(article) else "未注明"
    return [f"## {article['title']}",
            f"来源：{article['source']}；最近节点：{when}。",
            classification["summary"], f"建议：{classification['next_step']}", f"原文：{article['url']}", ""]


def guidance(data):
    today = datetime.now(TZ).date()
    today_iso = today.isoformat()
    horizon = (today + timedelta(days=14)).isoformat()
    active = []
    for article in data["articles"]:
        classification = article.get("classification") or {}
        if not classification.get("verified_activity"):
            continue
        if activity_expired(article):
            continue
        days = _marked_days(article)
        if any(day < today_iso for day in days):
            continue
        active.append(article)
    active.sort(key=lambda article: (_soonest(article), article.get("title") or ""))
    near = [article for article in active if _soonest(article) <= horizon]
    later = [article for article in active if _soonest(article) > horizon]
    lines = [f"# 北理每日活动指引 · {today_iso}", "", f"生成时间：{data['generated_at']}",
             "原文日期、报名资格及临时调整以主办方为准。临近的安排排在前面。", ""]
    if not active:
        lines += ["当前没有通过日期与来源核验的可参加活动。请先查看今日新消息和待核验事项。", ""]
    else:
        lines += ["## 两周内", ""]
        if near:
            for article in near:
                lines += _guide_block(article)
        else:
            lines += ["未来 14 天没有已核验、仍开放的活动。", ""]
        if later:
            lines += ["## 更晚仍开放", ""]
            for article in later[:12]:
                lines += _guide_block(article)
            if len(later) > 12:
                lines += [f"其余 {len(later) - 12} 条仍开放的安排见网页。", ""]
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
        unknown=a.get("publication_status")=="unknown"
        if unknown:text="首次收录，原文发布日期未核验。"+text
        fields=[("title",a["title"]),("link",a["url"]),("guid",a["id"]),
                ("description",text + " 来源：" + a["source"])]
        if not unknown:fields.append(("pubDate",format_datetime(datetime.fromtimestamp(a["published_at"],TZ))))
        for k,v in fields:
            ET.SubElement(item,k).text = str(v)
    atomic_text(path,ET.tostring(root,encoding="unicode",xml_declaration=True))


def build(data, destination, base_url="https://lankerr.github.io/bit-student-feed/"):
    data = validate_public_data(data)
    dest = Path(destination)
    dest.mkdir(parents=True,exist_ok=True)
    for name in ("index.html","app.js","style.css"):
        copyfile(Path(__file__).parent/"web"/name, dest/name)
    (dest/".nojekyll").write_text("",encoding="utf-8")
    atomic_text(dest/"data.json",json.dumps(data,ensure_ascii=False,indent=2))
    atomic_text(dest/"sources.json",json.dumps(data.get("sources",[]),ensure_ascii=False,indent=2))
    candidate_path=Path(__file__).parent.parent/"config"/"wechat-candidates.json"
    if candidate_path.exists():copyfile(candidate_path,dest/"wechat-candidates.json")
    report, active = guidance(data)
    atomic_text(dest/"daily.md",report)
    rss(dest/"feed.xml", sorted(data["articles"],key=lambda a:(a.get("publication_status")=="unknown",-a["published_at"])), "北理新消息",base_url)
    rss(dest/"activities.xml", active,"北理可参加活动",base_url)
    return {"articles":len(data["articles"]),"verified_activities":len(active),"output":str(dest)}
