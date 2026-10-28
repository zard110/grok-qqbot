"""Durable local transport ledger, distinct from cloud business completion."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .files import ensure_shared_perms
from .settings import DATA_DIR
from .reply_policy import denial, same_topic


class ReplySuppressed(ValueError):
    """An intentional policy skip; no network request has been started."""


def stable_id(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def event_identity(event: dict) -> tuple[str, str]:
    data = event.get("d") or {}
    actor = data.get("author") or {}
    scope = ("group:" + str(data.get("group_openid") or data.get("group_id"))) if data.get("group_openid") or data.get("group_id") else "c2c:" + str(actor.get("user_openid") or data.get("user_openid") or "system")
    source_id = event.get("id") or data.get("id")
    return stable_id([scope, event.get("t"), source_id if source_id else event]), scope


class DeliveryStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or DATA_DIR / "runtime.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS inbox (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL,
                    updated REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status, created);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, detail TEXT NOT NULL DEFAULT '{}', request_hash TEXT);
                CREATE TABLE IF NOT EXISTS outbound_parts (
                    action_id TEXT NOT NULL, part INTEGER NOT NULL, status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(action_id, part));
                CREATE TABLE IF NOT EXISTS reply_claims (
                    action_id TEXT PRIMARY KEY, scope TEXT NOT NULL, created REAL NOT NULL,
                    metadata TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS reply_scope ON reply_claims(scope,created);
                CREATE INDEX IF NOT EXISTS reply_source ON reply_claims(scope,json_extract(metadata,'$.source'));
                CREATE INDEX IF NOT EXISTS reply_content ON reply_claims(scope,json_extract(metadata,'$.content_hash'),created);
                CREATE INDEX IF NOT EXISTS reply_topic ON reply_claims(scope,json_extract(metadata,'$.topic_key'),created);
                CREATE INDEX IF NOT EXISTS outbox_scope_time ON outbox(scope,updated);
                CREATE TABLE IF NOT EXISTS natural_topics (
                    scope TEXT NOT NULL, topic TEXT NOT NULL, tokens TEXT NOT NULL,
                    updated REAL NOT NULL, PRIMARY KEY(scope,topic));
            """)
            if "request_hash" not in {row[1] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN request_hash TEXT")
        for suffix in ("", "-wal", "-shm"):
            ensure_shared_perms(Path(str(self.path) + suffix))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, event: dict) -> str:
        identity, scope = event_identity(event)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO inbox(id,scope,payload,created,updated) VALUES(?,?,?,?,?)",
                (identity, scope, json.dumps(event, ensure_ascii=False), now, now))
        return identity

    def pending(self, limit=256):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM inbox WHERE rowid IN (SELECT MIN(rowid) FROM inbox WHERE status='pending' GROUP BY scope) ORDER BY created,rowid LIMIT ?", (limit,))]

    def recover(self):
        # Called only under the exclusive bridge process lock. Once delivery began,
        # a crash cannot prove whether the external webhook accepted the request.
        with self.connect() as db:
            db.execute("UPDATE inbox SET status='unknown',error='Bridge restarted during processing',updated=? WHERE status='processing'", (time.time(),))

    def claim(self, identity: str) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE inbox SET status='processing',attempts=attempts+1,updated=? WHERE id=? AND status='pending'", (time.time(), identity)).rowcount == 1

    def finish(self, identity: str, status: str, error: str | None = None):
        if status not in {"accepted", "skipped", "failed", "unknown"}:
            raise ValueError("Invalid inbox outcome")
        with self.connect() as db:
            db.execute("UPDATE inbox SET status=?,error=?,updated=? WHERE id=?", (status, error, time.time(), identity))

    def reserve_action(self, identity: str, scope: str, request_hash: str | None = None, *, force: bool = False,
                       reply_meta: dict | None = None, limits: dict | None = None) -> tuple[bool, dict]:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM outbox WHERE id=?", (identity,)).fetchone()
            if old and (old["scope"] != scope or (request_hash is not None and old["request_hash"] != request_hash)):
                raise ValueError("Action identity was already used for another scope or content")
            retry = bool(old and force and old["status"] in {"unknown", "failed"})
            if old and not retry:
                return False, dict(old)
            if reply_meta is not None:
                reason = denial(db, scope, identity, reply_meta, limits, now, force)
                if reason:
                    raise ReplySuppressed(reason)
            added = db.execute("INSERT OR IGNORE INTO outbox(id,scope,status,created,updated,request_hash) VALUES(?,?,'pending',?,?,?)", (identity, scope, now, now, request_hash)).rowcount == 1
            row = dict(db.execute("SELECT * FROM outbox WHERE id=?", (identity,)).fetchone())
            if reply_meta is not None:
                db.execute("INSERT INTO reply_claims VALUES(?,?,?,?) ON CONFLICT(action_id) DO UPDATE SET created=excluded.created,metadata=excluded.metadata",
                           (identity, scope, now, json.dumps(reply_meta, ensure_ascii=False)))
            if retry:
                db.execute("UPDATE outbox SET status='pending',updated=?,request_hash=? WHERE id=?", (now, request_hash, identity))
                row = dict(db.execute("SELECT * FROM outbox WHERE id=?", (identity,)).fetchone())
                return True, row
            # Existing pending/inflight/unknown actions must not be resent blindly.
            return added, row

    def admit_natural(self, scope: str, topic: str, tokens: list[str], window: float) -> bool:
        """Leading-edge debounce: log every inbound event, forward only one burst.

        This does not sleep or delay gateway ACKs. No semantic claim beyond token similarity.
        """
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM natural_topics WHERE updated<?", (now - 86400,))
            rows = db.execute("SELECT topic,tokens FROM natural_topics WHERE scope=? AND updated>?", (scope, now - window)).fetchall()
            if any(row["topic"] == topic or same_topic(tokens, json.loads(row["tokens"])) for row in rows):
                return False
            db.execute("INSERT INTO natural_topics VALUES(?,?,?,?) ON CONFLICT(scope,topic) DO UPDATE SET tokens=excluded.tokens,updated=excluded.updated",
                       (scope, topic, json.dumps(tokens), now))
            return True

    def action_status(self, identity: str, status: str, detail=None):
        if status not in {"pending", "inflight", "sent", "failed", "unknown", "partial"}:
            raise ValueError("Invalid outbound outcome")
        with self.connect() as db:
            db.execute("UPDATE outbox SET status=?,detail=?,updated=? WHERE id=?", (status, json.dumps(detail or {}, ensure_ascii=False), time.time(), identity))

    def recent_actions(self, scope: str, window: float = 120):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,status,updated FROM outbox WHERE scope=? AND updated>? AND status IN ('inflight','sent','partial') ORDER BY updated DESC LIMIT 20", (scope, time.time() - window))]

    def part_status(self, identity: str, part: int, status: str, detail=None):
        with self.connect() as db:
            db.execute("INSERT INTO outbound_parts(action_id,part,status,detail) VALUES(?,?,?,?) ON CONFLICT(action_id,part) DO UPDATE SET status=excluded.status,detail=excluded.detail", (identity, part, status, json.dumps(detail or {}, ensure_ascii=False)))


def snapshot(db_path=None) -> dict:
    path = Path(db_path or DATA_DIR / "runtime.sqlite3")
    if not path.exists():
        return {"inbox": {}, "outbox": {}, "oldest_pending_age": 0, "recent_failures": []}
    # Mode=ro keeps dashboards from creating/migrating transport state.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        counts = {table: dict(db.execute("SELECT status,COUNT(*) FROM " + table + " GROUP BY status")) for table in ("inbox", "outbox")}
        oldest = db.execute("SELECT MIN(created) FROM inbox WHERE status IN ('pending','processing')").fetchone()[0]
        failures = [{"id": row[0], "scope": row[1], "status": row[2], "updated": row[3]} for row in db.execute("SELECT id,scope,status,updated FROM inbox WHERE status IN ('failed','unknown') ORDER BY updated DESC LIMIT 20")]
    finally:
        db.close()
    return {**counts, "oldest_pending_age": max(0, time.time() - oldest) if oldest else 0, "recent_failures": failures}


class GroupDispatcher:
    """One active event per scope, bounded cross-scope workers; backlog stays in SQLite."""
    def __init__(self, store: DeliveryStore, handler, workers=4):
        self.store, self.handler = store, handler
        self.workers = workers
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dispatch")
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def put(self, event):
        identity = self.store.enqueue(event)
        self._wake.set()
        return identity

    def _loop(self):
        while not self._stop.is_set():
            try:
                for row in self.store.pending():
                    with self._lock:
                        if len(self._active) >= self.workers:
                            break
                        if row["scope"] in self._active:
                            continue
                        self._active.add(row["scope"])
                    try:
                        if self.store.claim(row["id"]):
                            self._pool.submit(self._run, row)
                        else:
                            with self._lock:
                                self._active.discard(row["scope"])
                    except Exception:
                        with self._lock:
                            self._active.discard(row["scope"])
                        raise
                self.last_error = None
            except Exception:
                self.last_error = "Durable scheduler storage unavailable"
            self._wake.wait(.25)
            self._wake.clear()

    def _run(self, row):
        try:
            event = json.loads(row["payload"])
            event["_delivery_id"] = row["id"]
            result = self.handler(event)
            self.store.finish(row["id"], result or "skipped")
        except Exception:
            # A generic handler exception may follow a successful external call.
            self.store.finish(row["id"], "unknown", "Handler did not produce a confirmed outcome")
        finally:
            with self._lock:
                self._active.discard(row["scope"])
            self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2)
        self._pool.shutdown(wait=True)
