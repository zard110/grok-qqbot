#!/usr/bin/env python3
"""Per-group registry for MaxShenBi (shared by bridge, send_group.py, kb.py).

groups/<group_openid>/config.json  — name, aliases, enabled, features, style, admins, knowledge
groups/<group_openid>/notes.md     — group-specific memory
groups/<group_openid>/knowledge/   — docs (md/txt/pdf) + repos/
groups/<group_openid>/sent-log.jsonl, .send.lock, last-events.jsonl
groups/_default/config.json        — template for auto-created groups
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path

DIR = Path("/workspace/qq-bot-persist/official-bot")
GROUPS = DIR / "groups"
DEFAULT_DIR = GROUPS / "_default"
C2C_DIR = DIR / "c2c"
SYSTEM_DIR = DIR / "_system"
OPENID_RE = re.compile(r"^[0-9A-F]{32}$")

FALLBACK_DEFAULT = {
    "name": "",
    "aliases": [],
    "group_openid": "",
    "qq_group_id": None,
    "enabled": True,
    "features": {
        "natural_reply": False,
        "at_reply": True,
        "daily_ai_news": False,
        "x_info_push": False,
        "image_gen": False,
        "buttons": False,
        "welcome": False,
        "media_understanding": True,
        "knowledge": True,
    },
    "style": "礼貌、简洁，用中文回答；只在被 @ 时回复。",
    "admins": [],
    "knowledge": {"docs_dir": "knowledge/", "repos": []},
    "auto_created": True,
}


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return copy.deepcopy(default)


def default_config() -> dict:
    cfg = copy.deepcopy(FALLBACK_DEFAULT)
    d = _read_json(DEFAULT_DIR / "config.json", {}) or {}
    for k, v in d.items():
        if k == "features" and isinstance(v, dict):
            cfg["features"].update(v)
        elif not k.startswith("_"):
            cfg[k] = v
    return cfg


def group_dir(goid: str) -> Path:
    return GROUPS / goid


def load_config(goid: str) -> dict | None:
    """Config merged over defaults (missing feature keys fall back to default). None if folder/config absent."""
    p = group_dir(goid) / "config.json"
    if not p.exists():
        return None
    raw = _read_json(p, {}) or {}
    cfg = default_config()
    cfg["auto_created"] = False
    for k, v in raw.items():
        if k == "features" and isinstance(v, dict):
            cfg["features"].update(v)
        else:
            cfg[k] = v
    cfg["group_openid"] = goid
    return cfg


def ensure_group(goid: str, name: str = "", extra: dict | None = None) -> tuple[dict, bool]:
    """Return (config, created). Creates folder from _default for unknown groups."""
    cfg = load_config(goid)
    if cfg is not None:
        return cfg, False
    gd = group_dir(goid)
    (gd / "knowledge").mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    cfg.update({"group_openid": goid, "name": name or f"未命名群-{goid[:6]}", "auto_created": True,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if extra:
        cfg.update(extra)
    (gd / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not (gd / "notes.md").exists():
        (gd / "notes.md").write_text(f"# {cfg['name']} 群笔记\n\n（自动创建；在这里记录本群的事实、约定、常见问题）\n", encoding="utf-8")
    return load_config(goid) or cfg, True


def update_config(goid: str, **fields) -> None:
    p = group_dir(goid) / "config.json"
    raw = _read_json(p, {}) or {}
    raw.update(fields)
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def all_groups() -> list[dict]:
    out = []
    if GROUPS.exists():
        for d in sorted(GROUPS.iterdir()):
            if d.is_dir() and OPENID_RE.match(d.name):
                c = load_config(d.name)
                if c:
                    out.append(c)
    return out


def default_group_openid() -> str:
    return (DIR / "group_openid.txt").read_text(encoding="utf-8").strip()


def resolve(ref: str | None) -> str:
    """alias / name / group_openid / qq number → group_openid. None/'' → default (Zwift)."""
    if not ref:
        return default_group_openid()
    r = ref.strip()
    if OPENID_RE.match(r.upper()) and group_dir(r.upper()).exists():
        return r.upper()
    low = r.lower()
    for c in all_groups():
        names = [str(a).lower() for a in c.get("aliases") or []] + [str(c.get("name") or "").lower()]
        if low in names or (c.get("qq_group_id") and str(c.get("qq_group_id")) == r):
            return c["group_openid"]
    if OPENID_RE.match(r.upper()):
        return r.upper()  # unknown but well-formed openid: allow
    raise SystemExit(f"unknown group: {ref!r} (known: {[c.get('aliases') or c.get('name') for c in all_groups()]})")
