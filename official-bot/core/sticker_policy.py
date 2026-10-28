"""Deterministic contextual reactions. Selection is advice, sending owns pacing."""
from __future__ import annotations

import hashlib

CHAINS = {"cool": ["clap", "doubt"], "laugh": ["rofl", "watch"],
          "cry": ["determined", "handshake"], "shock": ["watch", "surprise"],
          "thanks": ["handshake", "approve"], "doubt": ["think", "sweat"]}
SERIOUS = ("急救", "去世", "自杀", "住院", "报警", "受伤", "很痛苦", "别斗图", "不要表情")


def recommend(items, query: str, context: str = "", previous: str = "", seed: str = "", recent=()):
    current = query.lower().strip()
    nearby = context.lower()[-1200:]
    if any(word in current or word in nearby for word in SERIOUS):
        return {"ok": True, "should_send": False, "reason": "serious_context", "with_text": "先认真回应，避免用表情替代帮助"}
    candidates = []
    for item in items:
        tags = [str(tag).lower() for tag in item.get("tags", [])]
        score = sum(5 for tag in tags if tag and tag in current)
        score += sum(1 for tag in tags if len(tag) >= 2 and tag in nearby)
        if item.get("mood") in CHAINS.get(previous, []):
            score += 3
        if not score:
            continue
        if item.get("file") in recent:
            score -= 4
        # Ties stay stable across retries of the same source event.
        tie = hashlib.sha256((seed + query + str(item.get("file"))).encode()).hexdigest()
        candidates.append((score, tie, item))
    if not candidates:
        return {"ok": True, "should_send": False, "reason": "no_contextual_match"}
    score, tie, item = max(candidates, key=lambda row: (row[0], row[1]))
    return {"ok": True, "should_send": True, "file": item["file"], "mood": item.get("mood", ""),
            "template": item.get("template"), "caption_suggestions": item.get("caption_suggestions", [item.get("text", "")]),
            "license": item.get("license", "unverified_user_collection"), "source_url": item.get("source_url"),
            "reason": "context_and_reply_chain" if previous else "context_match", "score": score,
            "suggested_delay_ms": 700 + int(tie[:4], 16) % 1100,
            "with_text": "最多配一句补充；图已表达情绪时不再复述；使用 send_group.py --sticker 执行群级节奏限制"}
