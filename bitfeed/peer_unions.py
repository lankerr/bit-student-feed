"""School union feeds collected through WeRead."""
import html
import re
from urllib.parse import parse_qs, urlsplit

from .domain import normalize_url
from .weread import WeReadError

PREFIX = "兄弟高校·"
SHARED_OFFICES = {"央音青年", "贸大研究生", "中石大新青年", "传媒团英"}
UNION_MENTION = re.compile(r"学生会|研究生会|校研会|校会|研会|研代会|学代会|学生代表大会|研究生代表大会")


def eligibility(body):
    if re.search(r"(?:活动对象|报名对象|参与对象)[：:\s]*[^。\n]{0,36}(?:协办学校|受邀高校|指定院校)", body):
        return "限原文所列学校，须核对北理是否在列"
    closed = re.search(r"(?<!不)(?:仅限|仅面向|只限|只面向)(?:本校|我校)|(?:谢绝|不面向)(?:校外|外校)(?:学生|同学|人员)", body)
    opened = re.search(r"(?:欢迎|接受)(?:外校|校外)(?:同学|学生)(?:报名|参加)|"
                       r"面向(?:全国|北京(?:市)?|首都)(?:各|所有)?高校(?:全体)?(?:在校)?(?:学生|大学生|研究生)(?:开放报名|征集|招募)|"
                       r"青年个人[：:][^。\n]{0,45}市内高校青年", body)
    if closed and opened:
        return "不同环节条件有别，外校资格待确认"
    if closed:
        return "限主办校"
    if opened and not re.search(r"不(?:再)?(?:欢迎|接受)|仅限|仅面向|定向邀请|限受邀|名额已满|报名已截止", body):
        return "原文明确面向外校，报名条件见原文"
    return "外校资格待确认"


def search_articles(session, account, now):
    name = account["name"][len(PREFIX):]
    payload = session.call("POST", "/web/wx_search_broker_proxy",
                           body={"query": name, "offset": 0, "searchcookies": ""})
    content = payload.get("content") or {}
    if content.get("ret") != 0 or not isinstance(content.get("data"), list):
        raise WeReadError("peer_search_unavailable")
    articles = {}
    for block in content["data"]:
        for result in block.get("items", []):
            url = html.unescape(result.get("doc_url") or "")
            parsed = urlsplit(url)
            query = parse_qs(parsed.query)
            if parsed.hostname != "mp.weixin.qq.com" or query.get("__biz") != [account["fakeid"]] or not query.get("mid"):
                continue
            date = result.get("date")
            title = _plain(result.get("title"))
            if not isinstance(date, int) or not now - 35 * 86400 <= date <= now or not title:
                continue
            url = normalize_url(url)
            articles[url] = dict(url=url, title=title, digest=_plain(result.get("desc"))[:500],
                                 published_at=date, origin="weread", source=account["name"], kind="wechat")
    return sorted(articles.values(), key=lambda item: -item["published_at"])


def _plain(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()
