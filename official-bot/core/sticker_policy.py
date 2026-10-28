"""Deterministic contextual reactions. Selection is advice, sending owns pacing."""
from __future__ import annotations

import hashlib
import re

CHAINS = {"cool": ["clap", "doubt"], "laugh": ["rofl", "watch"],
          "cry": ["determined", "handshake"], "shock": ["watch", "surprise"],
          "thanks": ["handshake", "approve"], "doubt": ["think", "sweat"]}
SERIOUS = ("急救", "去世", "自杀", "住院", "报警", "受伤", "很痛苦", "别斗图", "不要表情")
MOODS = (
    ("laugh", ("搞笑", "哈哈", "笑死", "大笑", "绷不住")),
    ("doubt", ("质疑", "怀疑", "鄙视", "就这")),
    ("watch", ("吃瓜", "围观", "暗中观察")),
    ("clap", ("鼓掌", "牛逼")),
    ("approve", ("大佬", "抱拳", "佩服", "点赞")),
    ("shock", ("震惊", "卧槽", "惊讶")),
    ("cry", ("破防", "裂开", "小丑", "哭")),
    ("sweat", ("汗", "尴尬", "摸鱼")),
    ("doubt", ("二哈",)),
    ("cat", ("猫", "喵", "cat")),
    ("dog", ("狗", "汪", "柴犬", "cheems", "dog")),
)


def _tags(item):
    value = item.get("tags", [])
    return [value.lower()] if isinstance(value, str) else [str(tag).lower() for tag in value] if isinstance(value, list) else []


def infer_mood(item) -> str:
    """Explicit moods win; local caption/tag emotion wins over character category."""
    if isinstance(item.get("mood"), str) and item["mood"].strip():
        return item["mood"].strip().lower()
    description = " ".join([str(item.get("text") or "").lower(), *_tags(item)])
    # Emotion first across caption/tags, then categories; animal fallbacks last.
    category = str(item.get("category") or "").lower()
    for haystack, mappings in ((description, MOODS[:8]), (category, MOODS[:8]),
                              (description + " " + category, MOODS[8:])):
        for mood, words in mappings:
            if any((re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", haystack)
                    if word.isascii() else word in haystack) for word in words):
                return mood
    return ""


def recommend(items, query: str, context: str = "", previous: str = "", seed: str = "", recent=()):
    current = query.lower().strip()
    nearby = context.lower()[-1200:]
    if any(word in current or word in nearby for word in SERIOUS):
        return {"ok": True, "should_send": False, "reason": "serious_context", "with_text": "先认真回应，避免用表情替代帮助"}
    candidates = []
    query_mood = infer_mood({"text": query})
    for item in items:
        tags = _tags(item)
        mood = infer_mood(item)
        score = sum(5 for tag in tags if tag and tag in current)
        score += sum(1 for tag in tags if len(tag) >= 2 and tag in nearby)
        if mood and mood == query_mood:
            score += 3
        if mood in CHAINS.get(previous, []):
            score += 3
        if not score:
            continue
        # Only boost relevant local assets; a native category alone is no match.
        if item.get("category") != "openmoji" and item.get("source") != "openmoji-reactions":
            score += 2
        if item.get("file") in recent:
            score -= 4
        # Ties stay stable across retries of the same source event.
        tie = hashlib.sha256((seed + query + str(item.get("file"))).encode()).hexdigest()
        candidates.append((score, tie, item))
    if not candidates:
        return {"ok": True, "should_send": False, "reason": "no_contextual_match"}
    score, tie, item = max(candidates, key=lambda row: (row[0], row[1]))
    return {"ok": True, "should_send": True, "file": item["file"], "mood": infer_mood(item),
            "template": item.get("template"), "caption_suggestions": item.get("caption_suggestions", [item.get("text", "")]),
            "license": item.get("license", "unverified_user_collection"), "source_url": item.get("source_url"),
            "reason": "context_and_reply_chain" if previous else "context_match", "score": score,
            "suggested_delay_ms": 700 + int(tie[:4], 16) % 1100,
            "with_text": "最多配一句补充；图已表达情绪时不再复述；使用 send_group.py --sticker 执行群级节奏限制"}
