"""Simple, beginner-editable TOML schedule configuration for activity digests."""
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from .domain import TZ

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.9 / 3.10 fallback for flat key = value TOML
    tomllib = None

DEFAULT_PATH = Path(__file__).parent.parent / "config" / "push.toml"
DEFAULT_REMOTE_URL = "https://raw.githubusercontent.com/lankerr/bit-student-feed/main/config/push.toml"
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

DEFAULTS = {
    "enabled": True,
    "every_n_weeks": 2,
    "push_weekday": 2,
    "push_time": "09:00",
    "anchor_date": "2026-11-24",
    "start_weekday": 5,
    "span_days": 15,
    "title_prefix": "北理双周活动汇总（学术先锋号）",
}

_TIME_RE = re.compile(r"^(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$")
_BAD_TITLE_RE = re.compile(r"[\r\n\t]|https?://")


def _parse_flat_toml(text: str) -> dict:
    """Minimal flat TOML parser for Python <3.11 (supports comments, bools, ints, strings)."""
    result = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        # Strip comments outside quotes
        in_quote = None
        escaped = False
        clean_chars = []
        for ch in raw_line:
            if escaped:
                clean_chars.append(ch)
                escaped = False
                continue
            if ch == "\\" and in_quote == '"':
                clean_chars.append(ch)
                escaped = True
                continue
            if ch in ('"', "'"):
                if in_quote is None:
                    in_quote = ch
                elif in_quote == ch:
                    in_quote = None
                clean_chars.append(ch)
                continue
            if ch == "#" and in_quote is None:
                break
            clean_chars.append(ch)
        if in_quote is not None:
            raise ValueError(f"第 {lineno} 行引号未闭合")
        line = "".join(clean_chars).strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"第 {lineno} 行缺少等号 '='：{line}")
        key, raw_val = line.split("=", 1)
        key = key.strip()
        val = raw_val.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError(f"第 {lineno} 行配置项名称无效：{key}")
        if key in result:
            raise ValueError(f"第 {lineno} 行重复配置项：{key}")
        if val in ("true", "false"):
            result[key] = val == "true"
        elif (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            if len(val) < 2:
                raise ValueError(f"第 {lineno} 行字符串格式错误")
            inner = val[1:-1]
            if val[0] == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            result[key] = inner
        elif re.fullmatch(r"[+-]?\d+", val):
            result[key] = int(val)
        else:
            raise ValueError(f"第 {lineno} 行值格式不支持（字符串请加英文双引号，布尔值填 true/false）：{val}")
    return result


def parse_clock(value: str) -> Tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError('push_time 必须是字符串格式，例如 "09:00"')
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError('push_time 格式不正确，请填 24 小时制 "HH:MM"（例如 "09:00" 或 "14:30"）')
    return int(match.group("hour")), int(match.group("minute"))


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("配置文件内容格式无效")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        names = "、".join(sorted(unknown))
        raise ValueError(f"配置文件中存在未识别的字段：{names}（请对照默认模板检查拼写）")
    merged = {**DEFAULTS, **raw}

    if not isinstance(merged["enabled"], bool):
        raise ValueError("enabled 只能填 true 或 false（全小写，不加引号）")

    for key, low, high, hint in (
        ("every_n_weeks", 1, 8, "1（每周）到 8 之间的整数，每双周请填 2"),
        ("push_weekday", 1, 7, "1 到 7 之间的整数（1=周一，2=周二 ... 7=周日）"),
        ("start_weekday", 1, 7, "1 到 7 之间的整数（5=周五）"),
        ("span_days", 1, 42, "1 到 42 之间的整数（本周五到下下个周五请填 15）"),
    ):
        val = merged[key]
        if isinstance(val, bool) or not isinstance(val, int) or not (low <= val <= high):
            raise ValueError(f"{key} 必须是 {hint}")

    hour, minute = parse_clock(merged["push_time"])
    merged["push_time"] = f"{hour:02d}:{minute:02d}"

    anchor_str = merged["anchor_date"]
    if not isinstance(anchor_str, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", anchor_str.strip()):
        raise ValueError('anchor_date 必须是 "YYYY-MM-DD" 日期字符串，例如 "2026-11-24"')
    try:
        date.fromisoformat(anchor_str.strip())
    except ValueError as exc:
        raise ValueError(f"anchor_date 不是合法的日历日期：{anchor_str}") from exc
    merged["anchor_date"] = anchor_str.strip()

    prefix = merged["title_prefix"]
    if not isinstance(prefix, str) or not prefix.strip():
        raise ValueError("title_prefix 不能为空字符串")
    prefix = _BAD_TITLE_RE.sub("", prefix).strip()
    if not (1 <= len(prefix) <= 40):
        raise ValueError("title_prefix 长度需在 1 到 40 个字符之间，且不能包含网址或换行")
    merged["title_prefix"] = prefix
    return merged


def load_toml_text(text: str) -> dict:
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"TOML 语法错误（请检查引号或等号是否完整）：{exc}") from exc
    else:
        parsed = _parse_flat_toml(text)
    return validate_config(parsed)


def _valid_remote_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == "raw.githubusercontent.com"
        and not parts.username
        and not parts.password
        and parts.port in (None, 443)
        and parts.path.endswith("/config/push.toml")
    )


def load_config(path=None, *, sync_remote=False, remote_url=None, session=None) -> Tuple[dict, str]:
    local_path = Path(path) if path is not None else DEFAULT_PATH
    if local_path.exists():
        local_cfg = load_toml_text(local_path.read_text(encoding="utf-8-sig"))
        local_source = "local"
    else:
        local_cfg = dict(DEFAULTS)
        local_source = "defaults"

    if not sync_remote:
        return local_cfg, local_source

    target_url = (remote_url or DEFAULT_REMOTE_URL).strip()
    if not _valid_remote_url(target_url):
        return local_cfg, local_source
    try:
        http = session or requests.Session()
        resp = http.get(target_url, timeout=(3, 5), allow_redirects=False)
        if resp.status_code == 200 and 0 < len(resp.content) <= 16384:
            remote_cfg = load_toml_text(resp.content.decode("utf-8-sig"))
            return remote_cfg, "github_remote"
    except (requests.RequestException, UnicodeDecodeError, ValueError, OSError):
        pass
    return local_cfg, f"{local_source}_fallback"


def window_dates(for_day: date, cfg: Optional[dict] = None) -> Tuple[date, date]:
    cfg = validate_config(cfg or DEFAULTS)
    offset = (cfg["start_weekday"] - for_day.isoweekday()) % 7
    start = for_day + timedelta(days=offset)
    end = start + timedelta(days=cfg["span_days"] - 1)
    return start, end


def active_cycle_start(cfg: Optional[dict] = None, now: Optional[float] = None) -> str:
    """Return the ISO start timestamp of the active push cycle containing ``now``."""
    cfg = validate_config(cfg or DEFAULTS)
    local = datetime.now(TZ) if now is None else datetime.fromtimestamp(now, TZ)
    anchor = date.fromisoformat(cfg["anchor_date"])
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    anchor_push_day = anchor_monday + timedelta(days=cfg["push_weekday"] - 1)
    hour, minute = parse_clock(cfg["push_time"])
    anchor_dt = datetime(anchor_push_day.year, anchor_push_day.month, anchor_push_day.day, hour, minute, tzinfo=TZ)
    period_seconds = 7 * cfg["every_n_weeks"] * 86400
    elapsed_seconds = int((local - anchor_dt).total_seconds())
    k = elapsed_seconds // period_seconds
    return (anchor_dt + timedelta(seconds=k * period_seconds)).isoformat()


def schedule_due(cfg: Optional[dict] = None, now: Optional[float] = None, *, check_clock: bool = True) -> dict:
    cfg = validate_config(cfg or DEFAULTS)
    local = datetime.now(TZ) if now is None else datetime.fromtimestamp(now, TZ)
    today = local.date()
    anchor = date.fromisoformat(cfg["anchor_date"])
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    current_monday = today - timedelta(days=today.weekday())
    weeks_since = (current_monday - anchor_monday).days // 7

    scheduled_day = current_monday + timedelta(days=cfg["push_weekday"] - 1)
    hour, minute = parse_clock(cfg["push_time"])
    scheduled_dt = datetime(scheduled_day.year, scheduled_day.month, scheduled_day.day, hour, minute, tzinfo=TZ)
    cycle_key = scheduled_dt.isoformat()

    if not cfg["enabled"]:
        return {"due": False, "reason": "disabled", "cycle_key": cycle_key, "scheduled_at": cycle_key}
    if weeks_since < 0:
        return {"due": False, "reason": "before_anchor", "cycle_key": cycle_key, "scheduled_at": cycle_key}
    if weeks_since % cfg["every_n_weeks"] != 0:
        return {"due": False, "reason": "off_week", "cycle_key": cycle_key, "scheduled_at": cycle_key}
    if today != scheduled_day:
        return {"due": False, "reason": "wrong_weekday", "cycle_key": cycle_key, "scheduled_at": cycle_key}
    if check_clock and local < scheduled_dt:
        return {"due": False, "reason": "before_push_time", "cycle_key": cycle_key, "scheduled_at": cycle_key}
    return {"due": True, "reason": "due", "cycle_key": cycle_key, "scheduled_at": cycle_key}


def upcoming_runs(cfg: Optional[dict] = None, now: Optional[float] = None, count: int = 4) -> List[Dict[str, str]]:
    cfg = validate_config(cfg or DEFAULTS)
    if not cfg["enabled"]:
        return []
    local = datetime.now(TZ) if now is None else datetime.fromtimestamp(now, TZ)
    cursor = local.date()
    anchor = date.fromisoformat(cfg["anchor_date"])
    anchor_monday = anchor - timedelta(days=anchor.weekday())
    hour, minute = parse_clock(cfg["push_time"])
    runs = []
    for _ in range(420):
        monday = cursor - timedelta(days=cursor.weekday())
        weeks_since = (monday - anchor_monday).days // 7
        if cursor.isoweekday() == cfg["push_weekday"] and weeks_since >= 0 and weeks_since % cfg["every_n_weeks"] == 0:
            dt = datetime(cursor.year, cursor.month, cursor.day, hour, minute, tzinfo=TZ)
            if dt >= local - timedelta(hours=12):
                w_start, w_end = window_dates(cursor, cfg)
                runs.append({
                    "push_date": cursor.isoformat(),
                    "push_weekday": WEEKDAY_NAMES[cursor.weekday()],
                    "push_time": cfg["push_time"],
                    "window_start": w_start.isoformat(),
                    "window_start_weekday": WEEKDAY_NAMES[w_start.weekday()],
                    "window_end": w_end.isoformat(),
                    "window_end_weekday": WEEKDAY_NAMES[w_end.weekday()],
                })
                if len(runs) >= count:
                    break
        cursor += timedelta(days=1)
    return runs


def describe_schedule(cfg: Optional[dict] = None) -> str:
    cfg = validate_config(cfg or DEFAULTS)
    if not cfg["enabled"]:
        return "定时推送已暂停（config/push.toml 中 enabled = false）"
    freq = "每周" if cfg["every_n_weeks"] == 1 else (
        "每双周" if cfg["every_n_weeks"] == 2 else f"每 {cfg['every_n_weeks']} 周"
    )
    push_wd = WEEKDAY_NAMES[cfg["push_weekday"] - 1]
    start_wd = WEEKDAY_NAMES[cfg["start_weekday"] - 1]
    end_wd = WEEKDAY_NAMES[(cfg["start_weekday"] - 1 + cfg["span_days"] - 1) % 7]
    return (
        f"{freq}{push_wd} {cfg['push_time']}（北京时间，基准日 {cfg['anchor_date']}），"
        f"统计本{start_wd}起连续 {cfg['span_days']} 天（至{end_wd}）全部已核验活动"
    )
