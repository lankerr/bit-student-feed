"""Read the public ``mp.weixin.qq.com`` article page: body text and publish time.

Since 2026-10 the page no longer renders ``#js_content`` for anonymous readers;
the text lives in the ``content_noencode`` JS string (we-mp-rss observation).
Verification pages are reported as ``blocked`` so the caller can back off.
"""
import html
import re

import requests

from .weread import USER_AGENT

BLOCKED = re.compile(r"环境异常|完成验证后即可继续访问|wappoc_appmsgcaptcha|secitptpage")
GONE = re.compile(r"该内容已被发布者删除|此内容因违规无法查看|内容已删除|该公众号已迁移|此帐号已被屏蔽|此帐号已自主注销")


class PageError(RuntimeError):
    def __init__(self, state):
        super().__init__(state)
        self.state = state


def _js_string(text, name):
    match = re.search(name + r"\s*:\s*'((?:\\.|[^'\\])*)'", text) or re.search(
        r"var\s+" + name + r"\s*=\s*'((?:\\.|[^'\\])*)'", text) or re.search(
        r"var\s+" + name + r'\s*=\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return ""
    raw = match.group(1)
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), raw)
    raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), raw)
    return raw.replace("\\n", "\n").replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")


def _plain(fragment):
    fragment = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>|</p>|</section>|</div>|</h\d>|</li>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(fragment).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def parse(text):
    if BLOCKED.search(text[:20000]):
        raise PageError("blocked")
    if GONE.search(text[:20000]) and "content_noencode" not in text and "js_content" not in text:
        raise PageError("deleted")
    body = _js_string(text, "content_noencode")
    if not body:
        match = re.search(r'(?is)<div[^>]+id="js_content"[^>]*>(.*?)</div>\s*<script', text)
        body = _plain(match.group(1)) if match else ""
    else:
        body = _plain(body.replace("\n", "<br>"))
    published = None
    for pattern in (r'var\s+ct\s*=\s*"(\d{9,11})"', r"create_time\s*[:=]\s*['\"]?(\d{9,11})", r'"publish_time"\s*:\s*(\d{9,11})'):
        match = re.search(pattern, text)
        if match:
            published = int(match.group(1))
            break
    title = _js_string(text, "msg_title") or _js_string(text, "title")
    link = _js_string(text, "msg_link")
    if not body and not title:
        raise PageError("unrecognized")
    return {"body": body[:18000], "published_at": published, "title": html.unescape(title).strip()[:500],
            "canonical": link.replace("http://", "https://", 1) if link.startswith("http://mp.weixin.qq.com/") else link}


def fetch(url, session=None, timeout=(10, 25)):
    http = session or requests.Session()
    try:
        response = http.get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
                                          "Accept-Language": "zh-CN,zh;q=0.9"},
                            timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        raise PageError("failed") from None
    finally:
        if session is None:
            http.close()
    if "captcha" in response.url or response.status_code in (403, 429):
        raise PageError("blocked")
    if response.status_code == 404:
        raise PageError("deleted")
    if response.status_code != 200:
        raise PageError("failed")
    response.encoding = "utf-8"
    return parse(response.text)
