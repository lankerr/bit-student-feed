import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

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
                if old and not item["body"] and old["body"]:
                    item = normalize_article({**item, "body": old["body"],
                        "digest": item["digest"] or old["digest"], "body_status": old["body_status"]})
                added += old is None
                changed += old is not None and old["source_hash"] != item["source_hash"]
                conn.execute('''INSERT INTO articles VALUES(
                    :id,:title,:url,:source,:published_at,:first_seen,:kind,:body,:digest,:source_hash,:body_status)
                    ON CONFLICT(id) DO UPDATE SET title=excluded.title,url=excluded.url,
                    source=excluded.source,published_at=excluded.published_at,kind=excluded.kind,
                    body=excluded.body,
                    digest=excluded.digest,source_hash=excluded.source_hash,body_status=excluded.body_status''',
                    {**item, "first_seen": int(time.time())})
            for key, value in (("last_collection", checked_at or now_iso()),
                               ("source_refreshed_at", refreshed_at or checked_at or now_iso())):
                conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, value))
        return {"imported": len(normalized), "added": added, "changed": changed}

    def candidates(self, limit=20, days=30):
        with self.connection() as conn:
            rows = conn.execute('''SELECT a.* FROM articles a LEFT JOIN classifications c
                ON c.article_id=a.id WHERE a.published_at>=? AND
                (c.article_id IS NULL OR c.source_hash!=a.source_hash OR c.prompt_version!=?)
                ORDER BY a.published_at DESC LIMIT ?''', (int(time.time())-days*86400, PROMPT_VERSION, limit)).fetchall()
        return [dict(row) for row in rows]

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
        with self.connection() as conn:
            rows = conn.execute('''SELECT a.id,a.title,a.url,a.source,a.published_at,a.kind,
                a.source_hash,c.payload,c.model,c.source_hash AS classified_source_hash
                FROM articles a LEFT JOIN classifications c ON c.article_id=a.id
                WHERE a.published_at>=? ORDER BY a.published_at DESC LIMIT ?''',
                (int(time.time())-days*86400, limit)).fetchall()
        results = []
        for row in rows:
            article = dict(row)
            payload = article.pop("payload")
            cache_hash = article.pop("classified_source_hash")
            article["classification"] = json.loads(payload) if payload and cache_hash == article["source_hash"] else None
            article["date"] = datetime.fromtimestamp(article["published_at"], TZ).strftime("%Y-%m-%d")
            if article["classification"]:
                c = article["classification"]
                today = datetime.now(TZ).date().isoformat()
                if any(c.get(k) and c[k] < today for k in ("deadline", "event_date")):
                    c.update(event_status="expired", verified_activity=False, score=min(c["score"], 10))
            results.append(article)
        return results

    def metadata(self):
        with self.connection() as conn:
            return dict(conn.execute("SELECT key,value FROM meta"))

    def record_job(self, kind, state, detail):
        with self.connection() as conn:
            conn.execute("INSERT INTO jobs(kind,started_at,state,detail) VALUES(?,?,?,?)",
                         (kind, now_iso(), state, str(detail)[:1000]))

    def reserve_call(self, daily_limit):
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        key = "model_calls:" + today
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= daily_limit:
                raise ValueError("daily model request budget reached")
            conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(count + 1)))
        return count + 1
