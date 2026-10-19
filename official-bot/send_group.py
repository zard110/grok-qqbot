#!/usr/bin/env python3
"""Send a group (or C2C) message via official QQ bot MaxShenBi.

Text / markdown / image / voice / video / file, quote-reply, keyboard buttons, recall.
See USAGE.md.
"""
from __future__ import annotations

import argparse
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

DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DIR))
import groups as G  # noqa: E402
API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# per-target anti-duplicate files (set in main(); Zwift's are also reachable via the old top-level symlinks)
SENT_LOG = str(DIR / "sent-log.jsonl")
LOCK = str(DIR / ".send.lock")


def set_target_files(goid: str | None, user_openid: str | None) -> None:
    global SENT_LOG, LOCK
    d = G.C2C_DIR if user_openid else G.group_dir(goid)
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
    global _token, _token_at
    if _token and (time.time() - _token_at) < 6000:
        return _token
    body = json.dumps({"appId": rd("appid"), "clientSecret": rd("appsecret")}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    tok = data.get("access_token") or ""
    if not tok:
        raise SystemExit("token failed (no access_token)")
    _token, _token_at = tok, time.time()
    return tok


def _parse(raw: str):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def api(method: str, path: str, payload: dict | None = None, timeout: int = 60) -> tuple[int, object]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"QQBot {get_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _parse(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read().decode())


def target_path(group_openid: str | None, user_openid: str | None) -> str:
    if user_openid:
        return f"/v2/users/{user_openid}"
    return f"/v2/groups/{group_openid or G.default_group_openid()}"


def log_sent(entry: dict) -> None:
    try:
        with open(SENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_sends(window: int = WINDOW):
    res = []
    try:
        now = time.time()
        for line in open(SENT_LOG, encoding="utf-8").readlines()[-30:]:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("scope", "group") != "group" or e.get("kind") == "recall":
                continue
            if now - e.get("ts", 0) <= window:
                res.append(e)
    except FileNotFoundError:
        pass
    return res


def _to_silk(path: str) -> str:
    """Convert audio to silk via pilk (only used if mp3/wav upload is rejected)."""
    try:
        import pilk  # type: ignore
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "pilk"])
        import pilk  # type: ignore
    pcm = tempfile.mktemp(suffix=".pcm")
    out = tempfile.mktemp(suffix=".silk")
    subprocess.check_call(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "24000", pcm]
    )
    pilk.encode(pcm, out, pcm_rate=24000, tencent=True)
    os.unlink(pcm)
    return out


def upload(kind: str, src: str, base: str) -> dict:
    """Upload media; returns the API response body (contains file_info). Never sends a message."""
    ft = FILE_TYPES[kind]
    body: dict = {"file_type": ft, "srv_send_msg": False}
    if src.startswith("http://") or src.startswith("https://"):
        body["url"] = src
    else:
        p = Path(src)
        if not p.is_file():
            raise SystemExit(f"file not found: {src}")
        data = p.read_bytes()
        if len(data) > 5 * 1024 * 1024:
            raise SystemExit("local file > 5MB: binary upload limit is 5MB (use a URL, or shrink it)")
        body["file_data"] = base64.b64encode(data).decode()
        body["file_name"] = p.name
    code, resp = api("POST", f"{base}/files", body, timeout=120)
    if code >= 300 or not isinstance(resp, dict) or not resp.get("file_info"):
        if kind == "voice" and "file_data" in body and not src.lower().endswith(".silk"):
            # fallback: convert to silk and retry once
            silk = _to_silk(src)
            body["file_data"] = base64.b64encode(Path(silk).read_bytes()).decode()
            body["file_name"] = Path(src).stem + ".silk"
            code, resp = api("POST", f"{base}/files", body, timeout=120)
            if code < 300 and isinstance(resp, dict) and resp.get("file_info"):
                resp["converted_to_silk"] = True
                return resp
        print(json.dumps({"error": True, "stage": "upload", "kind": kind, "http_status": code, "body": resp}, ensure_ascii=False, indent=2))
        sys.exit(1)
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
    return {"http_status": code, "body": resp, "ok": code < 300}


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
    ap.add_argument("--group", help="target group: alias (e.g. zwift), name, QQ number or group_openid. Default: Zwift练功房")
    ap.add_argument("--group-openid", dest="group_openid", help="same as --group (kept for old commands)")
    ap.add_argument("--feature", help="refuse (exit 4) unless this feature is on in the group's config.json, e.g. daily_ai_news")
    ap.add_argument("--user-openid", dest="user_openid", help="send to a C2C (private) user instead of the group")
    ap.add_argument("--force", action="store_true", help="send even if MaxShenBi already sent within the last 120s")
    ap.add_argument("--recent", action="store_true", help="print MaxShenBi sends in the last 120s and exit")
    ap.add_argument("--upload-only", action="store_true", help="only upload media and print file_info (sends nothing)")
    ap.add_argument("--dry-run", action="store_true", help="print the request payload(s) and exit (sends nothing; no upload)")
    ap.add_argument("--dry-credentials", action="store_true", help="only verify token + group_openid (no send)")
    args = ap.parse_args()

    goid = None if args.user_openid else G.resolve(args.group or args.group_openid)
    set_target_files(goid, args.user_openid)
    if args.recent:
        print(json.dumps(recent_sends(), ensure_ascii=False, indent=2))
        return
    if args.dry_credentials:
        tok = get_token()
        print(json.dumps({"ok": True, "appid": rd("appid"), "group_openid": goid, "token_prefix": tok[:8] + "..."}, ensure_ascii=False, indent=2))
        return
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

    if args.recall:
        code, resp = api("DELETE", f"{base}/messages/{args.recall}")
        out = {"http_status": code, "body": resp, "ok": code < 300}
        if out["ok"]:
            log_sent({"ts": time.time(), "at": time.strftime("%H:%M:%S"), "kind": "recall", "scope": scope, "id": args.recall})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0 if out["ok"] else 1)

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

    if args.upload_only:
        if not media:
            ap.error("--upload-only needs a media option")
        resp = upload(media[0][0], media[0][1], base)
        print(json.dumps({"ok": True, "upload": {k: v for k, v in resp.items() if k != "file_info"}, "file_info_len": len(resp.get("file_info", ""))}, ensure_ascii=False, indent=2))
        return

    # build message plan (list of payloads); file_info filled after upload
    plan: list[dict] = []
    caption_label = ""
    if media:
        mp: dict = {"msg_type": 7, "media": {"file_info": "<upload>"}}
        if args.text is not None:
            mp["content"] = args.text  # text caption rides in the same message
        else:
            mp["content"] = " "
        plan.append(mp)
        if args.markdown is not None:
            md = {"msg_type": 2, "markdown": {"content": args.markdown}}
            if keyboard:
                md["keyboard"] = keyboard
            plan.append(md)  # caption as a separate message right after the media
        caption_label = args.text if args.text is not None else (args.markdown or "")
    elif args.markdown is not None:
        md = {"msg_type": 2, "markdown": {"content": args.markdown}}
        if keyboard:
            md["keyboard"] = keyboard
        plan.append(md)
    else:
        plan.append({"msg_type": 0, "content": args.text})

    if args.dry_run:
        print(json.dumps({"dry_run": True, "group": goid, "sent_log": SENT_LOG, "target": base + "/messages", "plan": plan, "reply_to": args.reply_to, "msg_id": args.msg_id, "event_id": args.event_id}, ensure_ascii=False, indent=2))
        return

    import fcntl
    lf = open(LOCK, "w")
    fcntl.flock(lf, fcntl.LOCK_EX)  # serialize concurrent runs: check + send is atomic
    if scope == "group":
        rec = recent_sends()
        if rec and not args.force:
            print(json.dumps({"skipped": True, "reason": "MaxShenBi already sent within the last 120s; another run probably answered. Read these and only resend with --force if yours covers a genuinely different, still-unanswered point.", "recent": rec}, ensure_ascii=False, indent=2))
            sys.exit(3)

    results = []
    up = None
    if media:
        up = upload(media[0][0], media[0][1], base)
        plan[0]["media"] = {"file_info": up["file_info"]}
    for i, payload in enumerate(plan):
        if args.msg_seq is not None and (args.msg_id or args.event_id):
            payload["msg_seq"] = args.msg_seq + i
        r = post_message(
            base, payload,
            msg_id=args.msg_id, event_id=args.event_id,
            reply_to=args.reply_to if i == 0 else None,
            seq_offset=i,
        )
        results.append(r)
        if not r["ok"]:
            break
        body = r["body"] if isinstance(r["body"], dict) else {}
        kind = {0: "text", 2: "markdown", 7: media[0][0] if media else "media"}[payload["msg_type"]]
        content = payload.get("markdown", {}).get("content") if payload["msg_type"] == 2 else payload.get("content")
        if payload["msg_type"] == 7:
            content = f"[{kind}: {Path(media[0][1]).name}] {(args.text or '')}".strip()
        log_sent({
            "ts": time.time(), "at": time.strftime("%H:%M:%S"), "scope": scope, "kind": kind,
            "content": content or "", "id": body.get("id") or "",
            "ref_idx": (body.get("ext_info") or {}).get("ref_idx") or "",
            "keyboard": bool(payload.get("keyboard")),
        })
    out = {"ok": all(r["ok"] for r in results), "results": results}
    if up is not None:
        out["upload"] = {k: v for k, v in up.items() if k not in ("file_info",)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out["ok"] else 1)


if __name__ == "__main__":
    main()
