"""Registry of subscribed official accounts (``config/wechat-accounts.json``)."""
import json
from pathlib import Path

from .weread import book_id_from_fakeid

# Poll interval per tier, in seconds; tier 3 is listed for reference only.
TIER_INTERVAL = {0: 3600, 1: 3 * 3600, 2: 8 * 3600}
DEFAULT_PATH = Path(__file__).parent.parent / "config" / "wechat-accounts.json"


def load(path=DEFAULT_PATH, include_disabled=False):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    rows = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("wechat-accounts.json must contain an accounts list")
    accounts, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("name") or not row.get("fakeid"):
            raise ValueError("every account needs name and fakeid")
        tier = int(row.get("tier", 1))
        if tier not in (0, 1, 2, 3):
            raise ValueError("tier must be 0..3")
        book_id = book_id_from_fakeid(row["fakeid"])
        if book_id in seen:
            raise ValueError("duplicate account " + row["name"])
        seen.add(book_id)
        if not include_disabled and (tier == 3 or row.get("enabled") is False):
            continue
        accounts.append({"name": str(row["name"])[:60], "fakeid": row["fakeid"], "book_id": book_id,
                         "unit": str(row.get("unit") or "")[:60], "tier": tier,
                         "interval": int(row.get("interval") or TIER_INTERVAL.get(tier, 86400))})
    return accounts
