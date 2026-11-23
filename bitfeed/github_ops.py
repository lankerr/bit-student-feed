"""Stateless GitHub Actions helper for config validation, Word preview, and Feishu push."""
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from . import digest, feishu
from .domain import TZ
from .planning import collect_pool
from .push_config import describe_schedule, load_config, schedule_due, upcoming_runs


def _derive_pool_url(source_url: str) -> str:
    parts = urlsplit(source_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("BITFEED_SOURCE_URL 格式无效")
    base = f"{parts.scheme}://{parts.netloc}/"
    return urljoin(base, "weekly/planning-pool.json")


def _derive_docx_url(source_url: str) -> str:
    parts = urlsplit(source_url.strip())
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}/weekly/bit-preview-latest.docx"
    return "https://lankerr.github.io/bit-student-feed/"


def fetch_pool(source_url: str, now: float, session=None) -> Tuple[List[Dict], str]:
    """Fetch pre-verified planning pool from VPS, falling back to site-data.json."""
    http = session or requests.Session()
    if source_url.startswith(("http://", "https://")):
        pool_url = _derive_pool_url(source_url)
        try:
            resp = http.get(pool_url, timeout=(10, 30), allow_redirects=False)
            if resp.status_code == 200 and len(resp.content) <= 12_000_000:
                payload = resp.json()
                if isinstance(payload.get("entries"), list):
                    return payload["entries"], "planning_pool"
        except (requests.RequestException, ValueError):
            pass
        resp = http.get(source_url, timeout=(10, 60), allow_redirects=False)
        resp.raise_for_status()
        if len(resp.content) > 12_000_000:
            raise ValueError("公开数据源体积超出上限")
        data = resp.json()
    else:
        raw = json.loads(Path(source_url).read_text(encoding="utf-8-sig"))
        if isinstance(raw.get("entries"), list):
            return raw["entries"], "local_pool"
        data = raw
    return collect_pool(data.get("articles", []), now), "site_data_fallback"


def format_schedule_markdown(cfg: dict, now: Optional[float] = None) -> str:
    lines = [
        "## 📅 当前推送排期配置（`config/push.toml`）",
        "",
        f"- **当前规则**：{describe_schedule(cfg)}",
        f"- **启用状态**：{'✅ 已开启自动推送' if cfg['enabled'] else '⏸️ 已暂停自动推送'}",
        "",
    ]
    runs = upcoming_runs(cfg, now=now, count=4)
    if runs:
        lines += [
            "### 未来 4 期自动推送时间表",
            "",
            "| 期次 | 群机器人推送时间（北京时间） | 统计活动范围（本周五 → 下下周五） |",
            "| --- | --- | --- |",
        ]
        for idx, r in enumerate(runs, 1):
            lines.append(
                f"| 第 {idx} 期 | {r['push_date']}（{r['push_weekday']}）{r['push_time']} "
                f"| {r['window_start']}（{r['window_start_weekday']}）至 {r['window_end']}（{r['window_end_weekday']}） |"
            )
        lines.append("")
    return "\n".join(lines)


def _append_step_summary(markdown: str) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


def run_github_push(
    *,
    config_path=None,
    source_url="",
    action="preview_only",
    start_date="",
    end_date="",
    output_dir="dist-pioneer",
    webhook="",
    signing_secret="",
    check_only=False,
    now=None,
    session=None,
) -> dict:
    now_ts = datetime.now(TZ).timestamp() if now is None else float(now)
    cfg, cfg_source = load_config(config_path)
    schedule_md = format_schedule_markdown(cfg, now=now_ts)

    if check_only:
        _append_step_summary("✅ **`config/push.toml` 配置格式检查通过！**\n\n" + schedule_md)
        return {"ok": True, "mode": "check_only", "schedule": describe_schedule(cfg), "config_source": cfg_source}

    if action == "auto_schedule":
        sched = schedule_due(cfg, now=now_ts, check_clock=False)
        if not sched["due"]:
            msg = (
                f"⏭️ **今日无需自动推送**（原因：`{sched['reason']}`）。\n\n"
                + schedule_md
            )
            _append_step_summary(msg)
            return {"ok": True, "state": "skipped_schedule", "reason": sched["reason"]}
        if not (webhook or "").strip():
            msg = (
                "ℹ️ **今日为排期推送日**，服务器端默认飞书群由 VPS 自动发送；"
                "GitHub Secrets 未额外配置 `FEISHU_WEBHOOK`，跳过 GitHub 侧额外群发送。\n\n"
                + schedule_md
            )
            _append_step_summary(msg)
            return {"ok": True, "state": "no_github_webhook", "reason": "server_handles_default_webhook"}

    if not (source_url or "").strip():
        raise ValueError("未设置 BITFEED_SOURCE_URL 变量，无法拉取公开活动数据")

    pool, data_source = fetch_pool(source_url, now_ts, session=session)
    report = digest.build(
        None,
        now=now_ts,
        use_model=False,
        cfg=cfg,
        start_date=start_date or None,
        end_date=end_date or None,
        pool=pool,
    )
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    docx_name = f"学术先锋号活动汇总_{report['span']['start']}_至_{report['span']['end']}.docx"
    docx_path = out / docx_name
    digest.write_docx(report, docx_path)
    docx_url = _derive_docx_url(source_url)
    card_md = digest.card(report, docx_url)
    (out / "preview.md").write_text(card_md, encoding="utf-8")

    delivery = {"state": "preview_only", "detail": ""}
    if action in ("send_feishu", "auto_schedule"):
        if not (webhook or "").strip():
            delivery = {
                "state": "missing_webhook",
                "detail": "尚未在 GitHub Secrets 中配置 FEISHU_WEBHOOK。若要推送到新飞书群，请按说明文档添加 FEISHU_WEBHOOK。",
            }
        else:
            delivery = feishu.send_direct(
                webhook,
                card_md,
                report["title"],
                signing_secret=signing_secret,
                session=session,
            )

    status_banner = {
        "preview_only": "✅ **活动预览与 Word 文档已生成（未发送飞书群）**",
        "sent": "✅ **已成功推送至飞书群机器人！**",
        "missing_webhook": "⚠️ **已生成预览与 Word，但 GitHub 尚未配置 `FEISHU_WEBHOOK` 密钥**",
        "rejected": f"❌ **飞书群机器人拒绝了本次请求**（{delivery.get('detail', '')}）",
        "unknown": f"⚠️ **飞书群机器人返回状态未明，请检查群消息**（{delivery.get('detail', '')}）",
    }.get(delivery["state"], f"状态：{delivery['state']}")

    summary_doc = "\n".join([
        status_banner,
        "",
        f"- **本期统计范围**：`{report['period']}`",
        f"- **已核验活动总数**：`{len(report['article_ids'])}` 条（数据源：`{data_source}`）",
        f"- **下载完整版 Word**：请滑动到本页面最下方 **Artifacts** 区域，点击下载 **`学术先锋号活动汇总-Word版`**",
        *(["- **提示**：" + delivery["detail"]] if delivery.get("detail") else []),
        "",
        "---",
        "",
        "### 📋 本期飞书卡片内容预览（可直接复制）",
        "",
        card_md,
        "",
        "---",
        "",
        schedule_md,
    ])
    _append_step_summary(summary_doc)

    ok = delivery["state"] in ("preview_only", "sent", "missing_webhook")
    return {
        "ok": ok,
        "state": delivery["state"],
        "detail": delivery.get("detail", ""),
        "period": report["period"],
        "items": len(report["article_ids"]),
        "docx_path": str(docx_path),
        "data_source": data_source,
    }
