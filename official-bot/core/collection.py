"""Fixed per-group collection window for ordinary QQ group messages.

The deadline is the first message's inbox arrival plus
``reply_policy.collection_window_seconds``. Later arrivals join that window
and do not move the deadline. @ mentions and slash commands never enter it.

This is not ``inbound_debounce`` (leading-edge suppression of a similar
natural topic) and not the legacy same-sender ``claim_followup`` wait.
The window stays disarmed when the seconds field is 0, the group is
disabled, or ``features.natural_reply`` is off, so only-@ groups keep the
previous path.
"""
from __future__ import annotations

import copy
import threading
import time

from .delivery import abort_if_not_a_database
from .reply_policy import settings


def armed_window_seconds(cfg: dict | None) -> float:
    """Seconds to hold ordinary messages, or 0 when the feature is off.

    Off unless the group is enabled, natural replies are enabled, and
    ``collection_window_seconds`` is a positive number. Invalid policy
    disarms the window instead of raising inside the gateway loop.
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled", True):
        return 0.0
    if not (cfg.get("features") or {}).get("natural_reply", False):
        return 0.0
    try:
        seconds = settings(cfg)["collection_window_seconds"]
    except (TypeError, ValueError):
        return 0.0
    if type(seconds) is bool or not isinstance(seconds, (int, float)):
        return 0.0
    if not seconds > 0:
        return 0.0
    return float(seconds)


def is_collection_bypass(payload: dict) -> bool:
    """@-bot and slash commands are forwarded alone, never held."""
    if payload.get("at_me"):
        return True
    text = payload.get("text_only")
    if text is None:
        text = payload.get("raw_message") or ""
    return str(text).strip().startswith("/")


def _speaker(payload: dict) -> str:
    nick = str((payload.get("sender") or {}).get("nickname") or "").strip()
    if nick:
        return nick
    member = str(payload.get("member_openid") or "").strip()
    return member[:8] if member else "群友"


def _body(payload: dict) -> str:
    text = payload.get("text_only")
    if text is None or not str(text).strip():
        text = payload.get("raw_message") or ""
    return str(text).strip()


def assemble_collection_payload(members: list[dict], window: dict) -> dict:
    """One webhook body. Idempotency uses the anchor; msg_id is the latest."""
    if not members:
        raise ValueError("empty collection")
    base = copy.deepcopy(members[0])
    last = members[-1]
    lines = []
    messages = []
    attachments = []
    segments = []
    for ordinal, payload in enumerate(members, start=1):
        speaker = _speaker(payload)
        body = _body(payload)
        lines.append(f"{speaker}: {body}" if body else f"{speaker}:")
        attachments.extend(payload.get("attachments") or [])
        segments.extend(payload.get("message") or [])
        messages.append({
            "ordinal": ordinal,
            "delivery_id": payload.get("delivery_id") or "",
            "msg_id": payload.get("msg_id") or "",
            "member_openid": payload.get("member_openid") or "",
            "user_id": payload.get("user_id"),
            "nickname": speaker,
            "text": body,
            "raw_message": payload.get("raw_message") or "",
            "time": payload.get("time"),
        })
    transcript = "\n".join(lines)
    base["text_only"] = transcript
    base["raw_message"] = transcript
    base["text"] = transcript
    base["attachments"] = attachments
    base["message"] = segments
    base["at_me"] = False
    # Reply target and dialogue actor follow the latest message. The batch
    # identity stays the first inbox row so a restart resends the same key.
    base["msg_id"] = last.get("msg_id") or base.get("msg_id") or ""
    base["message_id"] = last.get("message_id") or base.get("message_id") or ""
    base["member_openid"] = last.get("member_openid") or base.get("member_openid") or ""
    if last.get("user_id") is not None:
        base["user_id"] = last.get("user_id")
    if last.get("sender"):
        base["sender"] = copy.deepcopy(last.get("sender"))
    base["delivery_id"] = window["anchor_id"]
    base["collection"] = {
        "window_seconds": window["window_seconds"],
        "opened": window["opened"],
        "deadline": window["deadline"],
        "anchor_delivery_id": window["anchor_id"],
        "generation": window["generation"],
        "messages": messages,
    }
    return base


class CollectionScheduler:
    """Flushes due windows on their original deadline without occupying a group worker."""

    def __init__(self, store, flusher, clock=time.time, log=None, start=True):
        self.store = store
        self.flusher = flusher
        self.clock = clock
        self.log = log or (lambda _msg: None)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._started = False
        self._thread = threading.Thread(target=self._loop, name="collection-window", daemon=True)
        if start:
            self._thread.start()
            self._started = True

    def wake(self) -> None:
        """Interrupt the wait. Does not move any deadline."""
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._started:
            self._thread.join(timeout=2)

    def poll(self) -> None:
        now = self.clock()
        try:
            due = self.store.lease_due_collections(now)
        except Exception as exc:
            abort_if_not_a_database(exc)
            self.log(f"collection lease failed: {exc!r}")
            return
        for window in due:
            self._flush_window(window)

    def _flush_window(self, window: dict) -> None:
        scope, generation = window["scope"], window["generation"]
        try:
            if not window.get("members"):
                self.store.complete_collection(scope, generation, "skipped", "empty collection")
                return
            if not self.store.begin_collection_flush(scope, generation):
                return
            payload = assemble_collection_payload(window["members"], window)
            try:
                outcome = self.flusher(payload)
            except Exception as exc:
                self.log(f"collection flush failed: {exc!r}")
                outcome = "unknown"
            self.store.complete_collection(scope, generation, outcome or "skipped")
        except Exception as exc:
            abort_if_not_a_database(exc)
            self.log(f"collection window failed: {exc!r}")
            try:
                self.store.complete_collection(scope, generation, "unknown", str(exc)[:300])
            except Exception as finish_exc:
                abort_if_not_a_database(finish_exc)
                self.log("collection window could not be completed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as exc:
                abort_if_not_a_database(exc)
                self.log(f"collection poll failed: {exc!r}")
            if self._stop.is_set():
                return
            try:
                delay = self.store.seconds_until_collection_due(self.clock())
            except Exception as exc:
                abort_if_not_a_database(exc)
                self.log(f"collection timer failed: {exc!r}")
                delay = None
            if delay is None:
                timeout = 0.5
            elif delay <= 0:
                timeout = 0.01
            else:
                timeout = delay
            self._wake.wait(timeout)
            self._wake.clear()
