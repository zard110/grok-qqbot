"""Shared reply fingerprints and transaction-level anti-collision rules."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

DEFAULTS = {"natural_cooldown": 120, "question_cooldown": 10,
            "content_window": 300, "topic_window": 600, "inbound_debounce": 12,
            "sticker_cooldown": 45, "sticker_hourly_limit": 6}
QUESTION = re.compile(r"[?？]|请问|怎么|如何|为什么|有没有|谁知道|哪个|哪些|多少|能不能|可不可以|可以吗|行不行|是不是|要不要")
REQUEST = re.compile(r"对比|比较(?!好|多|少|大|小|高|低|快|慢|喜欢|满意)|看看|评测|打(?:个)?分|分析|推荐|试试|再来|解释|说说|讲讲")


def settings(config=None):
    value = {**DEFAULTS, **((config or {}).get("reply_policy") or {})}
    for key, number in value.items():
        if key not in DEFAULTS or type(number) not in (int, float) or not 0 <= number <= 86400:
            raise ValueError("Invalid reply policy limit")
    return value


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text)).lower()
    text = re.sub(r"<qqbot-at-user\b[^>]*>|<@[^>]+>", "", text)
    return "".join(re.findall(r"[\w\u3400-\u9fff]+", text, flags=re.UNICODE))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def topic_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", unicodedata.normalize("NFKC", text).lower())
    tokens = set()
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            tokens.update(word[n:n + 2] for n in range(len(word) - 1))
        else:
            tokens.add(word)
    return sorted(tokens)[:80]


def same_topic(left, right) -> bool:
    a, b = set(left), set(right)
    return bool(a and b and len(a & b) >= 2 and len(a & b) / len(a | b) >= .65)


def event_context(event: dict) -> dict:
    text = str(event.get("text_only") or event.get("text") or event.get("raw_message") or "")
    trigger = "at" if event.get("at_me") else "command" if text.strip().startswith("/") else "question" if (
        QUESTION.search(text) or REQUEST.search(text)) else "natural"
    source = str(event.get("msg_id") or event.get("message_id") or event.get("event_id") or event.get("delivery_id") or "")
    tokens = topic_tokens(text)
    return {"source_event_id": source, "trigger": trigger, "topic_key": digest(tokens) if tokens else "",
            "topic_text": text[:1000], "action_id": "reply:" + digest([event.get("group_openid"), source]),
            "instructions": "One logical reply per source_event_id; pass --source-event-id, --trigger and --topic-text to send_group.py. Do not use --force for routine retries."}


def metadata(trigger, source, text, media, *, topic_text="", topic_key="", sticker=False):
    if trigger not in {"legacy", "natural", "at", "question", "command", "continuation"}:
        raise ValueError("Unknown reply trigger")
    tokens = topic_tokens(topic_text)
    return {"trigger": trigger, "source": source or "", "content_hash": digest([normalized(text), media]),
            "topic_key": topic_key or (digest(tokens) if tokens else ""),
            "topic_tokens": tokens, "sticker": bool(sticker)}


def denial(db, scope, action_id, meta, limits, now, force=False):
    """Called inside the outbox reservation transaction, never after network I/O."""
    if meta["source"]:
        claimed = db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
            WHERE r.scope=? AND json_extract(r.metadata,'$.source')=? AND r.action_id!=?
            AND o.status IN ('pending','inflight','sent','partial','unknown') LIMIT 1""",
            (scope, meta["source"], action_id)).fetchone()
        if claimed:
            return "source_event_already_claimed"
    duplicate = db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
        WHERE r.scope=? AND r.created>? AND r.action_id!=?
        AND json_extract(r.metadata,'$.content_hash')=?
        AND o.status IN ('pending','inflight','sent','partial','unknown')
        AND (? IN ('natural','legacy') OR json_extract(r.metadata,'$.source')=?)
        AND NOT (? AND o.status='unknown') LIMIT 1""",
        (scope, now - limits["content_window"], action_id, meta["content_hash"], meta["trigger"], meta["source"], force)).fetchone()
    if duplicate:
        return "duplicate_content"
    if not force:
        if meta["trigger"] == "natural" and db.execute("""SELECT 1 FROM outbox WHERE scope=? AND id!=?
                AND updated>? AND status IN ('inflight','sent','partial') LIMIT 1""",
                (scope, action_id, now - limits["natural_cooldown"])).fetchone():
            return "natural_cooldown"
        if meta["sticker"]:
            if db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                    WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.sticker')=1
                    AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
                    (scope, now - limits["sticker_cooldown"], action_id)).fetchone():
                return "sticker_cooldown"
            count = db.execute("""SELECT COUNT(*) FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.sticker')=1
                AND o.status IN ('pending','inflight','sent','partial')""", (scope, now - 3600, action_id)).fetchone()[0]
            if count >= limits["sticker_hourly_limit"]:
                return "sticker_hourly_quota"
        if meta["topic_key"] and meta["trigger"] in {"natural", "question"}:
            window = limits["topic_window"] if meta["trigger"] == "natural" else limits["question_cooldown"]
            if db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                    WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.topic_key')=?
                    AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
                    (scope, now - window, action_id, meta["topic_key"])).fetchone():
                return "topic_recently_answered" if meta["trigger"] == "natural" else "question_topic_debounce"
    rows = db.execute("""SELECT r.*,o.status FROM reply_claims r JOIN outbox o ON o.id=r.action_id
        WHERE r.scope=? AND r.created>? AND r.action_id!=? AND o.status IN
        ('pending','inflight','sent','partial','unknown') ORDER BY r.created DESC LIMIT 500""",
        (scope, now - max(3600, limits["topic_window"], limits["content_window"]), action_id)).fetchall()
    for row in rows:
        other = json.loads(row["metadata"])
        age = now - row["created"]
        # Failed/unknown do not consume ordinary cooldown. Explicit force keeps
        # the production escape hatch, but never steals another live source claim.
        if row["status"] == "unknown" or force:
            continue
        if meta["trigger"] == "natural":
            if age < limits["natural_cooldown"]:
                return "natural_cooldown"
            if age < limits["topic_window"] and (
                (meta["topic_key"] and meta["topic_key"] == other.get("topic_key"))
                or same_topic(meta["topic_tokens"], other.get("topic_tokens", []))):
                return "topic_recently_answered"
    return None
