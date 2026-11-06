"""Durable local transport ledger, distinct from cloud business completion."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .files import ensure_shared_perms
from .settings import DATA_DIR
from .reply_policy import canonical_group, denial, related_topic, same_topic


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
                CREATE TABLE IF NOT EXISTS collection_windows (
                    scope TEXT NOT NULL, generation INTEGER NOT NULL,
                    deadline REAL NOT NULL, opened REAL NOT NULL,
                    anchor_id TEXT NOT NULL, window_seconds REAL NOT NULL,
                    status TEXT NOT NULL, owner TEXT,
                    PRIMARY KEY(scope, generation));
                CREATE UNIQUE INDEX IF NOT EXISTS collection_one_open
                    ON collection_windows(scope) WHERE status='open';
                CREATE TABLE IF NOT EXISTS collection_members (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL,
                    ordinal INTEGER NOT NULL, payload TEXT NOT NULL, enrolled REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS collection_member_order
                    ON collection_members(scope, generation, ordinal);
                CREATE TABLE IF NOT EXISTS collection_receipts (
                    anchor_id TEXT PRIMARY KEY, scope TEXT NOT NULL, generation INTEGER NOT NULL,
                    status TEXT NOT NULL, updated REAL NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS natural_leases (
                    scope TEXT NOT NULL, batch_id TEXT NOT NULL,
                    tokens TEXT NOT NULL, anchors TEXT NOT NULL, sources TEXT NOT NULL,
                    opened REAL NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY (scope, batch_id));
                CREATE INDEX IF NOT EXISTS natural_lease_scope ON natural_leases(scope, expires);
            """)
            if "request_hash" not in {row[1] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN request_hash TEXT")
            from .conversation import initialize
            initialize(db)
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
        # Rows parked in a collection stay 'collecting'. An open window resumes;
        # a flush without a terminal receipt is retried with the same anchor id.
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE inbox SET status='unknown',error='Bridge restarted during processing',updated=? WHERE status='processing'", (now,))
            self._recover_collections(db, now)

    def claim(self, identity: str) -> bool:
        with self.connect() as db:
            return db.execute("UPDATE inbox SET status='processing',attempts=attempts+1,updated=? WHERE id=? AND status='pending'", (time.time(), identity)).rowcount == 1

    def finish(self, identity: str, status: str, error: str | None = None):
        if status not in {"accepted", "skipped", "failed", "unknown"}:
            raise ValueError("Invalid inbox outcome")
        with self.connect() as db:
            db.execute("UPDATE inbox SET status=?,error=?,updated=? WHERE id=?", (status, error, time.time(), identity))

    def reserve_action(self, identity: str, scope: str, request_hash: str | None = None, *, force: bool = False,
                       reply_meta: dict | None = None, limits: dict | None = None, cooldown_active: bool = False,
                       conversation_turn: dict | None = None) -> tuple[bool, dict]:
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
            if cooldown_active and not force:
                raise ReplySuppressed("Group cooldown active")
            if conversation_turn is not None:
                from .conversation import reserve
                try:
                    reserve(db, scope, identity, conversation_turn, now)
                except ValueError as error:
                    raise ReplySuppressed(str(error)) from error
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

    def claim_followup(self, scope: str, member_openid: str) -> dict | None:
        """Claim one pending follow-up from the same sender. A different sender stops the scan."""
        if not scope or not member_openid:
            return None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id, payload FROM inbox WHERE scope=? AND status='pending' ORDER BY created, rowid LIMIT 5",
                (scope,),
            ).fetchall()
            for row in rows:
                try:
                    ev = json.loads(row["payload"])
                except Exception:
                    continue
                if ev.get("t") not in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                    break
                d = ev.get("d") or {}
                author = (d.get("author") or {}) if isinstance(d, dict) else {}
                aoid = author.get("member_openid") or author.get("id") or ""
                if aoid != member_openid:
                    break
                if db.execute(
                    "UPDATE inbox SET status='accepted', error='coalesced_into_previous', updated=? WHERE id=? AND status='pending'",
                    (time.time(), row["id"]),
                ).rowcount == 1:
                    ev["_delivery_id"] = row["id"]
                    return ev
        return None

    def admit_natural(self, scope: str, topic: str, tokens: list[str], window: float) -> bool:
        """Leading-edge debounce: log every inbound event, forward only one burst.

        This does not sleep or delay gateway ACKs. No semantic claim beyond token similarity.
        """
        if window <= 0 or (not topic and not tokens):
            return True
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
            from .conversation import finish
            db.execute("UPDATE outbox SET status=?,detail=?,updated=? WHERE id=?", (status, json.dumps(detail or {}, ensure_ascii=False), time.time(), identity))
            finish(db, identity, status, time.time())

    def recent_actions(self, scope: str, window: float = 120):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,status,updated FROM outbox WHERE scope=? AND updated>? AND status IN ('inflight','sent','partial') ORDER BY updated DESC LIMIT 20", (scope, time.time() - window))]

    def part_status(self, identity: str, part: int, status: str, detail=None):
        with self.connect() as db:
            db.execute("INSERT INTO outbound_parts(action_id,part,status,detail) VALUES(?,?,?,?) ON CONFLICT(action_id,part) DO UPDATE SET status=excluded.status,detail=excluded.detail", (identity, part, status, json.dumps(detail or {}, ensure_ascii=False)))


SAME_SENDER_FOLLOWUP_SECONDS = 3.5
COLLECTION_TERMINAL = ("accepted", "skipped", "failed", "unknown")


def coalesce_same_sender(store, scope: str, member_openid: str, *, wait_sec: float, absorb, now=None, sleep=None) -> None:
    """Legacy same-sender wait used only while the collection window is disarmed.

    The initial deadline is ``now + wait_sec``. Each absorbed follow-up replaces
    it with ``min(deadline + 1s, now + 1.5s)``, so a stream of same-sender
    messages keeps sliding the deadline. ``absorb(event)`` is true when merged.
    """
    clock = now or time.time
    pause = sleep or time.sleep
    if wait_sec <= 0 or not scope or not member_openid:
        return
    deadline = clock() + wait_sec
    while clock() < deadline:
        nxt = store.claim_followup(scope, member_openid)
        if nxt and absorb(nxt):
            deadline = min(deadline + 1.0, clock() + 1.5)
            continue
        if deadline - clock() <= 0:
            break
        pause(0.25)


def _recover_collections(self, db, now: float) -> None:
    rows = db.execute("SELECT * FROM collection_windows WHERE status!='closed'").fetchall()
    for row in rows:
        receipt = db.execute("SELECT status, error FROM collection_receipts WHERE anchor_id=?", (row["anchor_id"],)).fetchone()
        if receipt and receipt["status"] in COLLECTION_TERMINAL:
            self._finalize_collection(db, row, receipt["status"], receipt["error"], now)
        elif row["status"] == "flushing":
            db.execute("UPDATE collection_windows SET status='open', owner=NULL WHERE scope=? AND generation=?",
                       (row["scope"], row["generation"]))


def _finalize_collection(self, db, row, status: str, error, now: float) -> None:
    db.execute("""UPDATE inbox SET status=?, error=?, updated=?
        WHERE id IN (SELECT id FROM collection_members WHERE scope=? AND generation=?)
        AND status IN ('collecting','processing')""",
        (status, error, now, row["scope"], row["generation"]))
    db.execute("UPDATE collection_windows SET status='closed', owner=NULL WHERE scope=? AND generation=?",
               (row["scope"], row["generation"]))


def _window_view(row, ordinal: int) -> dict:
    return {"action": "collecting", "scope": row["scope"], "generation": row["generation"],
            "deadline": row["deadline"], "opened": row["opened"], "anchor_id": row["anchor_id"],
            "ordinal": ordinal, "window_seconds": row["window_seconds"]}


def enroll_collection(self, *, scope: str, delivery_id: str, payload: dict, window_seconds: float) -> dict:
    """Park one inbox row in the scope's open fixed window.

    The deadline is the anchor arrival plus ``window_seconds``. A message that
    arrived at or after that deadline seals the window and opens the next one.
    """
    if not scope or not delivery_id:
        raise ValueError("collection enroll requires scope and delivery id")
    if type(window_seconds) is bool or not isinstance(window_seconds, (int, float)) or not window_seconds > 0 or window_seconds > 86400:
        raise ValueError("collection window is disabled")
    last_error = None
    for _attempt in range(2):
        try:
            return self._enroll_collection(scope, delivery_id, dict(payload), float(window_seconds))
        except sqlite3.IntegrityError as exc:
            last_error = exc
    raise RuntimeError("collection window enrollment conflicted") from last_error


def _enroll_collection(self, scope: str, delivery_id: str, payload: dict, window_seconds: float) -> dict:
    payload["delivery_id"] = delivery_id
    body = json.dumps(payload, ensure_ascii=False)
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        inbox = db.execute("SELECT scope, status, created FROM inbox WHERE id=?", (delivery_id,)).fetchone()
        if not inbox:
            raise ValueError("collection enroll requires a durable inbox row")
        if inbox["scope"] != scope:
            raise ValueError("collection scope does not match inbox")
        arrived = float(inbox["created"])
        existing = db.execute("SELECT scope, generation, ordinal FROM collection_members WHERE id=?", (delivery_id,)).fetchone()
        if existing:
            if inbox["status"] in ("pending", "processing"):
                db.execute("UPDATE inbox SET status='collecting', error=NULL, updated=? WHERE id=?", (time.time(), delivery_id))
            window = db.execute("SELECT * FROM collection_windows WHERE scope=? AND generation=?",
                                (existing["scope"], existing["generation"])).fetchone()
            if not window:
                raise ValueError("collection member has no window")
            return _window_view(window, existing["ordinal"])
        if inbox["status"] not in ("pending", "processing"):
            raise ValueError("inbox status cannot join a collection")
        window = db.execute("SELECT * FROM collection_windows WHERE scope=? AND status='open'", (scope,)).fetchone()
        if window and arrived >= float(window["deadline"]):
            db.execute("UPDATE collection_windows SET status='flushing' WHERE scope=? AND generation=? AND status='open'",
                       (scope, window["generation"]))
            window = None
        if window is None:
            generation = db.execute("SELECT COALESCE(MAX(generation),0)+1 FROM collection_windows WHERE scope=?", (scope,)).fetchone()[0]
            db.execute("""INSERT INTO collection_windows
                (scope,generation,deadline,opened,anchor_id,window_seconds,status,owner)
                VALUES(?,?,?,?,?,?,'open',NULL)""",
                (scope, generation, arrived + window_seconds, arrived, delivery_id, window_seconds))
            ordinal = 1
        else:
            generation = window["generation"]
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM collection_members WHERE scope=? AND generation=?",
                                 (scope, generation)).fetchone()[0]
        db.execute("INSERT INTO collection_members(id,scope,generation,ordinal,payload,enrolled) VALUES(?,?,?,?,?,?)",
                   (delivery_id, scope, generation, ordinal, body, arrived))
        if db.execute("UPDATE inbox SET status='collecting', error=NULL, updated=? WHERE id=? AND status IN ('pending','processing')",
                      (time.time(), delivery_id)).rowcount != 1:
            raise ValueError("inbox row was not claimable for collection")
        stored = db.execute("SELECT * FROM collection_windows WHERE scope=? AND generation=?", (scope, generation)).fetchone()
        return _window_view(stored, ordinal)


def lease_due_collections(self, now: float) -> list[dict]:
    """Claim windows whose original deadline has arrived. Does not move deadlines."""
    token = uuid.uuid4().hex
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE collection_windows SET status='flushing', owner=? WHERE status='open' AND deadline<=?", (token, now))
        db.execute("UPDATE collection_windows SET owner=? WHERE status='flushing' AND owner IS NULL AND deadline<=?", (token, now))
        rows = [dict(row) for row in db.execute("SELECT * FROM collection_windows WHERE owner=? ORDER BY deadline, generation", (token,))]
        for row in rows:
            members = []
            for member in db.execute("SELECT id, payload FROM collection_members WHERE scope=? AND generation=? ORDER BY ordinal, rowid",
                                     (row["scope"], row["generation"])):
                body = json.loads(member["payload"])
                body["delivery_id"] = member["id"]
                members.append(body)
            row["members"] = members
        return rows


def begin_collection_flush(self, scope: str, generation: int) -> bool:
    """Record an inflight receipt. A terminal receipt is not sent again."""
    now = time.time()
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM collection_windows WHERE scope=? AND generation=?", (scope, generation)).fetchone()
        if not row or row["status"] == "closed":
            return False
        db.execute("INSERT OR IGNORE INTO collection_receipts(anchor_id,scope,generation,status,updated,error) VALUES(?,?,?,'inflight',?,NULL)",
                   (row["anchor_id"], scope, generation, now))
        receipt = db.execute("SELECT status, error FROM collection_receipts WHERE anchor_id=?", (row["anchor_id"],)).fetchone()
        if receipt and receipt["status"] in COLLECTION_TERMINAL:
            self._finalize_collection(db, row, receipt["status"], receipt["error"], now)
            return False
        return True


def complete_collection(self, scope: str, generation: int, status: str, error: str | None = None) -> None:
    if status not in COLLECTION_TERMINAL:
        status = "unknown"
    now = time.time()
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM collection_windows WHERE scope=? AND generation=?", (scope, generation)).fetchone()
        if not row or row["status"] == "closed":
            return
        db.execute("""INSERT INTO collection_receipts(anchor_id,scope,generation,status,updated,error) VALUES(?,?,?,?,?,?)
            ON CONFLICT(anchor_id) DO UPDATE SET status=excluded.status, generation=excluded.generation,
            updated=excluded.updated, error=excluded.error""",
            (row["anchor_id"], scope, generation, status, now, error))
        self._finalize_collection(db, row, status, error, now)


def seconds_until_collection_due(self, now: float) -> float | None:
    with self.connect() as db:
        row = db.execute("""SELECT MIN(deadline) AS deadline FROM collection_windows
            WHERE status='open' OR (status='flushing' AND owner IS NULL)""").fetchone()
        if not row or row["deadline"] is None:
            return None
        return float(row["deadline"]) - now


def _lease_sources_live(db, scope: str, sources: list[str], action_id: str) -> bool:
    if not sources:
        return False
    placeholders = ",".join("?" * len(sources))
    return db.execute(f"""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
        WHERE r.scope=? AND r.action_id!=? AND json_extract(r.metadata,'$.source') IN ({placeholders})
        AND json_extract(r.metadata,'$.trigger')='natural'
        AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
        (scope, action_id, *sources)).fetchone() is not None


def claim_natural_forward(self, *, scope: str, batch_id: str, sources: list[str], tokens: list[str],
                          anchors: list[str], weak: bool, window: float, suppress: bool = True) -> bool:
    """Atomically admit one unsolicited natural wake for a group burst.

    The deadline is ``now + window`` and is not moved when a later message joins.
    The same batch id may be forwarded again until a live natural answer exists,
    so a crashed flush can retry. A different source in the same topic, and an
    image or short agreement while any lease is open, does not start a second wake.
    A substantive unrelated topic opens its own lease. ``suppress=False`` records
    a batch that must still be delivered (it contains a new question).
    """
    if window <= 0 or not scope or not batch_id:
        return True
    now = time.time()
    group = canonical_group(scope)
    incoming = list(dict.fromkeys(str(item) for item in sources if item))
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM natural_leases WHERE expires<?", (now - 86400,))
        rows = db.execute("""SELECT * FROM natural_leases WHERE scope=? AND expires>?
            ORDER BY opened DESC""", (group, now)).fetchall()
        send_scopes = list(dict.fromkeys((group, "/v2/groups/" + group, "group:" + group)))
        for row in rows:
            held = [str(item) for item in json.loads(row["sources"]) if item]
            if row["batch_id"] == batch_id:
                if suppress and any(_lease_sources_live(db, item, held, "") for item in send_scopes):
                    return False
                return True
            joined = weak or related_topic(tokens, anchors, json.loads(row["tokens"]), json.loads(row["anchors"]))
            if not joined:
                continue
            merged = list(dict.fromkeys([*held, *incoming]))
            db.execute("UPDATE natural_leases SET sources=? WHERE scope=? AND batch_id=?",
                       (json.dumps(merged, ensure_ascii=False), group, row["batch_id"]))
            return not suppress
        db.execute("""INSERT INTO natural_leases(scope,batch_id,tokens,anchors,sources,opened,expires)
            VALUES(?,?,?,?,?,?,?)""",
            (group, batch_id, json.dumps(list(tokens), ensure_ascii=False),
             json.dumps(list(anchors), ensure_ascii=False), json.dumps(incoming, ensure_ascii=False),
             now, now + float(window)))
        return True


def release_natural_lease(self, scope: str, batch_id: str) -> None:
    """Drop a wake that never reached the webhook, so a later message can retry."""
    group = canonical_group(scope)
    with self.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT sources FROM natural_leases WHERE scope=? AND batch_id=?", (group, batch_id)).fetchone()
        if not row:
            return
        held = [str(item) for item in json.loads(row["sources"]) if item]
        send_scopes = (group, "/v2/groups/" + group, "group:" + group)
        if any(_lease_sources_live(db, item, held, "") for item in send_scopes):
            return
        db.execute("DELETE FROM natural_leases WHERE scope=? AND batch_id=?", (group, batch_id))


DeliveryStore._recover_collections = _recover_collections
DeliveryStore._finalize_collection = _finalize_collection
DeliveryStore.enroll_collection = enroll_collection
DeliveryStore._enroll_collection = _enroll_collection
DeliveryStore.lease_due_collections = lease_due_collections
DeliveryStore.begin_collection_flush = begin_collection_flush
DeliveryStore.complete_collection = complete_collection
DeliveryStore.seconds_until_collection_due = seconds_until_collection_due
DeliveryStore.claim_natural_forward = claim_natural_forward
DeliveryStore.release_natural_lease = release_natural_lease


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
            # A fixed collection window parks the row as 'collecting' and returns
            # before this worker sleeps or forwards. Do not overwrite that status.
            if result != "collecting":
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
