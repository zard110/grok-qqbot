#!/usr/bin/env python3
"""Send a group (or C2C) message via official QQ bot MaxShenBi.

Text / markdown / image / voice / video / file, quote-reply, keyboard buttons, recall.
See USAGE.md.
"""
from __future__ import annotations

import argparse
import hashlib
import base64
import json
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))
from core.settings import DATA_DIR
from core.files import file_lock, append_jsonl, confined_path, tail_jsonl
from core.qq_client import client as qq_client, QQTransportError, classify_status
from core.media import read_local, image_info, validate_url, safe_open
from core.delivery import DeliveryStore, ReplySuppressed, stable_id
from core import message_format, reply_policy, conversation
DIR = DATA_DIR
import groups as G  # noqa: E402
API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# per-target anti-duplicate files (set in main(); Zwift's are also reachable via the old top-level symlinks)
SENT_LOG = str(DIR / "sent-log.jsonl")
LOCK = str(DIR / ".send.lock")


def set_target_files(goid: str | None, user_openid: str | None) -> None:
    global SENT_LOG, LOCK
    d = confined_path(G.C2C_DIR, user_openid) if user_openid else G.group_dir(goid)
    d.mkdir(parents=True, exist_ok=True)
    SENT_LOG = str(d / "sent-log.jsonl")
    LOCK = str(d / ".send.lock")
WINDOW = 120
FILE_TYPES = {"image": 1, "video": 2, "voice": 3, "file": 4}
_token = ""
_token_at = 0.0


def rd(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8").strip()


def get_token() -> str:
    return qq_client.token()


def _parse(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def api(method: str, path: str, payload: dict | None = None, timeout: int = 60) -> tuple[int, object]:
    return qq_client.request(method, path, payload, timeout)


def target_path(group_openid: str | None, user_openid: str | None) -> str:
    import re
    target = user_openid or group_openid or G.default_group_openid()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target):
        raise ValueError("Invalid target identity")
    if user_openid:
        return f"/v2/users/{target}"
    return f"/v2/groups/{target}"


def log_sent(entry: dict) -> None:
    append_jsonl(Path(SENT_LOG), entry)


def recent_sends(window: int = WINDOW):
    now = time.time()
    return [row for row in tail_jsonl(SENT_LOG)
            if row.get("scope", "group") == "group" and row.get("kind") != "recall"
            and isinstance(row.get("ts"), (int, float)) and now - row["ts"] <= window]


def _to_silk(path: str) -> str:
    """Convert audio to silk via pilk (only used if mp3/wav upload is rejected)."""
    try:
        import pilk  # type: ignore
    except ImportError:
        raise RuntimeError("Voice conversion requires preinstalled pilk and ffmpeg") from None
    pcm_fd, pcm = tempfile.mkstemp(suffix=".pcm")
    out_fd, out = tempfile.mkstemp(suffix=".silk")
    os.close(pcm_fd)
    os.close(out_fd)
    try:
        subprocess.check_call(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "24000", pcm], timeout=30
        )
        pilk.encode(pcm, out, pcm_rate=24000, tencent=True)
    except Exception:
        Path(out).unlink(missing_ok=True)
        raise
    finally:
        Path(pcm).unlink(missing_ok=True)
    return out



def upload_markdown_image(path_str: str) -> tuple[str, int, int]:
    """Upload local image to /v2/media-images and return (qqbot-media:// url, width, height)."""
    import hashlib
    from PIL import Image
    fp = Path(path_str)
    data = read_local(fp)
    w, h, fmt = image_info(data)
    mime = f"image/{'jpeg' if fmt in ('jpg', 'jpeg') else fmt}"
    md5 = hashlib.md5(data).hexdigest()
    sha1 = hashlib.sha1(data).hexdigest()
    c1, prep = api("POST", "/v2/media-images/prepare", {
        "file_name": fp.name,
        "file_size": len(data),
        "content_type": mime,
        "md5": md5,
        "sha1": sha1,
    })
    if c1 >= 300 or not isinstance(prep, dict) or "presigned_url" not in prep:
        raise RuntimeError(f"media-images/prepare failed: {c1} {prep}")
    with safe_open(prep["presigned_url"], data=data, method="PUT", headers={"Content-Type": mime}, timeout=30) as r:
        if r.status >= 300:
            raise RuntimeError("Presigned media upload failed")
        etag = (r.headers.get("ETag") or "").strip('"')
    c2, comp = api("POST", "/v2/media-images/complete", {"upload_id": prep["upload_id"], "md5": etag or md5})
    if c2 >= 300 or not isinstance(comp, dict) or "url" not in comp:
        raise RuntimeError(f"media-images/complete failed: {c2} {comp}")
    return comp["url"], w, h


def upload(kind: str, src: str, base: str) -> dict:
    """Upload media; returns the API response body (contains file_info). Never sends a message."""
    ft = FILE_TYPES[kind]
    body: dict = {"file_type": ft, "srv_send_msg": False}
    if src.startswith("http://") or src.startswith("https://"):
        body["url"] = validate_url(src)
    else:
        p = Path(src)
        if not p.is_file():
            raise SystemExit(f"file not found: {src}")
        data = read_local(p)
        body["file_data"] = base64.b64encode(data).decode()
        body["file_name"] = p.name
    code, resp = api("POST", f"{base}/files", body, timeout=120)
    if code >= 300 or not isinstance(resp, dict) or not resp.get("file_info"):
        if kind == "voice" and "file_data" in body and not src.lower().endswith(".silk"):
            # fallback: convert to silk and retry once
            silk = _to_silk(src)
            try:
                body["file_data"] = base64.b64encode(read_local(silk)).decode()
            finally:
                Path(silk).unlink(missing_ok=True)
            body["file_name"] = Path(src).stem + ".silk"
            code, resp = api("POST", f"{base}/files", body, timeout=120)
            if code < 300 and isinstance(resp, dict) and resp.get("file_info"):
                resp["converted_to_silk"] = True
                return resp
        print(json.dumps({"error": True, "stage": "upload", "kind": kind, "http_status": code, "body": resp}, ensure_ascii=False, indent=2))
        raise RuntimeError("Media upload rejected")
    return resp


def _seq() -> int:
    return int(time.time() * 1000) % 1_000_000


def post_message(base: str, payload: dict, *, msg_id, event_id, reply_to, seq_offset: int = 0) -> dict:
    if msg_id:
        payload["msg_id"] = msg_id
    if event_id:
        payload["event_id"] = event_id
    if msg_id or event_id:
        payload.setdefault("msg_seq", _seq() + seq_offset)
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to, "ignore_get_message_error": True}
    code, resp = api("POST", f"{base}/messages", payload)
    return {"http_status": code, "body": resp, "ok": code < 300 and (not isinstance(resp, dict) or resp.get("code", 0) in (0, "0", None))}


def main() -> None:
    ap = argparse.ArgumentParser(description="Send message as MaxShenBi (official QQ bot). See USAGE.md")
    ap.add_argument("--text", help="plain text (msg_type=0)")
    ap.add_argument("--markdown", help="markdown content (msg_type=2)")
    ap.add_argument("--image", help="image path (png/jpg, <=5MB) or http(s) URL")
    ap.add_argument("--voice", help="voice path (mp3/wav/silk) or URL")
    ap.add_argument("--video", help="video path (mp4, <=5MB local) or URL")
    ap.add_argument("--file", help="any file path (<=5MB local) or URL")
    ap.add_argument("--keyboard", help="keyboard JSON (string or @path) — {\"rows\":[...]} or {\"id\":...}; needs --markdown")
    ap.add_argument("--reply-to", dest="reply_to", help="quote-reply: REFIDX_... (payload field ref_idx) of the message to quote")
    ap.add_argument("--recall", help="recall (delete) one of OUR messages by message id (within 2 min)")
    ap.add_argument("--msg-id", dest="msg_id", help="event message id for passive reply")
    ap.add_argument("--event-id", dest="event_id", help="event id for passive reply to an event (INTERACTION_CREATE / GROUP_ADD_ROBOT / GROUP_MSG_RECEIVE)")
    ap.add_argument("--msg-seq", dest="msg_seq", type=int, help="optional msg_seq")
    ap.add_argument("--action-id", help="stable action id; --force retries only failed/unknown with identical content")
    ap.add_argument("--group", help="target group: alias (e.g. zwift), name, QQ number or group_openid. Default: Zwift练功房")
    ap.add_argument("--group-openid", dest="group_openid", help="same as --group (kept for old commands)")
    ap.add_argument("--feature", help="refuse (exit 4) unless this feature is on in the group's config.json, e.g. daily_ai_news")
    ap.add_argument("--plugin-id", help="enforce the originating plugin's message.send capability")
    ap.add_argument("--user-openid", dest="user_openid", help="send to a C2C (private) user instead of the group")
    ap.add_argument("--force", action="store_true", help="send even if MaxShenBi already sent within the last 120s")
    ap.add_argument("--recent", action="store_true", help="print MaxShenBi sends in the last 120s and exit")
    ap.add_argument("--upload-only", action="store_true", help="only upload media and print file_info (sends nothing)")
    ap.add_argument("--dry-run", action="store_true", help="print the request payload(s) and exit (sends nothing; no upload)")
    ap.add_argument("--dry-credentials", action="store_true", help="only verify token + group_openid (no send)")
    ap.add_argument("--format", choices=["auto", "plain", "markdown"], help="presentation mode; auto uses group-verified capabilities")
    ap.add_argument("--max-chars", type=int, help="part size 120..2000 (default 1000)")
    ap.add_argument("--max-parts", type=int, help="maximum parts 1..5 (default 3); never silently truncates")
    ap.add_argument("--trigger", choices=["legacy", "natural", "at", "question", "command"], help="infer from matching --msg-id history when omitted; otherwise legacy")
    ap.add_argument("--source-event-id", help="stable source identity shared by all routines handling an event")
    ap.add_argument("--topic-text", default="", help="incoming topic, not the generated reply")
    ap.add_argument("--topic-key", default="", help="stable topic id when supplied by the bridge")
    ap.add_argument("--sticker", action="store_true", help="enforce sticker pacing and a short natural delay")
    ap.add_argument("--decision", help="model reply decision JSON or @path; answer/clarify/ignore; requires a recorded source")
    ap.add_argument("--choice-buttons", action="store_true", help="render clarification choices as buttons when group capabilities are verified")
    args = ap.parse_args()

    goid = None if args.user_openid else G.resolve(args.group or args.group_openid)
    set_target_files(goid, args.user_openid)
    if args.recent:
        print(json.dumps(recent_sends(), ensure_ascii=False, indent=2))
        return
    if args.dry_credentials and args.dry_run:
        print(json.dumps({"dry_run": True, "credential_check": "not_executed"}))
        return
    if args.dry_credentials and not args.dry_run:
        tok = get_token()
        print(json.dumps({"ok": True, "group_openid": goid, "token_available": bool(tok)}, ensure_ascii=False, indent=2))
        return
    decision, conversation_turn = None, None
    if args.decision:
        if any(getattr(args, key) is not None for key in ("text", "markdown", "keyboard", "image", "voice", "video", "file", "recall")) or args.upload_only or args.sticker:
            ap.error("--decision cannot be combined with message/media/recall options")
        value = args.decision
        if value.startswith("@"):
            with Path(value[1:]).open("rb") as stream:
                value = stream.read(32769).decode("utf-8-sig")
        if len(value.encode("utf-8")) > 32768:
            ap.error("decision exceeds 32 KiB")
        decision = conversation.parse_decision(json.loads(value))
        if decision["action"] == "ignore":
            print(json.dumps({"ok": True, "skipped": True, "reason": "model_chose_silence"}))
            return
        if bool(args.msg_id) == bool(args.event_id):
            ap.error("--decision requires exactly one of --msg-id or --event-id")
        args.text = conversation.render(decision)
    elif args.choice_buttons:
        ap.error("--choice-buttons requires --decision")
    inferred_context = None
    if goid and args.msg_id and args.trigger is None and not args.topic_text and not args.recall:
        from core.reply_context import recover
        inferred_context = recover(G.group_dir(goid), goid, args.msg_id)
        if inferred_context:
            args.trigger = inferred_context["trigger"]
            args.topic_text = inferred_context["topic_text"]
            args.topic_key = args.topic_key or inferred_context["topic_key"]
    args.trigger = args.trigger or "legacy"
    policy_plugin = args.plugin_id or {"natural": "natural_reply", "question": "natural_reply", "at": "reply", "command": "command"}.get(args.trigger)
    cfg = {}
    if goid:
        cfg = G.load_config(goid)
        if cfg is not None and not cfg.get("enabled", True) and not args.recall:
            print(json.dumps({"skipped": True, "reason": f"group {cfg.get('name')} is disabled in config.json"}, ensure_ascii=False))
            sys.exit(4)
        if args.feature and cfg is not None and not (cfg.get("features") or {}).get(args.feature):
            print(json.dumps({"skipped": True, "reason": f"feature {args.feature!r} is off for group {cfg.get('name')}"}, ensure_ascii=False))
            sys.exit(4)
    base = target_path(goid, args.user_openid)
    scope = "c2c" if args.user_openid else "group"
    if decision:
        conversation_turn = conversation.prepare(DeliveryStore(), base,
            args.msg_id or args.event_id or args.source_event_id or "", decision)
        policy_plugin = conversation_turn["plugin_id"]
        args.source_event_id = conversation_turn["source"]
        context = conversation_turn["context"]
        args.trigger = context.get("trigger") if context.get("trigger") in {"at", "command"} else (
            "question" if decision["intent"] == "request" else "natural")
        if decision["conversation_id"]:
            args.trigger = "continuation"
        args.topic_text, args.topic_key = context.get("topic_text", ""), context.get("topic_key", "")
        if args.choice_buttons and decision["action"] == "clarify" and decision["options"]:
            if (cfg.get("features") or {}).get("buttons") and message_format.policy(cfg)["markdown_verified"]:
                args.keyboard = json.dumps(conversation.keyboard(conversation_turn["question_id"], decision["options"]), ensure_ascii=False)
                args.markdown, args.text, args.format = args.text, None, "markdown"
    if goid and policy_plugin:
        G.authorize(goid, policy_plugin, "message.send")

    if args.recall:
        if args.dry_run:
            print(json.dumps({"dry_run": True, "method": "DELETE", "target": base + "/messages/<message-id>"}))
            return
        from collections import deque
        from urllib.parse import quote
        with file_lock(LOCK, timeout=180):
            records = tail_jsonl(SENT_LOG)
            if not any(row.get("id") == args.recall and row.get("kind") != "recall" and row.get("scope", "group") == scope and time.time() - row.get("ts", 0) <= 120 for row in records):
                raise SystemExit("Recall requires a recent confirmed send in this conversation")
            action_id = stable_id([base, "recall", args.recall])
            store = DeliveryStore()
            reserved, previous = store.reserve_action(action_id, base)
            if not reserved:
                print(json.dumps({"duplicate": True, "status": previous["status"], "action_id": action_id}))
                raise SystemExit(0 if previous["status"] == "sent" else 5)
            store.action_status(action_id, "inflight")
            try:
                code, resp = api("DELETE", f"{base}/messages/{quote(args.recall, safe='')}")
                state = "sent" if code < 300 else "unknown" if code >= 500 else "failed"
                out = {"http_status": code, "body": resp, "ok": code < 300, "status": state}
            except QQTransportError:
                state, out = "unknown", {"ok": False, "status": "unknown"}
            store.action_status(action_id, state, out)
            if out["ok"]:
                log_sent({"ts": time.time(), "at": time.strftime("%H:%M:%S"), "kind": "recall", "scope": scope, "id": args.recall})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            sys.exit(0 if out["ok"] else 5 if state == "unknown" else 1)

    media = [(k, getattr(args, k)) for k in ("image", "voice", "video", "file") if getattr(args, k)]
    if len(media) > 1:
        ap.error("one media option per message (--image / --voice / --video / --file)")
    if args.text is not None and args.markdown is not None:
        ap.error("use only one of --text or --markdown")
    if not media and args.text is None and args.markdown is None:
        ap.error("need --text / --markdown / --image / --voice / --video / --file (or --recall/--recent)")
    keyboard = None
    if args.keyboard:
        kb = args.keyboard
        if kb.startswith("@"):
            kb = Path(kb[1:]).read_text(encoding="utf-8")
        keyboard = json.loads(kb)
        if "rows" in keyboard:
            keyboard = {"content": keyboard}
        if args.markdown is None:
            ap.error("--keyboard needs --markdown (buttons hang under a markdown message)")

    if args.upload_only and not args.dry_run:
        if not media:
            ap.error("--upload-only needs a media option")
        resp = upload(media[0][0], media[0][1], base)
        print(json.dumps({"ok": True, "upload": {k: v for k, v in resp.items() if k != "file_info"}, "file_info_len": len(resp.get("file_info", ""))}, ensure_ascii=False, indent=2))
        return

    # build message plan (list of payloads); file_info filled after upload
    plan: list[dict] = []
    import re as _re

    def _norm_at_for_text(s: str) -> str:
        # msg_type=0 (plain text) and msg_type=7 use <@openid> for real @ mentions
        return _re.sub(
            r'<qqbot-at-user\s+id=["\']?([0-9A-Fa-f]{16,})["\']?\s*/?>',
            r"<@\1>",
            s,
        )

    def _norm_at_for_md(s: str) -> str:
        # msg_type=2 (markdown) uses <qqbot-at-user id="openid" /> for real @ mentions
        return _re.sub(r"<@([0-9A-Fa-f]{16,})>", r'<qqbot-at-user id="\1" />', s)

    # If sending a local image + caption that contains an @ mention (<qqbot-at-user or <@HEX>),
    # msg_type=7 does NOT parse @ tags in group chat! Automatically upload via /v2/media-images
    # and send as a single msg_type=2 markdown message with qqbot-media:// + <qqbot-at-user>.
    combined_caption = args.text if args.text is not None else (args.markdown or "")
    has_at_tag = bool(_re.search(r"<qqbot-at-user|<@[0-9A-Fa-f]{16,}>", combined_caption))
    markdown_image = bool(media and media[0][0] == "image" and has_at_tag and not media[0][1].startswith(("https://", "http://")))
    settings = message_format.policy(cfg)
    if args.sticker and (not media or media[0][0] != "image"):
        ap.error("--sticker requires --image")
    parts, format_warnings = message_format.prepare(combined_caption, "markdown" if args.markdown is not None else "plain",
        cfg, mode=args.format, max_chars=args.max_chars, max_parts=args.max_parts)
    for part in parts:
        if part["msg_type"] == 2:
            part["markdown"]["content"] = _norm_at_for_md(part["markdown"]["content"])
        else:
            part["content"] = _norm_at_for_text(part["content"])
    if keyboard and (len(parts) != 1 or parts[0]["msg_type"] != 2):
        ap.error("keyboard requires a single Markdown part; shorten content or explicitly choose --format markdown")
    if keyboard:
        parts[0]["keyboard"] = keyboard
    if decision and decision["action"] == "clarify" and len(parts) != 1:
        ap.error("clarification must fit one message; shorten the preface/question")
    if markdown_image:
        if len(combined_caption) > (args.max_chars or settings["max_chars"]) - 200:
            ap.error("image + mention caption is too long; shorten the caption")
        md = {"msg_type": 2, "markdown": {"content": _norm_at_for_md(combined_caption) + "\n<uploaded-image>"}}
        if keyboard:
            md["keyboard"] = keyboard
        plan.append(md)
        format_warnings.append("Image + mention uses the existing Markdown media route; verify client visibility")
    elif media:
        mp = {"msg_type": 7, "media": {"file_info": "<upload>"}, "content": " "}
        if parts and parts[0]["msg_type"] == 0:
            mp["content"] = parts.pop(0)["content"] or " "
        plan = [mp, *parts] if combined_caption else [mp]
    else:
        plan = parts
    if len(plan) > (args.max_parts or settings["max_parts"]):
        ap.error("media and caption exceed maximum message parts; shorten the reply")

    if args.dry_run:
        print(json.dumps({"dry_run": True, "group": goid, "sent_log": SENT_LOG, "target": base + "/messages", "plan": plan, "format_warnings": format_warnings, "trigger": args.trigger, "context_recovered": bool(inferred_context), "reply_to": args.reply_to, "msg_id": args.msg_id, "event_id": args.event_id}, ensure_ascii=False, indent=2))
        return

    media_identity = []
    for kind, source in media:
        media_identity.append([kind, source if source.startswith(("http://", "https://")) else hashlib.sha256(read_local(source)).hexdigest()])
    trigger = args.msg_id or args.source_event_id or args.event_id or (time.time_ns() if args.force else int(time.time() // WINDOW))
    identity_parts = [base, trigger, plan, media_identity, args.reply_to]
    request_parts = [plan, media_identity, args.reply_to, args.msg_id, args.event_id]
    if decision:
        identity_parts.append(decision)
        request_parts.append(decision)
    action_id = stable_id([base, args.action_id]) if args.action_id else stable_id(identity_parts)
    meta = reply_policy.metadata(args.trigger, args.msg_id or args.source_event_id or args.event_id,
        message_format.plain_text(combined_caption), media_identity, topic_text=args.topic_text,
        topic_key=args.topic_key, sticker=args.sticker)
    if args.sticker and media_identity:
        meta["sticker_hash"] = media_identity[0][1]
    limits = reply_policy.settings(cfg)
    store = DeliveryStore()
    with file_lock(LOCK, timeout=180):
        legacy_cooldown = bool(scope == "group" and args.trigger == "legacy" and not args.force
                               and (recent_sends() or store.recent_actions(base)))
        try:
            reserved, previous = store.reserve_action(action_id, base,
                stable_id(request_parts), force=bool(args.force), reply_meta=meta, limits=limits,
                cooldown_active=legacy_cooldown, conversation_turn=conversation_turn)
        except ReplySuppressed as error:
            print(json.dumps({"skipped": True, "reason": str(error), "action_id": action_id}))
            raise SystemExit(3)
        if not reserved:
            status = previous["status"]
            if status in {"pending", "inflight"}:
                status = "unknown"
                store.action_status(action_id, status, {"reason": "Prior process did not confirm completion"})
            print(json.dumps({"ok": status == "sent", "duplicate": True, "action_id": action_id, "status": status}))
            raise SystemExit(0 if status == "sent" else 5)
        results, state, up = [], "failed", None
        try:
            if args.sticker and args.trigger == "natural":
                time.sleep(.7 + int(action_id[:4], 16) % 1100 / 1000)
            if markdown_image:
                murl, width, height = upload_markdown_image(media[0][1])
                plan[0]["markdown"]["content"] = _norm_at_for_md(combined_caption) + f"\n![image #{width}px #{height}px]({murl})"
            elif media:
                up = upload(media[0][0], media[0][1], base)
                plan[0]["media"] = {"file_info": up["file_info"]}
            for index, payload in enumerate(plan):
                # Recheck policy immediately before each externally visible part.
                if goid:
                    cfg = G.load_config(goid)
                    if cfg is None or not cfg.get("enabled", True) or (args.feature and not (cfg.get("features") or {}).get(args.feature)):
                        raise RuntimeError("Group policy no longer allows this send")
                    if policy_plugin:
                        G.authorize(goid, policy_plugin, "message.send")
                if args.msg_id or args.event_id:
                    payload["msg_seq"] = (args.msg_seq if args.msg_seq is not None else int(action_id[:8], 16) % 1_000_000) + index
                store.action_status(action_id, "inflight")
                store.part_status(action_id, index, "inflight")
                state = "unknown"
                result = post_message(base, payload, msg_id=args.msg_id, event_id=args.event_id,
                    reply_to=args.reply_to if index == 0 else None)
                fallback_cfg = {**(cfg or {}), "message_format": {**message_format.policy(cfg), "max_chars": args.max_chars or settings["max_chars"]}}
                if message_format.may_fallback(result, payload, fallback_cfg, choice_fallback=bool(decision and args.choice_buttons)):
                    # Only a definite format rejection may take this branch. A
                    # timeout or successful HTTP response is never replayed.
                    rejection = {"http_status": result["http_status"], "body": result["body"]}
                    payload = {"msg_type": 0, "content": _norm_at_for_text(message_format.plain_text(payload["markdown"]["content"]))}
                    if args.msg_id or args.event_id:
                        payload["msg_seq"] = (args.msg_seq if args.msg_seq is not None else int(action_id[:8], 16) % 1_000_000) + index
                    result = post_message(base, payload, msg_id=args.msg_id, event_id=args.event_id,
                        reply_to=args.reply_to if index == 0 else None)
                    result["format_fallback"] = rejection
                results.append(result)
                part_state = "sent" if result["ok"] else ("unknown" if result["http_status"] >= 500 else "failed")
                store.part_status(action_id, index, part_state, result)
                if not result["ok"]:
                    state = "partial" if any(item["ok"] for item in results) else part_state
                    break
                body = result["body"] if isinstance(result["body"], dict) else {}
                kind = {0: "text", 2: "markdown", 7: media[0][0] if media else "media"}[payload["msg_type"]]
                content = payload.get("markdown", {}).get("content") if payload["msg_type"] == 2 else payload.get("content")
                state = "sent" if index == len(plan) - 1 else "partial"
                content = str(content or "").strip() or "[" + kind + "]"
                try:
                    log_sent({"ts": time.time(), "at": time.strftime("%H:%M:%S"), "scope": scope, "kind": kind,
                        "content": content, "id": body.get("id") or "", "action_id": action_id,
                        "reply_policy": meta, "ref_idx": (body.get("ext_info") or {}).get("ref_idx") or "", "keyboard": bool(payload.get("keyboard"))})
                    from core.memory import MemoryStore
                    MemoryStore(DIR).add_message(goid or "c2c:" + args.user_openid, "out:" + str(body.get("id") or action_id + ":" + str(index)), content, direction="out",
                                                actor="bot", reply_to=args.msg_id or args.event_id or "")
                except Exception:
                    result["warning"] = "Confirmed send; local history update failed"
        except Exception as error:
            if any(item["ok"] for item in results):
                state = "partial"
            results.append({"ok": False, "error": type(error).__name__, "reason": "See durable delivery status before retrying"})
        store.action_status(action_id, state, {"results": results})
        print(json.dumps({"ok": state == "sent", "status": state, "action_id": action_id, "format_warnings": format_warnings, "results": results}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if state == "sent" else 5 if state in {"unknown", "partial"} else 1)


if __name__ == "__main__":
    main()
