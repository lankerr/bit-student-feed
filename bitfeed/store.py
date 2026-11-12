import json
import sqlite3
import time
import re
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime

from .domain import PROMPT_VERSION, TZ, normalize_article, now_iso, validate_classification


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "bitfeed.sqlite"
        with self.connection() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS articles(
                id TEXT PRIMARY KEY, title TEXT, url TEXT, source TEXT,
                published_at INTEGER, first_seen INTEGER, kind TEXT,
                body TEXT, digest TEXT, source_hash TEXT, body_status TEXT);
            CREATE TABLE IF NOT EXISTS classifications(
                article_id TEXT PRIMARY KEY, source_hash TEXT, prompt_version TEXT,
                model TEXT, payload TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started_at TEXT,
                state TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(
                target_hash TEXT, batch_id TEXT, state TEXT, updated_at TEXT,
                detail TEXT, PRIMARY KEY(target_hash,batch_id));
            CREATE TABLE IF NOT EXISTS delivered_articles(
                target_hash TEXT, article_id TEXT, sent_at TEXT,
                PRIMARY KEY(target_hash,article_id));
            CREATE INDEX IF NOT EXISTS article_time ON articles(published_at);
            ''')
            existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
            for name, definition in (("origin", "TEXT DEFAULT 'unknown'"),
                                     ("publication_status", "TEXT DEFAULT 'known'"),
                                     ("source_expiry", "INTEGER DEFAULT 0")):
                if name not in existing:
                    conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def import_articles(self, items, checked_at=None, refreshed_at=None):
        normalized = [normalize_article(item) for item in items]
        added = changed = 0
        with self.connection() as conn:
            for item in normalized:
                old = conn.execute("SELECT * FROM articles WHERE id=?", (item["id"],)).fetchone()
                if old and item["publication_status"] == "unknown":
                    item = normalize_article({**item,"published_at":old["published_at"]})
                if old and not item["body"] and old["body"]:
                    item = normalize_article({**item, "body": old["body"],
                        "digest": item["digest"] or old["digest"], "body_status": old["body_status"]})
                added += old is None
                changed += old is not None and old["source_hash"] != item["source_hash"]
                conn.execute('''INSERT INTO articles(id,title,url,source,published_at,first_seen,kind,body,digest,source_hash,body_status,origin,publication_status,source_expiry) VALUES(
                    :id,:title,:url,:source,:published_at,:first_seen,:kind,:body,:digest,:source_hash,:body_status,:origin,:publication_status,:source_expiry)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,
                    source=excluded.source,published_at=excluded.published_at,kind=excluded.kind,
                    body=excluded.body,
                    digest=excluded.digest,source_hash=excluded.source_hash,body_status=excluded.body_status,
                    origin=excluded.origin,publication_status=excluded.publication_status,source_expiry=excluded.source_expiry''',
                    {**item, "first_seen": int(time.time())})
            for key, value in (("last_collection", checked_at or now_iso()),
                               ("source_refreshed_at", refreshed_at or checked_at or now_iso())):
                conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
        return {"imported": len(normalized), "added": added, "changed": changed}

    def candidates(self, limit=20, days=30):
        if limit<=0:return []
        # Full-text WeRead articles must not be starved by the large career-list backlog:
        # available body + known publish time ranks first, then dated list metadata.
        with self.connection() as conn:
            rows = conn.execute('''SELECT a.* FROM articles a LEFT JOIN classifications c
                ON c.article_id=a.id WHERE (a.published_at>=? OR a.source_expiry>? OR a.body_status='available') AND
                (a.source_expiry=0 OR a.source_expiry>=?) AND
                (c.article_id IS NULL OR c.source_hash!=a.source_hash OR c.prompt_version!=?)
                ORDER BY CASE WHEN a.body_status='available' AND a.publication_status='known' THEN 0
                    WHEN a.body_status='structured_list_metadata' AND a.source_expiry>? THEN 1
                    WHEN a.publication_status='known' THEN 2
                    WHEN a.body_status='structured_list_metadata' THEN 3 ELSE 4 END,
                    a.published_at DESC''',
                (int(time.time())-days*86400, int(time.time()), int(time.time()), PROMPT_VERSION, int(time.time()))).fetchall()
        result=[]
        cutoff=int(time.time())-days*86400
        today=datetime.now(TZ).date()
        for row in rows:
            item=dict(row)
            future=False
            for y,m,d in re.findall(r'(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?',item.get('body') or ''):
                try:
                    if date(int(y),int(m),int(d))>=today:future=True;break
                except ValueError:pass
            if item['published_at']>=cutoff or item['source_expiry']>time.time() or future:
                result.append(item)
                if len(result)>=limit:break
        return result

    def apply_classifications(self, data, model, expected=None):
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("classification items required")
        if expected is not None and {x.get("article_id") for x in items} != set(expected):
            raise ValueError("classifier must return exactly the requested article IDs")
        validated, seen = [], set()
        with self.connection() as conn:
            for item in items:
                identifier = item.get("article_id")
                if identifier in seen:
                    raise ValueError("duplicate classification ID")
                seen.add(identifier)
                row = conn.execute("SELECT * FROM articles WHERE id=?", (identifier,)).fetchone()
                if row is None:
                    raise ValueError("classification references an unknown article")
                validated.append(validate_classification(item, dict(row)))
            for item in validated:
                conn.execute("INSERT OR REPLACE INTO classifications VALUES(?,?,?,?,?,?)",
                    (item["article_id"], item["source_hash"], PROMPT_VERSION, model,
                     json.dumps(item, ensure_ascii=False), now_iso()))
        return {"classified": len(validated), "verified_activities": sum(x["verified_activity"] for x in validated)}

    def public_articles(self, days=30, limit=5000):
        from .activity_time import activity_expired
        with self.connection() as conn:
            rows = conn.execute('''SELECT a.id,a.title,a.url,a.source,a.published_at,a.kind,
                a.source_hash,a.origin,a.publication_status,a.body_status,a.source_expiry,
                c.payload,c.model,c.source_hash AS classified_source_hash
                FROM articles a LEFT JOIN classifications c ON c.article_id=a.id
                WHERE (a.published_at>=? OR a.source_expiry>0 OR c.payload IS NOT NULL)
                AND (a.source_expiry=0 OR a.source_expiry>=?)
                ORDER BY a.published_at DESC LIMIT ?''',
                (int(time.time())-days*86400, int(time.time()), limit)).fetchall()
        results = []
        for row in rows:
            article = dict(row)
            payload = article.pop("payload")
            cache_hash = article.pop("classified_source_hash")
            article["classification"] = json.loads(payload) if payload and cache_hash == article["source_hash"] else None
            article["date"] = datetime.fromtimestamp(article["published_at"], TZ).strftime("%Y-%m-%d")
            if article["classification"]:
                c = article["classification"]
                if activity_expired(article):
                    c.update(event_status="expired", verified_activity=False, score=min(c["score"], 10))
                elif article['source'] in ('北理就业官网·招聘职位','北理就业官网·实习信息') and article['body_status']=='structured_list_metadata':
                    c.update(event_status='needs_verification',verified_activity=False,score=min(c['score'],40))
            if article['published_at']<int(time.time())-days*86400 and not (
                    article.get('source_expiry') or (article['classification'] or {}).get('verified_activity')):
                continue
            results.append(article)
        return results

    def missing_bodies(self, origin, days=14, limit=5):
        # title_only is a parsed page that had no text yet. no_text means that stayed
        # true across retries, so picture posts do not keep consuming the body budget.
        with self.connection() as conn:
            rows = conn.execute('''SELECT * FROM articles WHERE origin=? AND (body IS NULL OR body='')
                AND body_status IN ('not_fetched','title_only') AND first_seen>=? ORDER BY first_seen DESC LIMIT ?''',
                (origin, int(time.time()) - days * 86400, limit)).fetchall()
        return [dict(row) for row in rows]

    def attach_body(self, article_id, body, published_at=None, status="available"):
        """Store fetched article text; a page-reported publish time replaces first-seen time."""
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
            if row is None:
                raise KeyError(article_id)
            item = dict(row)
            known = published_at is not None
            item = normalize_article({**item, "body": body or "", "body_status": status,
                "published_at": published_at if known else item["published_at"],
                "publication_status": "known" if known else item["publication_status"]})
            conn.execute('''UPDATE articles SET body=?,body_status=?,published_at=?,publication_status=?,
                source_hash=? WHERE id=?''', (item["body"], item["body_status"], item["published_at"],
                item["publication_status"], item["source_hash"], article_id))
        return item

    def metadata(self):
        with self.connection() as conn:
            return dict(conn.execute("SELECT key,value FROM meta"))

    def record_job(self, kind, state, detail):
        with self.connection() as conn:
            conn.execute("INSERT INTO jobs(kind,started_at,state,detail) VALUES(?,?,?,?)",
                         (kind, now_iso(), state, str(detail)[:1000]))

    def reserve_call(self, daily_limit, budget="model_calls"):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        key = budget + ":" + today
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= daily_limit:
                raise ValueError("daily model request budget reached")
            conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(count + 1)))
        return count + 1

    def set_meta(self, key, value):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))

    def set_source_audit(self, audits):
        previous = {a.get("id"):a for a in json.loads(self.metadata().get("source_audit","[]"))}
        for a in audits:
            old = previous.get(a.get("id"),{})
            a["last_successful_at"] = (a.get("checked_at") if a.get("pages",0)>0 and
                a.get("state") in {"ok","partial","empty"} else old.get("last_successful_at"))
        self.set_meta("source_audit",json.dumps(audits,ensure_ascii=False))
