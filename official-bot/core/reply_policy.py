"""Shared reply fingerprints and transaction-level anti-collision rules."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

DEFAULTS = {"natural_cooldown": 120, "question_cooldown": 10,
            "content_window": 300, "topic_window": 600, "inbound_debounce": 12,
            # Fixed collection window. 0 keeps the legacy same-sender follow-up.
            # This is not inbound_debounce: that knob suppresses later naturals
            # after an immediate forward, and it must not drop a collected batch.
            "collection_window_seconds": 0,
            # Unsolicited same-burst guard. 0 leaves every group on today's path.
            # It is not natural_cooldown and does not apply to @ or new questions.
            "natural_repeat_seconds": 0,
            "sticker_cooldown": 5, "sticker_hourly_limit": 60}
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
    cleaned = re.sub(r"https?://\S+|<faceType=[^>]*>|\[(?:自定义表情包|图片|语音|视频|附件)[^\]]*\]", " ", str(text or ""))
    words = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]+", unicodedata.normalize("NFKC", cleaned).lower())
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


def canonical_group(scope: str) -> str:
    """Group openid for both gateway scopes and ``/v2/groups/<id>`` send scopes."""
    text = str(scope or "")
    marker = "/groups/"
    if marker in text:
        return text.split(marker, 1)[1].split("/", 1)[0]
    if text.startswith("group:"):
        return text.split(":", 1)[1]
    return text


def topic_anchors(text: str) -> list[str]:
    """Distinctive runs only: a CJK span or latin token of at least 3 characters.

    This is not a paraphrase embedding. Two wordings of one topic match only
    when one of those runs contains the other, or the existing token overlap
    rule already calls them the same topic.
    """
    cleaned = _clean_text(text)
    folded = unicodedata.normalize("NFKC", cleaned).lower()
    found = re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u3400-\u9fff]{3,}", folded)
    anchors = []
    seen = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            anchors.append(item)
        if len(anchors) >= 40:
            break
    return anchors


def related_topic(left_tokens, left_anchors, right_tokens, right_anchors) -> bool:
    left, right = list(left_anchors or []), list(right_anchors or [])
    if set(left) & set(right):
        return True
    for anchor in left:
        if len(anchor) >= 3 and any(anchor != other and (anchor in other or other in anchor) for other in right):
            return True
    return same_topic(left_tokens, right_tokens)


def weak_natural_context(text: str, attachments=None) -> bool:
    """Image-only or very short non-question chatter, not a new topic.

    Folded text of 4 or fewer characters is follow-up context. A short
    wording that still matches the question/request rules is not weak.
    """
    raw = str(text or "")
    cleaned = _clean_text(raw).strip()
    has_media = bool(attachments) or raw.strip() != cleaned
    if not cleaned:
        return bool(has_media or not raw.strip())
    if QUESTION.search(cleaned) or REQUEST.search(cleaned):
        return False
    if not topic_tokens(cleaned) and has_media:
        return True
    return len(normalized(cleaned)) <= 4


def repeat_window_seconds(config) -> float:
    """Opt-in seconds, or 0 when the guard is off or the policy is invalid."""
    try:
        seconds = settings(config or {})["natural_repeat_seconds"]
    except (TypeError, ValueError):
        return 0.0
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not seconds > 0:
        return 0.0
    return float(seconds)


def wake_signature(payload: dict) -> dict:
    """One natural wake: batch sources stay together so later sends share a claim."""
    collection = payload.get("collection") if isinstance(payload.get("collection"), dict) else None
    if collection:
        parts = [str(item.get("text") or "") for item in (collection.get("messages") or [])]
        sources = []
        for item in collection.get("messages") or []:
            for key in ("msg_id", "delivery_id"):
                if item.get(key):
                    sources.append(str(item[key]))
        text = "\n".join(parts)
        question = any(event_context({"text_only": part})["trigger"] == "question" for part in parts)
        weak = bool(parts) and all(weak_natural_context(part) for part in parts)
    else:
        text = payload.get("text_only")
        if text is None:
            text = payload.get("raw_message") or ""
        text = str(text)
        sources = []
        question = False
        weak = weak_natural_context(text, payload.get("attachments") or [])
    for key in ("msg_id", "message_id", "delivery_id"):
        if payload.get(key):
            sources.append(str(payload[key]))
    sources = list(dict.fromkeys(item for item in sources if item))
    batch_id = str(payload.get("delivery_id") or (collection or {}).get("anchor_delivery_id") or (sources[0] if sources else ""))
    return {"tokens": topic_tokens(text), "anchors": topic_anchors(text), "sources": sources,
            "weak": weak and not question, "question": question, "batch_id": batch_id}


def _clean_text(text: str) -> str:
    return re.sub(r"https?://\S+|<faceType=[^>]*>|\[(?:自定义表情包|图片|语音|视频|附件)[^\]]*\]", " ", str(text or "")).strip()


def event_context(event: dict) -> dict:
    raw = str(event.get("text_only") or event.get("text") or event.get("raw_message") or "")
    text = _clean_text(raw)
    trigger = "at" if event.get("at_me") else "command" if text.startswith("/") else "question" if (
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
            "topic_tokens": tokens, "anchors": topic_anchors(topic_text),
            "weak_context": weak_natural_context(topic_text), "sticker": bool(sticker)}


def natural_repeat_denial(db, scope, action_id, meta, limits, now) -> str | None:
    """Second unsolicited natural answer for one burst or one lexical topic.

    Questions, @, commands and stickers never reach this. Unknown/failed
    replies do not hold the guard. A wording that shares no distinctive run
    with the first topic is a different topic and is allowed.
    """
    window = float(limits.get("natural_repeat_seconds") or 0)
    if window <= 0:
        return None
    source = str(meta.get("source") or "")
    group = canonical_group(scope)
    leases = db.execute("SELECT sources FROM natural_leases WHERE scope=? AND expires>?", (group, now)).fetchall()
    for lease in leases:
        held = [str(item) for item in json.loads(lease["sources"]) if item]
        if not source or source not in held:
            continue
        mates = [item for item in held if item != source]
        if not mates:
            continue
        placeholders = ",".join("?" * len(mates))
        live = db.execute(f"""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
            WHERE r.scope=? AND r.action_id!=? AND json_extract(r.metadata,'$.source') IN ({placeholders})
            AND json_extract(r.metadata,'$.trigger')='natural'
            AND COALESCE(json_extract(r.metadata,'$.sticker'), 0)=0
            AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
            (scope, action_id, *mates)).fetchone()
        if live:
            return "topic_recently_answered"
    rows = db.execute("""SELECT r.metadata FROM reply_claims r JOIN outbox o ON o.id=r.action_id
        WHERE r.scope=? AND r.created>? AND r.action_id!=?
        AND json_extract(r.metadata,'$.trigger')='natural'
        AND COALESCE(json_extract(r.metadata,'$.sticker'), 0)=0
        AND o.status IN ('pending','inflight','sent','partial')
        ORDER BY r.created DESC LIMIT 40""", (scope, now - window, action_id)).fetchall()
    if not rows:
        return None
    if meta.get("weak_context"):
        return "topic_recently_answered"
    for row in rows:
        other = json.loads(row["metadata"])
        if other.get("sticker"):
            continue
        if related_topic(meta.get("topic_tokens") or [], meta.get("anchors") or [],
                         other.get("topic_tokens") or [], other.get("anchors") or []):
            return "topic_recently_answered"
    return None


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
        if meta["trigger"] == "natural" and not meta["sticker"] and limits["natural_cooldown"] > 0 and db.execute("""SELECT 1 FROM outbox WHERE scope=? AND id!=?
                AND updated>? AND status IN ('pending','inflight','sent','partial') LIMIT 1""",
                (scope, action_id, now - limits["natural_cooldown"])).fetchone():
            return "natural_cooldown"
        if meta["sticker"]:
            if limits["sticker_cooldown"] > 0 and db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                    WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.sticker')=1
                    AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
                    (scope, now - limits["sticker_cooldown"], action_id)).fetchone():
                return "sticker_cooldown"
            if limits["sticker_hourly_limit"] > 0:
                count = db.execute("""SELECT COUNT(*) FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                    WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.sticker')=1
                    AND o.status IN ('pending','inflight','sent','partial')""", (scope, now - 3600, action_id)).fetchone()[0]
                if count >= limits["sticker_hourly_limit"]:
                    return "sticker_hourly_quota"
        if meta["topic_key"] and meta["trigger"] in {"natural", "question"}:
            window = limits["topic_window"] if meta["trigger"] == "natural" else limits["question_cooldown"]
            if window > 0 and db.execute("""SELECT 1 FROM reply_claims r JOIN outbox o ON o.id=r.action_id
                    WHERE r.scope=? AND r.created>? AND r.action_id!=? AND json_extract(r.metadata,'$.topic_key')=?
                    AND o.status IN ('pending','inflight','sent','partial') LIMIT 1""",
                    (scope, now - window, action_id, meta["topic_key"])).fetchone():
                return "topic_recently_answered" if meta["trigger"] == "natural" else "question_topic_debounce"
        if limits.get("natural_repeat_seconds", 0) > 0 and meta["trigger"] == "natural" and not meta["sticker"]:
            reason = natural_repeat_denial(db, scope, action_id, meta, limits, now)
            if reason:
                return reason
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
        if meta["trigger"] == "natural" and not meta["sticker"]:
            if limits["natural_cooldown"] > 0 and age < limits["natural_cooldown"]:
                return "natural_cooldown"
            if limits["topic_window"] > 0 and age < limits["topic_window"] and (
                (meta["topic_key"] and meta["topic_key"] == other.get("topic_key"))
                or same_topic(meta["topic_tokens"], other.get("topic_tokens", []))):
                return "topic_recently_answered"
    return None
