"""Recover legacy CLI reply hints from bounded, conversation-local history."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .files import confined_path, tail_jsonl
from .reply_policy import event_context

MAX_BYTES = 1024 * 1024
GROUP_MESSAGES = {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}


def _matched(record, group: str, message_id: str) -> dict | None:
    if not isinstance(record, dict):
        return None
    if isinstance(record.get("t"), str) and record["t"] in GROUP_MESSAGES:
        data = record.get("d")
        if not isinstance(data, dict) or data.get("id") != message_id:
            return None
        if data.get("group_openid", data.get("group_id")) != group:
            return None
        mentions = data.get("mentions") or []
        at_me = record["t"] == "GROUP_AT_MESSAGE_CREATE" or (isinstance(mentions, list) and any(
            isinstance(mention, dict) and mention.get("is_you") is True for mention in mentions))
        text = data.get("content", "")
        if not isinstance(text, str):
            return None
        return event_context({"group_openid": group, "msg_id": message_id, "at_me": at_me,
                              "text_only": re.sub(r"<@!?[^>]+>", "", text).strip()})
    # Missing scope/identity must never borrow the latest message's privileges.
    if record.get("group_openid") != group or record.get("post_type", "message") != "message":
        return None
    ids = [record[key] for key in ("msg_id", "message_id") if record.get(key)]
    if not ids or any(value != message_id for value in ids):
        return None
    context = record.get("reply_context")
    if isinstance(context, dict) and context.get("source_event_id", message_id) == message_id:
        # Reclassify old natural hints with today's follow-up rules, instead of
        # perpetuating yesterday's mistaken 120-second cooldown classification.
        text = context.get("topic_text")
        if not isinstance(text, str):
            text = record.get("text_only") or record.get("text") or record.get("raw_message") or ""
        if not isinstance(text, str):
            return None
        result = event_context({"group_openid": group, "msg_id": message_id,
                                "text_only": text, "at_me": record.get("at_me") is True or context.get("trigger") == "at"})
        if isinstance(context.get("trigger"), str) and context["trigger"] in {"at", "question", "command"}:
            result["trigger"] = context["trigger"]
        if isinstance(context.get("topic_key"), str):
            result["topic_key"] = context["topic_key"]
        return result
    if context is not None:
        return None
    text = record.get("text_only") or record.get("text") or record.get("raw_message") or ""
    if not isinstance(text, str):
        return None
    return event_context({**record, "text_only": text, "at_me": record.get("at_me") is True})


def recover(directory: Path, group: str, message_id: str) -> dict | None:
    """No network, directory scan, or unbounded log read; absent hints stay legacy."""
    try:
        with confined_path(directory, "last-forward.json").open("rb") as stream:
            content = stream.read(MAX_BYTES + 1)
        if len(content) <= MAX_BYTES:
            context = _matched(json.loads(content), group, message_id)
            if context is not None:
                return context
    except (OSError, ValueError):
        pass
    try:
        records = tail_jsonl(confined_path(directory, "last-events.jsonl"), count=1000, max_bytes=MAX_BYTES)
    except (OSError, ValueError):
        return None
    for record in reversed(records):
        context = _matched(record, group, message_id)
        if context is not None:
            return context
    return None
