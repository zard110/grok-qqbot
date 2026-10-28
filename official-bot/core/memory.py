"""Scoped message windows, summaries, facts and versioned knowledge (stdlib only).

SQLite is the authority for edited memories. Legacy files are import/export formats.
Every public operation requires a conversation scope; no cross-scope search exists.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .files import confined_path, ensure_shared_perms


class MemoryConflict(ValueError):
    pass


def _scope(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", value):
        raise ValueError("Invalid conversation scope")
    return value


def _text(value: str, maximum: int = 20000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Text must contain 1 to {maximum} characters")
    return value.strip()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
    result = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            result.append(word)
    return list(dict.fromkeys(result))


class MemoryStore:
    def __init__(self, data_dir: Path | str, *, use_fts: bool = True, auto_import: bool = True):
        self.path = Path(data_dir) / "memory.sqlite3"
        self.auto_import = auto_import
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts = False
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
              CREATE TABLE IF NOT EXISTS memory_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              INSERT OR IGNORE INTO memory_meta VALUES('schema_version','1');
              CREATE TABLE IF NOT EXISTS messages(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                event_key TEXT NOT NULL, direction TEXT NOT NULL, text TEXT NOT NULL,
                timestamp REAL NOT NULL, UNIQUE(scope,event_key));
              CREATE INDEX IF NOT EXISTS messages_scope ON messages(scope,seq);
              CREATE TABLE IF NOT EXISTS memories(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,subject TEXT NOT NULL,
                predicate TEXT NOT NULL,value TEXT NOT NULL,source TEXT NOT NULL,
                revision INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS memories_scope ON memories(scope,deleted);
              CREATE TABLE IF NOT EXISTS summaries(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,from_seq INTEGER NOT NULL,
                to_seq INTEGER NOT NULL,text TEXT NOT NULL,method TEXT NOT NULL,
                revision INTEGER NOT NULL,created_at REAL NOT NULL,
                UNIQUE(scope,from_seq,to_seq));
              CREATE TABLE IF NOT EXISTS imports(
                scope TEXT NOT NULL,source TEXT NOT NULL,digest TEXT NOT NULL,
                PRIMARY KEY(scope,source));
              CREATE TABLE IF NOT EXISTS command_receipts(
                scope TEXT NOT NULL,operation_id TEXT NOT NULL,request_hash TEXT NOT NULL,
                result TEXT NOT NULL,PRIMARY KEY(scope,operation_id));
              CREATE TABLE IF NOT EXISTS documents(
                id INTEGER PRIMARY KEY,scope TEXT NOT NULL,name TEXT NOT NULL,
                revision INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,
                digest TEXT NOT NULL,UNIQUE(scope,name));
              CREATE TABLE IF NOT EXISTS document_versions(
                document_id INTEGER NOT NULL,revision INTEGER NOT NULL,text TEXT NOT NULL,
                digest TEXT NOT NULL,created_at REAL NOT NULL,
                PRIMARY KEY(document_id,revision));
              CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL,scope TEXT NOT NULL,
                line INTEGER NOT NULL,text TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS chunks_scope ON chunks(scope,document_id);
            """)
            # Serialize additive migration across gateway/CLI processes. Old rows
            # have unknown authors; never invent their identity during migration.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            for name in ("actor", "nickname", "reply_to"):
                if name not in columns:
                    db.execute(f"ALTER TABLE messages ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR REPLACE INTO memory_meta VALUES('schema_version','2')")
            db.commit()
            if use_fts:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text)")
                    self.fts = True
                    self._ensure_fts(db)
                except sqlite3.OperationalError:
                    self.fts = False
        for suffix in ("", "-wal", "-shm"):
            ensure_shared_perms(Path(str(self.path) + suffix))

    def _ensure_fts(self, db):
        state = db.execute("SELECT value FROM memory_meta WHERE key='fts_ready'").fetchone()
        if state and state[0] == '1':
            return
        db.execute("DELETE FROM chunks_fts")
        db.executemany("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (
            (r["id"], " ".join(_tokens(r["text"])))
            for r in db.execute("SELECT id,text FROM chunks").fetchall()))
        db.execute("INSERT OR REPLACE INTO memory_meta VALUES('fts_ready','1')")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_message(self, scope, event_key, text, direction="in", timestamp=None, *, actor="", nickname="", reply_to=""):
        scope = _scope(scope)
        if direction not in ("in", "out"):
            raise ValueError("Invalid direction")
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO messages(scope,event_key,direction,text,timestamp,actor,nickname,reply_to) VALUES(?,?,?,?,?,?,?,?)",
                       (scope, _text(event_key, 300), direction, _text(text, 100000), timestamp or time.time(),
                        str(actor)[:160], str(nickname)[:100], str(reply_to)[:300]))
            return db.execute("SELECT seq FROM messages WHERE scope=? AND event_key=?", (scope, event_key)).fetchone()[0]

    def messages(self, scope, limit=50, after_seq=0):
        self._legacy(scope)
        with self.connection() as db:
            rows = db.execute("SELECT * FROM messages WHERE scope=? AND seq>? ORDER BY seq DESC LIMIT ?",
                              (_scope(scope), int(after_seq), max(1, min(int(limit), 500)))).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_memory(self, scope, subject, predicate, value, *, source="manual", memory_id=None, expected_revision=None):
        scope, subject, predicate, value = _scope(scope), _text(subject, 300), _text(predicate, 300), _text(value)
        source = _text(source, 2000)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if memory_id:
                old = db.execute("SELECT * FROM memories WHERE id=? AND scope=? AND deleted=0", (memory_id, scope)).fetchone()
                if not old:
                    raise KeyError("Memory not found")
                if expected_revision != old["revision"]:
                    raise MemoryConflict("Memory changed; reload before saving")
                revision = old["revision"] + 1
                db.execute("UPDATE memories SET subject=?,predicate=?,value=?,source=?,revision=?,updated_at=? WHERE id=?",
                           (subject, predicate, value, source, revision, time.time(), memory_id))
            else:
                memory_id, revision = uuid.uuid4().hex, 1
                db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,0,?)",
                           (memory_id, scope, subject, predicate, value, source, revision, time.time()))
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def search_memories(self, scope, query="", limit=100):
        scope = _scope(scope)
        self._legacy(scope)
        with self.connection() as db:
            # instr is literal: %/_/[ have no wildcard or regexp meaning.
            return [dict(r) for r in db.execute(
                "SELECT * FROM memories WHERE scope=? AND deleted=0 AND instr(lower(subject||' '||predicate||' '||value),lower(?))>0 ORDER BY updated_at DESC LIMIT ?",
                (scope, query, max(1, min(int(limit), 500))))]

    def delete_memory(self, scope, memory_id, expected_revision):
        with self.connection() as db:
            cur = db.execute("UPDATE memories SET deleted=1,revision=revision+1,updated_at=? WHERE scope=? AND id=? AND deleted=0 AND revision=?",
                             (time.time(), _scope(scope), memory_id, expected_revision))
            if cur.rowcount != 1:
                raise MemoryConflict("Memory changed or no longer exists")

    def apply_command(self, scope, operation_id, kind, text=None, memory_id=None, actor=""):
        """Commit memory side effect and replay receipt in the SAME database transaction.

        The caller must authorize the actor for this scope before every invocation.
        """
        scope, operation_id = _scope(scope), _text(operation_id, 500)
        request_hash = hashlib.sha256(json.dumps([kind, text, memory_id, actor], ensure_ascii=False).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT request_hash,result FROM command_receipts WHERE scope=? AND operation_id=?", (scope, operation_id)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise MemoryConflict("Operation ID reused with different arguments")
                return json.loads(previous["result"])
            if kind in ("memory.add", "add"):
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,1,0,?)", (identifier, scope, "群约定", "记忆", _text(text), "command:" + str(actor)[:300], time.time()))
                result = dict(db.execute("SELECT * FROM memories WHERE id=?", (identifier,)).fetchone())
            elif kind in ("memory.delete", "delete"):
                row = db.execute("SELECT revision FROM memories WHERE scope=? AND id=? AND deleted=0", (scope, memory_id)).fetchone()
                if not row:
                    raise KeyError("Memory not found")
                db.execute("UPDATE memories SET deleted=1,revision=revision+1,updated_at=? WHERE scope=? AND id=?", (time.time(), scope, memory_id))
                result = {"deleted": True, "id": memory_id, "revision": row["revision"] + 1}
            else:
                raise ValueError("Unsupported memory command")
            db.execute("INSERT INTO command_receipts VALUES(?,?,?,?)", (scope, operation_id, request_hash, json.dumps(result, ensure_ascii=False)))
            return result

    def create_summary(self, scope, from_seq, to_seq, text=None, expected_revision=None):
        scope = _scope(scope)
        start, end = int(from_seq), int(to_seq)
        if start > end:
            raise ValueError("Invalid summary interval")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT seq,direction,text FROM messages WHERE scope=? AND seq BETWEEN ? AND ? ORDER BY seq", (scope, start, end)).fetchall()
            if not rows or rows[0]["seq"] != start or rows[-1]["seq"] != end:
                raise ValueError("Summary endpoints must belong to this conversation")
            # Deterministic extract, explicitly not advertised as an LLM summary.
            method = "manual" if text else "extractive"
            body = _text(text) if text else "\n".join(f"[{r['seq']} {r['direction']}] {r['text'][:180]}" for r in rows[-30:])[:6000]
            old = db.execute("SELECT id,revision FROM summaries WHERE scope=? AND from_seq=? AND to_seq=?", (scope, start, end)).fetchone()
            overlap = db.execute("SELECT 1 FROM summaries WHERE scope=? AND from_seq<=? AND to_seq>=? AND NOT(from_seq=? AND to_seq=?)", (scope, end, start, start, end)).fetchone()
            if overlap:
                raise MemoryConflict("Summary overlaps an existing published interval")
            if old and expected_revision != old["revision"]:
                raise MemoryConflict("Summary changed; reload before saving")
            identifier, revision = (old["id"], old["revision"] + 1) if old else (uuid.uuid4().hex, 1)
            db.execute("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?,?,?)", (identifier, scope, start, end, body, method, revision, time.time()))
            return dict(db.execute("SELECT * FROM summaries WHERE id=?", (identifier,)).fetchone())

    def summaries(self, scope):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM summaries WHERE scope=? ORDER BY to_seq DESC LIMIT 30", (_scope(scope),))]

    def context(self, scope, limit=25, max_chars=12000):
        budget = max(256, min(int(max_chars), 100000))
        result = {"scope": _scope(scope), "memories": [], "summaries": [], "messages": []}
        # Reserve space for current conversation; long-term facts cannot consume all.
        for kind, items, field, allowance in (
            ("memories", self.search_memories(scope, limit=20), "value", budget // 4),
            ("summaries", self.summaries(scope)[:2], "text", budget // 4),
            ("messages", list(reversed(self.messages(scope, limit))), "text", budget // 2)):
            for item in items:
                content = item[field]
                if allowance <= 0:
                    break
                clipped = content[:allowance]
                result[kind].append({**item, field: clipped, "truncated": len(clipped) < len(content)})
                allowance -= len(clipped)
        result["messages"].reverse()
        # Include identifiers, subjects and source metadata in the actual serialized budget.
        while len(json.dumps(result, ensure_ascii=False)) > budget:
            candidates = [(kind, item, field) for kind, field in (("memories", "value"), ("summaries", "text"), ("messages", "text")) for item in result[kind]]
            if not candidates:
                break
            kind, item, field = max(candidates, key=lambda v: len(v[1][v[2]]))
            excess = len(json.dumps(result, ensure_ascii=False)) - budget
            if len(item[field]) > excess + 20:
                item[field] = item[field][:-excess - 20]
                item["truncated"] = True
            else:
                result[kind].remove(item)
        return result

    def _legacy(self, scope):
        if self.auto_import and re.fullmatch(r"[0-9A-F]{32}", scope):
            folder = confined_path(self.path.parent / "groups", scope)
            if folder.is_dir():
                self.import_legacy(scope, confined_path(folder, "last-events.jsonl"), confined_path(folder, "notes.md"), confined_path(folder, "sent-log.jsonl"))

    def import_legacy(self, scope, event_path=None, notes_path=None, sent_path=None):
        """Repeatable import. Malformed/torn lines are counted, never erase newer edits."""
        scope = _scope(scope)
        counts = {"messages": 0, "skipped": 0, "notes": 0}
        for path, direction in ((event_path, "in"), (sent_path, "out")):
            if not path or not Path(path).exists():
                continue
            path = Path(path)
            source = "log:" + direction + ":" + str(path.resolve())
            with self.connection() as db:
                cursor = db.execute("SELECT digest FROM imports WHERE scope=? AND source=?", (scope, source)).fetchone()
            offset = int(cursor[0]) if cursor and cursor[0].isdigit() else 0
            if offset > path.stat().st_size:
                offset = 0
            with path.open("rb") as stream:
                stream.seek(offset)
                while True:
                    start = stream.tell()
                    raw = stream.readline(1_000_001)
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        # A currently-appended final record must be retried, not consumed.
                        if len(raw) <= 1_000_000:
                            stream.seek(start)
                            break
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(1_000_001)
                        counts["skipped"] += 1
                        continue
                    try:
                        line = raw.decode("utf-8")
                        event = json.loads(line)
                        data = event.get("d", event)
                        if not isinstance(data, dict):
                            raise ValueError("Invalid event")
                        external = data.get("group_openid") or data.get("group_id")
                        if external and str(external) != scope:
                            raise ValueError("Wrong group")
                        if direction == "out" and event.get("scope", "group") != "group":
                            raise ValueError("Cannot infer private recipient")
                        text = data.get("content") or data.get("raw_message") or data.get("text")
                        if not isinstance(text, str) or not text.strip():
                            raise ValueError("No text")
                        key = direction + ":" + str(data.get("id") or event.get("id") or hashlib.sha256(line.encode()).hexdigest())
                        with self.connection() as db:
                            cur = db.execute("INSERT OR IGNORE INTO messages(scope,event_key,direction,text,timestamp) VALUES(?,?,?,?,?)",
                                             (scope, key, direction, text[:100000], float(event.get("ts") or time.time())))
                            counts["messages"] += cur.rowcount
                    except (ValueError, TypeError, AttributeError, UnicodeError):
                        counts["skipped"] += 1
                with self.connection() as db:
                    db.execute("INSERT OR REPLACE INTO imports VALUES(?,?,?)", (scope, source, str(stream.tell())))
        if notes_path and Path(notes_path).is_file():
            with Path(notes_path).open(encoding="utf-8") as stream:
                content = stream.read(20000).strip()
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                imported = db.execute("SELECT 1 FROM imports WHERE scope=? AND source='legacy_notes'", (scope,)).fetchone()
                if content and not imported:
                    db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,1,0,?)", (uuid.uuid4().hex, scope, "群笔记", "legacy_notes", content, "legacy_notes", time.time()))
                    db.execute("INSERT INTO imports VALUES(?,?,?)", (scope, "legacy_notes", hashlib.sha256(content.encode()).hexdigest()))
                    counts["notes"] = 1
        return counts

    def _replace_chunks(self, db, document_id, scope, text):
        if self.fts:
            self._ensure_fts(db)
            db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE document_id=?)", (document_id,))
        else:
            db.execute("INSERT OR REPLACE INTO memory_meta VALUES('fts_ready','0')")
        db.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        lines = text.splitlines()
        for offset in range(0, len(lines), 12):
            block = "\n".join(lines[offset:offset + 12])
            # Long lines get bounded chunks too, preserving their starting line.
            for part in range(0, len(block), 3000):
                chunk = block[part:part + 3000]
                cur = db.execute("INSERT INTO chunks(document_id,scope,line,text) VALUES(?,?,?,?)", (document_id, scope, offset + 1, chunk))
                if self.fts:
                    db.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, " ".join(_tokens(chunk))))

    def index_document(self, scope, name, text):
        scope, name = _scope(scope), _text(name, 1000)
        if not isinstance(text, str) or len(text) > 8_000_000:
            raise ValueError("Document text exceeds 8 million characters")
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM documents WHERE scope=? AND name=?", (scope, name)).fetchone()
            if old and old["active"] and old["digest"] == digest:
                return dict(old)
            revision = old["revision"] + 1 if old else 1
            if old:
                identifier = old["id"]
                db.execute("UPDATE documents SET revision=?,active=1,digest=? WHERE id=?", (revision, digest, identifier))
            else:
                identifier = db.execute("INSERT INTO documents(scope,name,revision,active,digest) VALUES(?,?,1,1,?)", (scope, name, digest)).lastrowid
            db.execute("INSERT INTO document_versions VALUES(?,?,?,?,?)", (identifier, revision, text, digest, time.time()))
            self._replace_chunks(db, identifier, scope, text)
            return dict(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())

    def documents(self, scope):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM documents WHERE scope=? ORDER BY name", (_scope(scope),))]

    def delete_document(self, scope, name):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM documents WHERE scope=? AND name=?", (_scope(scope), name)).fetchone()
            if row:
                self._replace_chunks(db, row["id"], scope, "")
                db.execute("UPDATE documents SET active=0 WHERE id=?", (row["id"],))

    def rollback_document(self, scope, name, revision):
        with self.connection() as db:
            row = db.execute("SELECT v.text FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.scope=? AND d.name=? AND v.revision=?", (_scope(scope), name, int(revision))).fetchone()
        if not row:
            raise KeyError("Document version not found")
        return self.index_document(scope, name, row["text"])

    def search_documents(self, scope, query, limit=20):
        scope, query = _scope(scope), _text(query, 1000)
        limit = max(1, min(int(limit), 100))
        tokens = _tokens(query)[:64]
        with self.connection() as db:
            if self.fts and tokens and not re.search(r"[^\w\s\u3400-\u9fff]", query):
                state = db.execute("SELECT value FROM memory_meta WHERE key='fts_ready'").fetchone()
                if not state or state[0] != '1':
                    db.execute("BEGIN IMMEDIATE")
                    self._ensure_fts(db)
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                rows = db.execute("""SELECT c.*,d.name,d.revision,bm25(chunks_fts) AS score
                  FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN documents d ON d.id=c.document_id
                  WHERE chunks_fts MATCH ? AND c.scope=? AND d.active=1 ORDER BY score LIMIT ?""", (match, scope, limit)).fetchall()
            else:
                rows = db.execute("""SELECT c.*,d.name,d.revision,0 AS score FROM chunks c JOIN documents d ON d.id=c.document_id
                  WHERE c.scope=? AND d.active=1 AND instr(lower(c.text),lower(?))>0 ORDER BY c.id LIMIT ?""", (scope, query, limit)).fetchall()
        return [{"file": r["name"], "line": r["line"], "match": True, "text": r["text"], "revision": r["revision"], "score": r["score"]} for r in rows]

    def export_scope(self, scope):
        scope = _scope(scope)
        self._legacy(scope)
        with self.connection() as db:
            documents = [dict(r) for r in db.execute("SELECT d.name,d.revision,v.text FROM documents d JOIN document_versions v ON v.document_id=d.id AND v.revision=d.revision WHERE d.scope=? AND d.active=1", (scope,))]
            facts = [dict(r) for r in db.execute("SELECT * FROM memories WHERE scope=? AND deleted=0", (scope,))]
            summaries = [dict(r) for r in db.execute("SELECT * FROM summaries WHERE scope=? ORDER BY from_seq", (scope,))]
            messages = [dict(r) for r in db.execute("SELECT * FROM messages WHERE scope=? ORDER BY seq", (scope,))]
        return {"format": 1, "scope": scope, "memories": facts, "summaries": summaries, "messages": messages, "documents": documents}

    def restore_scope(self, scope, exported):
        """Non-destructive import: creates facts, republishes docs, never erases existing state."""
        if exported.get("format") != 1 or exported.get("scope") != _scope(scope):
            raise ValueError("Snapshot scope/format mismatch")
        facts, messages, summaries = exported.get("memories", []), exported.get("messages", []), exported.get("summaries", [])
        for fact in facts:
            _text(fact["subject"], 300); _text(fact["predicate"], 300); _text(fact["value"])
        for doc in exported.get("documents", []):
            _text(doc["name"], 1000)
            if not isinstance(doc["text"], str) or len(doc["text"]) > 8_000_000:
                raise ValueError("Invalid snapshot document")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            mapping = {}
            for msg in messages:
                if msg["direction"] not in ("in", "out"):
                    raise ValueError("Invalid snapshot direction")
                db.execute("INSERT OR IGNORE INTO messages(scope,event_key,direction,text,timestamp,actor,nickname,reply_to) VALUES(?,?,?,?,?,?,?,?)",
                           (scope, _text(msg["event_key"], 300), msg["direction"], _text(msg["text"], 100000), float(msg["timestamp"]),
                            str(msg.get("actor", ""))[:160], str(msg.get("nickname", ""))[:100], str(msg.get("reply_to", ""))[:300]))
                mapping[msg["seq"]] = db.execute("SELECT seq FROM messages WHERE scope=? AND event_key=?", (scope, msg["event_key"])).fetchone()[0]
            for fact in facts:
                identifier = _text(fact["id"], 100)
                existing = db.execute("SELECT scope FROM memories WHERE id=?", (identifier,)).fetchone()
                if existing and existing[0] != scope:
                    raise ValueError("Snapshot identity belongs to another scope")
                db.execute("INSERT OR IGNORE INTO memories VALUES(?,?,?,?,?,?,1,0,?)", (identifier, scope, fact["subject"], fact["predicate"], fact["value"], "restored_snapshot", time.time()))
            for summary in summaries:
                start, end = mapping.get(summary["from_seq"]), mapping.get(summary["to_seq"])
                if start is None or end is None:
                    raise ValueError("Snapshot is missing summary source messages")
                overlap = db.execute("SELECT 1 FROM summaries WHERE scope=? AND from_seq<=? AND to_seq>=?", (scope, end, start)).fetchone()
                if not overlap:
                    db.execute("INSERT INTO summaries VALUES(?,?,?,?,?,?,1,?)", (uuid.uuid4().hex, scope, start, end, _text(summary["text"]), "restored_" + str(summary.get("method", "manual"))[:30], time.time()))
        for doc in exported.get("documents", []):
            self.index_document(scope, doc["name"], doc["text"])
        return {"memories": len(facts), "messages": len(messages), "summaries": len(summaries), "documents": len(exported.get("documents", []))}
