#!/usr/bin/env python3
"""Official QQ bot (MaxShenBi) gateway bridge → Grok webhook (OneBot-compatible)."""
from __future__ import annotations

import hashlib
import json
import shlex
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

CODE_DIR = Path(__file__).resolve().parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))
from core.settings import DATA_DIR
from core.files import atomic_write_json, atomic_write_text, append_jsonl, file_lock
from core.qq_client import client as qq_client
from core.media import cache_image
from core.collection import CollectionScheduler, armed_window_seconds, is_collection_bypass
from core.delivery import (
    SAME_SENDER_FOLLOWUP_SECONDS, DeliveryStore, GroupDispatcher,
    coalesce_same_sender, event_identity, snapshot,
)
DIR = DATA_DIR
import groups as G  # noqa: E402  per-group registry
FORWARDER = Path(os.environ.get("QQBOT_FORWARDER_DIR", "/root/forwarder"))
LOG = DIR / "official-ws-bridge.log"
# Local identity values live in git-ignored files next to this script (see secrets.example.md).
def _local_int(name: str, default: int = 0) -> int:
    try:
        return int((DIR / name).read_text(encoding="utf-8").strip())
    except Exception:
        return default


def _local_str(name: str, default: str) -> str:
    try:
        return (DIR / name).read_text(encoding="utf-8").strip() or default
    except Exception:
        return default


BOT_UIN = _local_int("bot_uin.txt")
BOT_NAME = _local_str("bot_name.txt", "MaxShenBi")
API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
# GROUP messages + public messages (covers GROUP_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE)
INTENTS = (1 << 25) | (1 << 30)
try:
    _iv = (Path(__file__).resolve().parent / "intents.txt").read_text().strip()
    if _iv:
        INTENTS = int(_iv, 0)
except Exception:
    pass

AT_RE = re.compile(r"<@!?([A-Fa-f0-9]+)>")


def log(msg: str) -> None:
    # run-official-ws.sh redirects stdout → official-ws-bridge.log
    print(time.strftime("[%F %T] ") + msg, flush=True)


def rd(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def get_token() -> str:
    return qq_client.token()


def get_gateway(token: str) -> str:
    status, data = qq_client.request("GET", "/gateway", timeout=20)
    if status >= 300 or not isinstance(data, dict):
        raise RuntimeError(f"gateway HTTP status {status}")
    url = data.get("url") or ""
    if not url.startswith("wss://"):
        raise RuntimeError("bad gateway response")
    return url


def member_user_id(member_openid: str) -> int:
    """Stable non-zero int from openid (OneBot field); not a real QQ number."""
    if not member_openid:
        return 0
    h = hashlib.sha1(member_openid.encode()).hexdigest()[:8]
    return int(h, 16) % 2_000_000_000 or 1


def strip_at(content: str) -> str:
    return AT_RE.sub("", content or "").strip()


def is_at_me(d: dict, bot_openid: str, event_t: str) -> bool:
    if event_t == "GROUP_AT_MESSAGE_CREATE":
        return True
    content = d.get("content") or ""
    if bot_openid and bot_openid in content:
        return True
    for m in d.get("mentions") or []:
        if not isinstance(m, dict):
            continue
        if m.get("is_you") or (bot_openid and m.get("id") == bot_openid):
            return True
        if m.get("bot") and m.get("username") == BOT_NAME:
            return True
    return False


FACE_RE = re.compile(r'<faceType=\d+,faceId="[^"]*"(?:,ext="([^"]*)")?>')
REF_CACHE_FILE = DIR / "ref-cache.json"
_ref_cache: dict = {}
_ref_lock = threading.RLock()
try:
    _ref_cache = json.loads(REF_CACHE_FILE.read_text(encoding="utf-8"))
except Exception:
    _ref_cache = {}


def _ext_map(d: dict) -> dict:
    out = {}
    for kv in ((d.get("message_scene") or {}).get("ext") or []):
        if isinstance(kv, str) and "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


def ref_cache_put(idx: str, name: str, text: str, goid: str = "") -> None:
    if not idx:
        return
    with _ref_lock:
        _ref_cache[goid + ":" + idx] = {"name": name, "text": (text or "")[:300], "ts": int(time.time()), "g": goid}
        if len(_ref_cache) > 600:
            for k in sorted(_ref_cache, key=lambda k: _ref_cache[k].get("ts", 0))[:200]:
                _ref_cache.pop(k, None)
        atomic_write_json(REF_CACHE_FILE, _ref_cache)


def ref_lookup(idx: str, goid: str = "") -> dict | None:
    if not idx:
        return None
    with _ref_lock:
        hit = _ref_cache.get(goid + ":" + idx)
    if hit and hit.get("g") == goid:
        return hit
    # our own sends: send_group.py stores ext_info.ref_idx in the group's sent-log.jsonl
    from core.files import confined_path
    sl = (confined_path(G.C2C_DIR, goid[4:]) if goid.startswith("c2c:") else G.group_dir(goid)) / "sent-log.jsonl" if goid else DIR / "sent-log.jsonl"
    try:
        for line in reversed(open(sl, encoding="utf-8").readlines()[-300:]):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("ref_idx") == idx:
                return {"name": BOT_NAME, "text": (e.get("content") or "")[:300]}
    except Exception:
        pass
    return None


def _face_sub(m: "re.Match") -> str:
    txt = ""
    try:
        import base64
        txt = json.loads(base64.b64decode(m.group(1) or "").decode("utf-8", "ignore")).get("text") or ""
    except Exception:
        pass
    return f"[表情{(':' + txt) if txt else ''}]"


def _att_list(atts, goid: str = "", is_face_msg: bool = False) -> tuple[list, list]:
    """normalize attachments -> (list of dicts, placeholder strings)"""
    out, ph = [], []
    for a in (atts or [])[:16]:
        if not isinstance(a, dict):
            continue
        ct = (a.get("content_type") or "").lower()
        item = {k: a.get(k) for k in ("url", "content_type", "filename", "size", "width", "height") if a.get(k) not in (None, "")}
        url = a.get("url") or ""
        if url.startswith("//"):
            url = "https:" + url
            item["url"] = url
        if ct.startswith("image"):
            w = int(a.get("width") or 0)
            h = int(a.get("height") or 0)
            sz = int(a.get("size") or 0)
            fn = a.get("filename") or ""
            is_sticker = is_face_msg or (0 < w <= 500 and 0 < h <= 500 and 0 < sz <= 500000) or (0 < w <= 1100 and 0 < h <= 1100 and 0 < sz <= 1050000 and 0.72 <= (w / max(h, 1)) <= 1.38)
            local_p = ""
            if is_sticker and url and fn:
                try:
                    from core.files import confined_path
                    scope_dir = confined_path(G.C2C_DIR, goid[4:]) if goid.startswith("c2c:") else G.group_dir(goid) if goid else G.C2C_DIR
                    sdir = scope_dir / "stickers"
                    sdir.mkdir(parents=True, exist_ok=True)
                    dest = cache_image(url, sdir)
                    if dest.exists():
                        local_p = str(dest)
                except Exception as e:
                    log(f"sticker cache skip {fn}: {e}")
            if is_sticker:
                item["kind"] = "sticker"
                if local_p:
                    item["local_path"] = local_p
                    in_idx = False
                    try:
                        idx_p = Path(local_p).parent / "index.json"
                        if idx_p.exists():
                            idx_obj = json.loads(idx_p.read_text("utf-8"))
                            known_files = {Path(str(x.get("file", ""))).name for x in idx_obj.get("ready_stickers", [])}
                            in_idx = (Path(local_p).name in known_files)
                    except Exception:
                        pass
                    item["in_index"] = in_idx
                    if in_idx:
                        ph.append(f"[自定义表情包(已在库中): {local_p}]")
                    else:
                        ph.append(f'[自定义表情包(新图未入库，请Read看图后顺手运行 python3 {shlex.quote(str(CODE_DIR / "make_sticker.py"))} --group {shlex.quote(goid)} --add-sticker {shlex.quote(str(local_p))} --text "<图中文字或无>" --category "<分类>" --tags "<标签1>,<标签2>" 收纳入库): {local_p}]')
                else:
                    ph.append(f"[自定义表情包: {url}]" if url else "[自定义表情包]")
            else:
                item["kind"] = "image"
                ph.append(f"[图片: {url}]" if url else "[图片]")
        elif ct == "voice" or ct.startswith("audio"):
            item["kind"] = "voice"
            asr = a.get("asr_refer_text") or ""
            if a.get("voice_wav_url"):
                item["voice_wav_url"] = a["voice_wav_url"]
            if asr:
                item["asr_refer_text"] = asr
            ph.append(f"[语音转文字: {asr}]" if asr else f"[语音: {a.get('voice_wav_url') or url}]")
        elif ct.startswith("video"):
            item["kind"] = "video"
            ph.append(f"[视频: {url}]" if url else "[视频]")
        else:
            item["kind"] = "file"
            ph.append(f"[文件: {a.get('filename') or ''}{(' ' + url) if url else ''}]")
        out.append(item)
    return out, ph


def _ark_info(ark) -> tuple[dict | None, str]:
    if not isinstance(ark, dict) or not ark:
        return None, ""
    f = ark.get("fields") or {}
    if not isinstance(f, dict):
        f = {}
    info = {
        "ark_type": ark.get("ark_type") or "",
        "ark_name": ark.get("ark_name") or "",
        "prompt": ark.get("prompt") or "",
        "title": f.get("title") or "",
        "desc": f.get("desc") or "",
        "url": f.get("jump_url") or f.get("url") or "",
        "source": f.get("source") or f.get("tag") or "",
    }
    label = info["title"] or info["prompt"] or info["desc"]
    return info, f"[卡片{('(' + info['ark_name'] + ')') if info['ark_name'] else ''}: {label}{(' ' + info['url']) if info['url'] else ''}]"


def _elements_text(els, depth: int = 0) -> list[str]:
    parts = []
    for el in els or []:
        if not isinstance(el, dict) or depth > 3:
            continue
        who = ((el.get("author") or {}).get("username")) or ""
        c = FACE_RE.sub(_face_sub, strip_at(el.get("content") or "")).strip()
        _, ph = _att_list(el.get("attachments"))
        _, arkph = _ark_info(el.get("ark_data"))
        seg = " ".join(x for x in [c] + ph + ([arkph] if arkph else []) if x)
        if seg:
            parts.append(f"{who + ': ' if who else ''}{seg}")
        parts += _elements_text(el.get("msg_elements"), depth + 1)
    return parts


def enrich(d: dict, text: str, goid: str = "") -> dict:
    """Extract non-text info. Returns dict with attachments/quoted/card/raw_message/ref_idx."""
    is_face_msg = "<faceType=" in (text or "")
    text = FACE_RE.sub(_face_sub, text or "").strip()
    atts, ph = _att_list(d.get("attachments"), goid=goid, is_face_msg=is_face_msg)
    card, cardph = _ark_info(d.get("ark_data"))
    ext = _ext_map(d)
    quoted = None
    qtext = ""
    mt = d.get("message_type")
    els = d.get("msg_elements") or []
    if els:
        parts = _elements_text(els)
        qtext = "\n".join(parts)[:600]
        if qtext:
            quoted = {"source": "msg_elements", "message_type": mt, "text": qtext}
    ref = ext.get("ref_msg_idx") or ""
    if ref:
        hit = ref_lookup(ref, goid)
        if hit:
            quoted = quoted or {}
            quoted.update({"ref_idx": ref, "name": hit.get("name"), "text": quoted.get("text") or hit.get("text")})
            qtext = qtext or f"{hit.get('name')}: {hit.get('text')}"
        elif not quoted:
            quoted = {"ref_idx": ref, "text": "", "note": "被引用消息不在缓存里（可能是很早的消息）"}
    raw_parts = []
    if quoted:
        raw_parts.append(f"[引用: {qtext[:300]}]" if qtext else "[引用了一条较早的消息]")
    if text:
        raw_parts.append(text)
    raw_parts += ph
    if cardph:
        raw_parts.append(cardph)
    return {
        "text": text,
        "raw_message": " ".join(raw_parts).strip(),
        "attachments": atts,
        "quoted": quoted,
        "card": card,
        "ref_idx": ext.get("msg_idx") or "",
        "content_type": mt,
    }


def group_qq_id(cfg: dict | None, goid: str) -> int:
    q = (cfg or {}).get("qq_group_id")
    try:
        if q:
            return int(q)
    except Exception:
        pass
    return member_user_id(goid)  # stable pseudo id for groups whose QQ number we don't know


_token_holder = {"t": "", "at": 0.0}


def _api_token() -> str:
    return qq_client.token()


def fetch_group_name(goid: str) -> str:
    try:
        status, data = qq_client.request("GET", f"/v2/groups/{goid}/info", timeout=5)
        return data.get("group_name", "") if status < 300 and isinstance(data, dict) else ""
    except Exception as e:
        log(f"group info failed {goid[:8]}: {e}")
        return ""


def group_cfg(goid: str, extra: dict | None = None) -> dict | None:
    """Load (or auto-create from _default) the group's config."""
    if not goid:
        return None
    cfg = G.load_config(goid)
    if cfg is None:
        name = fetch_group_name(goid)
        cfg, created = G.ensure_group(goid, name, extra)
        if created:
            log(f"NEW GROUP auto-created {goid} name={cfg.get('name')!r} (at_reply only)")
    return cfg


def group_fields(cfg: dict | None, goid: str) -> dict:
    return {
        "group_id": group_qq_id(cfg, goid),
        "group_openid": goid,
        "group_name": (cfg or {}).get("name") or "",
        "group_alias": ((cfg or {}).get("aliases") or [""])[0],
        "group_dir": str(G.group_dir(goid)) if goid else "",
        "group_enabled": bool((cfg or {}).get("enabled", True)),
        "features": (cfg or {}).get("features") or {},
    }


def save_group_event(goid: str, ev: dict) -> None:
    gd = G.group_dir(goid) if goid else G.SYSTEM_DIR
    append_jsonl(gd / "last-events.jsonl", ev)


def to_payload(outer: dict, bot_openid: str, our_group: str) -> dict | None:
    t = outer.get("t") or ""
    if t not in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
        return None
    d = outer.get("d") or {}
    if not isinstance(d, dict):
        return None
    goid = d.get("group_openid") or d.get("group_id") or ""
    cfg = group_cfg(goid)
    if cfg and (not cfg.get("enabled", True) or not (cfg.get("features") or {}).get("media_understanding", True)):
        d = {**d, "attachments": []}
    author = d.get("author") or {}
    is_bot = author.get("bot") is True  # forward other bots too (from_bot=true); our own is skipped below
    # skip our own openid if present
    aoid = author.get("member_openid") or author.get("id") or ""
    if bot_openid and aoid == bot_openid:
        return None
    content = d.get("content") or ""
    text = strip_at(content)
    mid = d.get("id") or ""
    at_me = is_at_me(d, bot_openid, t)
    uid = member_user_id(aoid)
    ex = enrich(d, text, goid)
    text = ex["raw_message"]
    ref_cache_put(ex["ref_idx"], author.get("username") or "", text, goid)
    segs = [{"type": "text", "data": {"text": text}}]
    for a in ex["attachments"]:
        if a.get("kind") == "image" and a.get("url"):
            segs.append({"type": "image", "data": {"url": a["url"], "file": a.get("filename") or ""}})
    return {
        "post_type": "message",
        "message_type": "group",
        "user_id": uid,
        "message_id": mid,
        "raw_message": text,
        "message": segs,
        "text_only": ex["text"],
        "attachments": ex["attachments"],
        "quoted": ex["quoted"],
        "reply_to": (ex["quoted"] or {}).get("ref_idx", ""),
        "card": ex["card"],
        "ref_idx": ex["ref_idx"],
        "content_type": ex["content_type"],
        "member_role": author.get("member_role") or "",
        "sender": {
            "nickname": author.get("username") or "",
            "user_id": uid,
        },
        "official": True,
        "bot": BOT_NAME,
        "bot_uin": BOT_UIN,
        "group_openid": goid,
        "member_openid": aoid,
        "msg_id": mid,
        "event_id": outer.get("id") or "",
        "at_me": at_me,
        "from_bot": is_bot,
        "time": int(time.time()),
        "self_id": BOT_UIN,
        **group_fields(cfg, goid),
    }


NOTICE_EVENTS = {
    "GROUP_MEMBER_ADD": "member_join",
    "GROUP_MEMBER_REMOVE": "member_leave",
    "GROUP_ADD_ROBOT": "bot_added",
    "GROUP_DEL_ROBOT": "bot_removed",
    "GROUP_MSG_REJECT": "proactive_off",
    "GROUP_MSG_RECEIVE": "proactive_on",
}


def _base(outer: dict, goid: str, cfg: dict | None = None) -> dict:
    b = {
        "official": True,
        "bot": BOT_NAME,
        "bot_uin": BOT_UIN,
        "event_id": outer.get("id") or "",
        "time": int(time.time()),
        "self_id": BOT_UIN,
    }
    if goid:
        b.update(group_fields(cfg, goid))
    return b


def notice_payload(outer: dict, our_group: str) -> dict | None:
    t = outer.get("t") or ""
    d = outer.get("d") or {}
    goid = d.get("group_openid") or ""
    extra = {"added_by": d.get("op_member_openid")} if t == "GROUP_ADD_ROBOT" and d.get("op_member_openid") else None
    cfg = group_cfg(goid, extra)
    mo = d.get("member_openid") or d.get("op_member_openid") or ""
    p = _base(outer, goid, cfg)
    p.update({
        "post_type": "notice",
        "notice_type": NOTICE_EVENTS[t],
        "event": t,
        "member_openid": mo,
        "user_id": member_user_id(mo),
        "at_tag": f'<qqbot-at-user id="{mo}" />' if mo else "",
        "raw": d,
    })
    return p


def interaction_ack(token: str, iid: str, code: int = 0) -> str:
    try:
        status, _ = qq_client.request("PUT", f"/interactions/{iid}", {"code": code}, timeout=3)
        return str(status)
    except urllib.error.HTTPError as e:
        return f"{e.code} {e.read().decode()[:200]}"
    except Exception as e:
        return f"err {e}"


def interaction_payload(outer: dict, our_group: str) -> dict | None:
    d = outer.get("d") or {}
    goid = d.get("group_openid") or ""
    cfg = group_cfg(goid) if goid else None
    res = ((d.get("data") or {}).get("resolved")) or {}
    mo = d.get("group_member_openid") or d.get("user_openid") or ""
    p = _base(outer, goid, cfg)
    p.update({
        "post_type": "interaction",
        "event_id": d.get("id") or outer.get("id") or "",
        "interaction_type": d.get("type"),
        "scene": d.get("scene") or "",
        "member_openid": mo,
        "user_id": member_user_id(mo),
        "at_tag": f'<qqbot-at-user id="{mo}" />' if mo and d.get("scene") == "group" else "",
        "button_id": res.get("button_id") or "",
        "button_data": res.get("button_data") or "",
        "raw_message": f"[按钮点击: {res.get('button_data') or res.get('button_id') or ''}]",
        "raw": d,
    })
    return p


def c2c_payload(outer: dict) -> dict:
    d = outer.get("d") or {}
    author = d.get("author") or {}
    uo = author.get("user_openid") or author.get("id") or ""
    ex = enrich(d, strip_at(d.get("content") or ""), "c2c:" + uo)
    p = _base(outer, "")
    p.update({
        "post_type": "message",
        "message_type": "private",
        "c2c_dir": str(G.C2C_DIR / uo),
        "group_openid": "",
        "user_openid": uo,
        "user_id": member_user_id(uo),
        "message_id": d.get("id") or "",
        "msg_id": d.get("id") or "",
        "raw_message": ex["raw_message"],
        "text_only": ex["text"],
        "message": [{"type": "text", "data": {"text": ex["raw_message"]}}],
        "attachments": ex["attachments"],
        "quoted": ex["quoted"],
        "reply_to": (ex["quoted"] or {}).get("ref_idx", ""),
        "card": ex["card"],
        "ref_idx": ex["ref_idx"],
        "sender": {"nickname": author.get("username") or "", "user_id": member_user_id(uo)},
        "at_me": True,
        "from_bot": False,
    })
    return p


def should_forward(p: dict) -> tuple[bool, str]:
    """Per-group feature gating. Returns (forward?, reason)."""
    if not p.get("group_openid"):
        return True, "c2c"
    if not p.get("group_enabled", True):
        return False, "group disabled"
    f = p.get("features") or {}
    pt = p.get("post_type")
    route = (p.get("dialogue") or {}).get("route_plugin")
    if route and pt in {"message", "interaction"}:
        if pt == "interaction" and not f.get("buttons", False):
            return False, "buttons off"
        feature = "at_reply" if route == "reply" else "natural_reply"
        return bool(f.get(feature, feature == "at_reply")), feature + " continuation policy"
    if pt == "message":
        if p.get("at_me"):
            ok = bool(f.get("at_reply", True))
            return ok, "" if ok else "at_reply off"
        ok = bool(f.get("natural_reply", False))
        return ok, "" if ok else "natural_reply off (logged for context only)"
    if pt == "notice":
        nt = p.get("notice_type")
        if nt in ("member_join", "member_leave"):
            ok = bool(f.get("welcome", False))
            return ok, "" if ok else "welcome off"
        return True, ""
    if pt == "interaction":
        ok = bool(f.get("buttons", False))
        return ok, "" if ok else "buttons off"
    return True, ""


def forward(payload: dict) -> str:
    from core.memory import MemoryStore
    from core import conversation
    from core.reply_policy import event_context, repeat_window_seconds, settings, topic_tokens, wake_signature
    memory = MemoryStore(DIR)
    scope = payload.get("group_openid") or "c2c:" + str(payload.get("user_openid") or payload.get("member_openid") or "system")
    delivery = DeliveryStore(DIR / "runtime.sqlite3")
    if payload.get("post_type") in {"message", "interaction"}:
        payload["reply_context"] = event_context(payload)
        target = "/v2/groups/" + scope if payload.get("group_openid") else "/v2/users/" + scope[4:]
        payload["dialogue"] = conversation.record_input(delivery, target, payload, payload["reply_context"])
        if not payload["dialogue"]["valid"]:
            log("ignored expired or foreign clarification choice")
            return "skipped"
    text = payload.get("text_only") or payload.get("raw_message") or ""
    if payload.get("post_type") in {"message", "interaction"} and text:
        memory.add_message(scope, "in:" + str(payload.get("msg_id") or payload.get("event_id") or payload.get("delivery_id")), text,
                           actor=payload.get("member_openid") or payload.get("user_openid") or "",
                           nickname=(payload.get("sender") or {}).get("nickname", ""), reply_to=payload.get("reply_to", ""))
    payload["text"] = text
    payload["context"] = memory.context(scope)
    if "dialogue" in payload:
        # Existing routines already read context; expose the shared policy here
        # without rewriting private group personas or duplicating task history.
        payload["context"]["reply_guidance"] = "先读取 payload.dialogue.pending（同一用户的候选待续问题）。" + conversation.POLICY
    if payload.get("group_openid") and (payload.get("post_type") in {"message", "schedule"}
            or (payload.get("dialogue") or {}).get("route_plugin")):
        from core.plugins import dispatch
        from core.action_runner import run_actions
        decision = dispatch(payload)
        payload["plugin_actions"] = decision["actions"]
        if decision["errors"]:
            if decision["errors"] == ["command:PermissionError"]:
                log("plugin command not authorized locally; forwarding to webhook: " + ",".join(decision["errors"]))
                payload["plugin_errors"] = decision["errors"]
                payload["plugin_results"] = []
                payload["local_actions_completed"] = False
                local_command = bool(payload.get("group_enabled", True))
            else:
                log("plugin request denied: " + ",".join(decision["errors"]))
                return "skipped"
        elif not decision["actions"]:
            return "skipped"
        else:
            payload["plugin_results"] = run_actions(payload, decision["actions"])
            payload["local_actions_completed"] = bool(payload["plugin_results"])
            if decision["actions"] and all(a.get("type") in ("coffee.step", "amap.query") for a in decision["actions"]):
                # luckin_coffee / amap reply by themselves (detached worker); the LLM routine must not answer too.
                log(decision["actions"][0]["type"] + " started locally; not forwarded")
                return "accepted"
            # Explicit commands have already passed their own plugin permission gate.
            local_command = bool(payload["plugin_results"])
    else:
        local_command = False
    ok, why = should_forward(payload)
    if not ok and not local_command:
        log(f"gated {payload.get('post_type')} group={payload.get('group_name') or payload.get('group_openid','')[:8]}: {why}")
        return "skipped"
    repeat_batch = None

    def _drop_unsent_wake(status: str) -> str:
        # A claim that never reached the webhook must not block the next real wake.
        if status in {"failed", "skipped"} and repeat_batch and payload.get("group_openid"):
            try:
                delivery.release_natural_lease(payload["group_openid"], repeat_batch)
            except Exception:
                log("natural lease release failed")
        return status

    if payload.get("post_type") == "message":
        context = payload["reply_context"]
        # A fixed collection window already waited and merged the burst. The
        # leading-edge natural debounce must not drop that single batch.
        batched = isinstance(payload.get("collection"), dict)
        pending = bool((payload.get("dialogue") or {}).get("pending"))
        if payload.get("group_openid") and not pending and (batched or context["trigger"] == "natural"):
            window = repeat_window_seconds(G.load_config(payload["group_openid"]))
            if window > 0:
                signature = wake_signature(payload)
                if signature["batch_id"] and not delivery.claim_natural_forward(
                        scope=payload["group_openid"], batch_id=signature["batch_id"],
                        sources=signature["sources"], tokens=signature["tokens"],
                        anchors=signature["anchors"], weak=signature["weak"],
                        window=window, suppress=not signature["question"]):
                    log("natural repeat suppressed; message retained in context")
                    return "skipped"
                repeat_batch = signature["batch_id"]
        if (not batched and payload.get("group_openid") and context["trigger"] == "natural" and not pending):
            limits = settings(G.load_config(payload["group_openid"]))
            if not delivery.admit_natural(scope, context["topic_key"],
                    topic_tokens(context["topic_text"]), limits["inbound_debounce"]):
                log("natural burst debounced; message retained in context")
                return _drop_unsent_wake("skipped")
    try:
        if payload.get("group_openid"):
            atomic_write_json(G.group_dir(payload["group_openid"]) / "last-forward.json", payload)
    except Exception:
        pass
    url = rd(FORWARDER / "url") or rd(DIR.parent / "forwarder-url")
    key = rd(FORWARDER / "key") or rd(DIR.parent / "forwarder-key")
    if not url.startswith("https://") or not key or key.startswith("PLACEHOLDER"):
        log("skip forward: url/key missing")
        return _drop_unsent_wake("failed")
    body = json.dumps(payload, ensure_ascii=False).encode()
    mid = payload.get("message_id")
    try:
        for action in payload.get("plugin_actions", []):
            current = G.load_config(action["group_openid"])
            if not current or current["revision"] != action["config_revision"] or not G.plugin_enabled(action["group_openid"], action["plugin_id"]):
                return _drop_unsent_wake("skipped")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
                "Idempotency-Key": payload.get("delivery_id", ""),
            },
        )
        class NoWebhookRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError("Webhook redirects are not allowed")
        with urllib.request.build_opener(NoWebhookRedirect()).open(req, timeout=20) as r:
            log(f"forwarded {payload.get('post_type')} mid={mid or payload.get('event_id')} at_me={payload.get('at_me')} -> {r.status}")
            return "accepted"
    except urllib.error.HTTPError as e:
        log(f"forward rejected HTTP {e.code}")
        return _drop_unsent_wake("failed") if e.code < 500 else "unknown"
    except Exception as e:
        log(f"forward outcome unknown mid={mid}")
        return "unknown"


class Bridge:
    """Gateway session with RESUME + fast dead-connection detection.

    Threads per connection (each bound to its own stop Event, so stale threads can't
    touch a newer socket):
      * heartbeat: sends op1 every heartbeat_interval (send has a socket timeout).
      * watchdog: independent of the heartbeat thread; if no op11 ACK for
        DEAD_AFTER seconds it shuts the raw socket down (unblocks a hung send/recv).
    Dispatch events are handed to one worker thread (keeps order) so slow webhook
    POSTs never delay reading ACKs.
    """

    SOCK_TIMEOUT = 15          # seconds; bounds ws.send / partial reads
    ACK_LOG_EVERY = 10         # log 'ack ok' every N acks (~7 min) + first ack per connection

    def __init__(self) -> None:
        self.seq = None            # last dispatch seq (op0 "s"); kept across reconnects for RESUME
        self.session_id = None
        self.heartbeat_ms = 41250
        self.ws = None
        self._stop = threading.Event()
        self.bot_openid = rd(DIR / "bot_openid.txt")
        self.our_group = rd(DIR / "group_openid.txt")
        self.token = ""
        self.token_at = 0.0
        self.last_ack = time.time()
        self.last_hb_sent = 0.0
        self.ack_count = 0
        self.resuming = False      # sent op6, waiting for RESUMED
        self.resume_failures = 0
        self.force_identify = False
        self.close_code = None
        self.store = DeliveryStore()
        self.store.recover()
        # Start the flusher before group workers so a pending ordinary message
        # can park without touching a collector that does not exist yet.
        self.collector = CollectionScheduler(self.store, self._flush_collected, log=log)
        self.q = GroupDispatcher(self.store, self._dispatch, workers=4)
        self.ack_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="interaction-ack")
        self.ack_slots = threading.BoundedSemaphore(32)
        self.last_event_at = 0.0
        self.last_dispatch_at = 0.0
        self.connected = False

    # ---------- helpers ----------
    def ensure_token(self) -> str:
        self.token = get_token()
        self.token_at = time.time()
        return self.token

    def dead_after(self) -> float:
        return max(60.0, self.heartbeat_ms / 1000.0 * 1.5)

    def _send(self, ws, obj: dict) -> None:
        ws.send(json.dumps(obj))

    def _kill_socket(self, ws, why: str) -> None:
        """Force the run_forever loop to return even if a send/recv is hung."""
        log(f"forcing reconnect: {why}")
        try:
            ws.keep_running = False
        except Exception:
            pass
        try:
            raw = ws.sock.sock if ws.sock else None
            if raw:
                raw.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            if ws.sock and ws.sock.sock:
                ws.sock.sock.close()
        except Exception:
            pass

    def _clear_session(self, why: str) -> None:
        if self.session_id or self.seq is not None:
            log(f"session cleared ({why}); next connect will IDENTIFY")
        self.session_id = None
        self.seq = None
        self.resuming = False
        self.resume_failures = 0

    # ---------- per-connection threads ----------
    def _heartbeat_loop(self, ws, stop: threading.Event) -> None:
        while not stop.wait(self.heartbeat_ms / 1000.0):
            try:
                self.last_hb_sent = time.time()
                self._send(ws, {"op": 1, "d": self.seq})
                log(f"heartbeat seq={self.seq}")
            except Exception as e:
                log(f"heartbeat send error: {e!r}")
                if not stop.is_set():
                    self._kill_socket(ws, "heartbeat send failed")
                return

    def _watchdog_loop(self, ws, stop: threading.Event) -> None:
        killed_at = 0.0
        while not stop.wait(5):
            now = time.time()
            if killed_at:
                if now - killed_at > 45:
                    log("run_forever did not return 45s after socket kill; exiting for run-script restart")
                    os._exit(3)
                continue
            gap = now - self.last_ack
            if gap > self.dead_after():
                hb = int(now - self.last_hb_sent) if self.last_hb_sent else -1
                self._kill_socket(ws, f"no heartbeat ACK for {int(gap)}s (last hb send {hb}s ago)")
                killed_at = now

    # ---------- websocket callbacks ----------
    def on_open(self, ws) -> None:
        self.connected = True
        log("ws open")
        self.last_ack = time.time()
        self.ack_count = 0
        threading.Thread(target=self._watchdog_loop, args=(ws, self._stop), daemon=True, name="watchdog").start()

    def on_message(self, ws, message: str) -> None:
        if len(message) > 1024 * 1024:
            self._kill_socket(ws, "gateway message exceeds configured limit")
            return
        try:
            ev = json.loads(message)
        except Exception:
            return
        op = ev.get("op")
        if op == 11:  # heartbeat ACK
            now = time.time()
            self.last_ack = now
            self.ack_count += 1
            self.write_health()
            if self.ack_count == 1 or self.ack_count % self.ACK_LOG_EVERY == 0:
                rtt = int((now - self.last_hb_sent) * 1000) if self.last_hb_sent else -1
                log(f"ack ok n={self.ack_count} seq={self.seq} rtt={rtt}ms")
            return
        if op == 10:  # Hello
            self.last_ack = time.time()
            hi = (ev.get("d") or {}).get("heartbeat_interval") or 41250
            self.heartbeat_ms = int(hi)
            log(f"HELLO heartbeat_ms={self.heartbeat_ms} dead_after={int(self.dead_after())}s")
            if self.session_id and self.seq is not None and not self.force_identify:
                self._send(ws, {"op": 6, "d": {
                    "token": f"QQBot {self.token}",
                    "session_id": self.session_id,
                    "seq": self.seq,
                }})
                self.resuming = True
                log(f"RESUME session={self.session_id} seq={self.seq}")
            else:
                self.force_identify = False
                self._send(ws, {"op": 2, "d": {
                    "token": f"QQBot {self.token}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                }})
                log(f"IDENTIFY intents={INTENTS}")
            threading.Thread(target=self._heartbeat_loop, args=(ws, self._stop), daemon=True, name="heartbeat").start()
            return
        if op == 0:
            t = ev.get("t") or ""
            if t == "READY":
                self.seq = ev.get("s", self.seq)
                d = ev.get("d") or {}
                self.session_id = d.get("session_id")
                self.resuming = False
                self.resume_failures = 0
                user = d.get("user") or {}
                log(f"READY session={self.session_id} user={user.get('username')} id={user.get('id') or ''} seq={self.seq}")
                save_group_event("", ev)  # _system/last-events.jsonl
                return
            if t == "RESUMED":
                self.seq = ev.get("s", self.seq)
                self.resuming = False
                self.resume_failures = 0
                log(f"RESUMED session={self.session_id} seq={self.seq}")
                return
            try:
                self.q.put(ev)  # durable commit precedes the gateway resume cursor
            except Exception:
                self._kill_socket(ws, "durable inbox unavailable")
                raise
            self.seq = ev.get("s", self.seq)
            self.last_event_at = time.time()
            if t == "INTERACTION_CREATE":
                iid = (ev.get("d") or {}).get("id") or ev.get("id")
                if iid and self.ack_slots.acquire(blocking=False):
                    self.ack_pool.submit(self._ack_interaction, iid)
                else:
                    log("interaction ACK capacity exceeded or id missing")
            self.write_health()
            return
        if op == 7:  # server asks us to reconnect (e.g. 4009 session timed out) -> RESUME
            log("op7 reconnect requested (will RESUME)")
            self._kill_socket(ws, "op7")
            return
        if op == 9:
            log(f"INVALID SESSION d={ev.get('d')!r} (resuming={self.resuming})")
            self._clear_session("op9 invalid session")
            self.force_identify = True
            self._kill_socket(ws, "op9")
            return
        log(f"op={op} raw={str(ev)[:200]}")

    def on_error(self, ws, err) -> None:
        log(f"ws error: {err!r}")

    def on_close(self, ws, code=None, reason=None, *a) -> None:
        self.connected = False
        self.write_health()
        log(f"ws closed code={code} reason={reason!r}")
        self.close_code = code
        self._stop.set()

    # ---------- dispatch worker ----------
    def _ack_interaction(self, iid):
        try:
            log(f"INTERACTION ack={interaction_ack(self.ensure_token(), iid)}")
        finally:
            self.ack_slots.release()

    def write_health(self):
        try:
            atomic_write_json(DIR / "health.json", {
                "updated_at": time.time(), "gateway_connected": self.connected,
                "last_ack_at": self.last_ack, "last_event_at": self.last_event_at,
                "last_dispatch_at": self.last_dispatch_at,
                "dispatcher_error": self.q.last_error, **snapshot(self.store.path),
            })
        except Exception:
            log("health publication failed")

    def _collection_decision(self, payload: dict) -> tuple[str, float]:
        """('enroll'|'bypass'|'', seconds). Empty means the fixed window is disarmed."""
        if not payload.get("group_openid") or not hasattr(self, "store"):
            return "", 0.0
        seconds = armed_window_seconds(group_cfg(payload["group_openid"]))
        if seconds <= 0:
            return "", 0.0
        if is_collection_bypass(payload):
            return "bypass", seconds
        return "enroll", seconds

    def _legacy_followup(self, payload: dict) -> None:
        """Same-sender slide used only when collection_window_seconds is off."""
        if not payload.get("group_openid") or not payload.get("member_openid") or not hasattr(self, "store"):
            return
        if "[自定义表情包" in (payload.get("raw_message") or ""):
            return
        scope = "group:" + str(payload["group_openid"])
        wait = 0.0 if payload.get("at_me") else SAME_SENDER_FOLLOWUP_SECONDS

        def absorb(nxt: dict) -> bool:
            save_group_event(payload["group_openid"], nxt)
            follow = to_payload(nxt, self.bot_openid, self.our_group)
            if not follow or "[自定义表情包" in (follow.get("raw_message") or ""):
                return False
            payload["raw_message"] = (payload.get("raw_message") or "") + "\n" + (follow.get("raw_message") or "")
            payload["text_only"] = "\n".join(
                part for part in ((payload.get("text_only") or "").strip(), (follow.get("text_only") or "").strip()) if part)
            payload["attachments"] = (payload.get("attachments") or []) + (follow.get("attachments") or [])
            payload["message"] = (payload.get("message") or []) + (follow.get("message") or [])
            payload["msg_id"] = follow.get("msg_id") or payload.get("msg_id")
            payload["message_id"] = follow.get("message_id") or payload.get("message_id")
            payload["at_me"] = bool(payload.get("at_me") or follow.get("at_me"))
            return True

        coalesce_same_sender(self.store, scope, payload["member_openid"], wait_sec=wait, absorb=absorb, sleep=time.sleep)

    def _flush_collected(self, payload: dict) -> str:
        goid = payload.get("group_openid") or ""
        cfg = group_cfg(goid) if goid else None
        if cfg:
            payload["group_enabled"] = bool(cfg.get("enabled", True))
            payload["features"] = cfg.get("features") or payload.get("features") or {}
            if cfg.get("name"):
                payload["group_name"] = cfg.get("name") or ""
            aliases = cfg.get("aliases") or []
            if aliases:
                payload["group_alias"] = aliases[0]
        count = len((payload.get("collection") or {}).get("messages") or [])
        log(f"collection flush group={payload.get('group_alias') or goid[:8]} n={count} delivery={payload.get('delivery_id')}")
        return forward(payload)

    def _dispatch(self, ev: dict) -> str | None:
        self.last_dispatch_at = time.time()
        t = ev.get("t") or ""
        if t in ("GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
            d = ev.get("d") or {}
            save_group_event(d.get("group_openid") or d.get("group_id") or "", ev)
            # learn bot openid from mentions.is_you
            for m in d.get("mentions") or []:
                if isinstance(m, dict) and m.get("is_you") and m.get("id"):
                    if m["id"] != self.bot_openid:
                        self.bot_openid = m["id"]
                        try:
                            atomic_write_text(DIR / "bot_openid.txt", self.bot_openid + "\n")
                        except Exception:
                            pass
                    break
            payload = to_payload(ev, self.bot_openid, self.our_group)
            if payload:
                payload["delivery_id"] = ev.get("_delivery_id", event_identity(ev)[0])
                decision, seconds = self._collection_decision(payload)
                if decision == "enroll":
                    _, scope = event_identity(ev)
                    parked = self.store.enroll_collection(
                        scope=scope, delivery_id=payload["delivery_id"], payload=payload,
                        window_seconds=seconds)
                    if parked.get("ordinal") == 1:
                        log(f"collection open group={payload.get('group_openid','')[:8]} "
                            f"seconds={seconds:g} deadline={parked['deadline']:.3f}")
                    self.collector.wake()
                    return "collecting"
                if decision != "bypass":
                    self._legacy_followup(payload)
                log(
                    f"recv {t} s={ev.get('s')} [{payload.get('group_alias') or payload.get('group_name') or payload['group_openid'][:8]}] "
                    f"from={payload.get('sender', {}).get('nickname')} "
                    f"at_me={payload['at_me']} text={payload['raw_message'][:80]!r}"
                )
                return forward(payload)
            else:
                goid = (d.get("group_openid") or d.get("group_id") or "")[:16]
                log(f"skip {t} group={goid}...")
            return
        if t == "INTERACTION_CREATE":
            d = ev.get("d") or {}
            iid = d.get("id") or ev.get("id") or ""
            log(f"INTERACTION type={d.get('type')} queued independently of ACK")
            self._save_event(ev)
            p = interaction_payload(ev, self.our_group)
            if p:
                p["delivery_id"] = ev.get("_delivery_id", event_identity(ev)[0])
                return forward(p)
            return
        if t in NOTICE_EVENTS:
            log(f"notice {t} raw={json.dumps(ev.get('d'), ensure_ascii=False)[:200]}")
            self._save_event(ev)
            p = notice_payload(ev, self.our_group)
            if p:
                p["delivery_id"] = ev.get("_delivery_id", event_identity(ev)[0])
                return forward(p)
            return
        if t == "C2C_MESSAGE_CREATE":
            self._save_event(ev)
            p = c2c_payload(ev)
            log(f"recv C2C from={p['sender']['nickname']} text={p['raw_message'][:80]!r}")
            p["delivery_id"] = ev.get("_delivery_id", event_identity(ev)[0])
            return forward(p)
            return
        log(f"event {t} raw={json.dumps(ev, ensure_ascii=False)[:300]}")

    def _save_event(self, ev: dict) -> None:
        d = ev.get("d") or {}
        goid = d.get("group_openid") if isinstance(d, dict) else ""
        if goid:
            save_group_event(goid, ev)
            return
        author = d.get("author") or {}
        user = author.get("user_openid") or author.get("id") or d.get("user_openid") or "system"
        from core.files import confined_path
        append_jsonl(confined_path(G.C2C_DIR, str(user)) / "last-events.jsonl", ev)

    # ---------- main loop ----------
    def run_forever(self) -> None:
        try:
            import websocket
        except ImportError:
            raise RuntimeError("Install pinned dependencies before starting the gateway") from None
        websocket.setdefaulttimeout(self.SOCK_TIMEOUT)
        sockopt = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
        for name, val in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sockopt.append((socket.IPPROTO_TCP, getattr(socket, name), val))
        if hasattr(socket, "TCP_USER_TIMEOUT"):  # unacked outgoing data -> error after 30s
            sockopt.append((socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 30000))

        while True:
            self._stop.set()           # stop threads of the previous connection
            self._stop = threading.Event()
            self.close_code = None
            delay = 3
            try:
                token = self.ensure_token()
                gw = get_gateway(token)
                log(f"connecting {gw} (session={'resume' if self.session_id and self.seq is not None and not self.force_identify else 'new'})")
                self.ws = websocket.WebSocketApp(
                    gw,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                self.ws.run_forever(ping_interval=0, sockopt=tuple(sockopt), reconnect=0)
            except Exception as e:
                log(f"loop error: {e}")
                if "401" in str(e):
                    # cached access token went stale: drop it so the next try fetches a fresh one
                    self.token = ""
                    qq_client.invalidate_token()
                    self.token_at = 0.0
                    _token_holder["t"] = ""
                    _token_holder["at"] = 0.0
                    log("401 -> cleared cached token")
            self._stop.set()
            code = self.close_code
            if self.resuming:
                # socket closed before RESUMED arrived
                self.resuming = False
                self.resume_failures += 1
                log(f"RESUME failed (#{self.resume_failures}, close code={code})")
                if self.resume_failures >= 2:
                    self._clear_session("resume failed twice")
            if code in (4006, 4007):  # invalid session / invalid seq -> cannot resume
                self._clear_session(f"close code {code}")
            elif code in (4914, 4915):  # bot offline / banned (sandbox / punished)
                log(f"close code {code}: bot offline/banned; slow retry")
                delay = 60
            elif code == 4008:  # rate limited
                delay = 15
            elif code == 4009:  # session timed out (QQ does this ~hourly) -> RESUME right away
                delay = 1
            log(f"reconnect in {delay}s")
            time.sleep(delay)


def main() -> None:
    log("official-ws-bridge starting")
    log(f"group_openid={rd(DIR / 'group_openid.txt')} bot_openid={rd(DIR / 'bot_openid.txt')}")
    br = Bridge()
    try:
        import signal

        # kill -USR1 <pid>: drop the socket but keep the session -> reconnect + RESUME (test / external watchdog)
        signal.signal(signal.SIGUSR1, lambda *_: br.ws and br._kill_socket(br.ws, "SIGUSR1"))
    except Exception:
        pass
    br.run_forever()


if __name__ == "__main__":
    try:
        with file_lock(DIR / "bridge.process.lock", timeout=0):
            main()
    except TimeoutError:
        raise SystemExit("A bridge already owns this data directory")
