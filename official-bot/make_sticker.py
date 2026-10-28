#!/usr/bin/env python3
"""Generate or pick斗图表情包 (custom multi-character meme stickers, animated GIFs, or ready library stickers).

Usage:
  # 1. Generate static custom meme with specific or random template (25 templates available):
  python3 make_sticker.py --template random --text "就这？|重构了个寂寞" --out /workspace/napcat/media/meme_out.jpg
  python3 make_sticker.py --template husky_disdain --text "我对你的功率|嗤之以鼻孔" --out /workspace/napcat/media/meme_husky.jpg

  # 2. Generate animated GIF custom meme (shake | zoom | bounce):
  python3 make_sticker.py --template pepe_smug --animate shake --text "看破不说破|继续听你吹" --out /workspace/napcat/media/meme_shake.gif

  # 3. Pick a ready sticker from library/group collection by keyword/mood (with anti-repeat memory):
  python3 make_sticker.py --pick "二哈 鄙视"
  python3 make_sticker.py --pick "打工人 摸鱼" --prefer-gif

  # 4. List all templates & library summary:
  python3 make_sticker.py --list
"""
from __future__ import annotations
import argparse
import contextvars
import functools
import hashlib
import io
import json
import math
import random
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import groups as G
from core.files import atomic_write_bytes, atomic_write_json, confined_path, file_lock

DIR = G.DIR


@dataclass(frozen=True)
class StickerPaths:
    group_openid: str
    stickers: Path
    templates: Path
    index: Path
    recent: Path


_SCOPE = contextvars.ContextVar("sticker_scope", default=None)


@contextmanager
def use_group(group: str | None = None):
    if group is None and _SCOPE.get() is not None:
        yield _SCOPE.get()
        return
    try:
        goid = G.resolve(group)
    except FileNotFoundError:
        if group is not None:
            raise
        # Preserve the old single-group CLI without an environment migration.
        # Explicit scopes never enter this path, and registered groups still
        # require their own policy instead of borrowing the legacy library.
        try:
            goid = G.resolve("zwift")
        except G.ConfigError:
            legacy = confined_path(G.GROUPS, "zwift/stickers")
            candidates = {confined_path(G.GROUPS, child.name + "/stickers")
                          for child in G.GROUPS.iterdir()
                          if child.is_dir() and child.name != "_default" and (child / "stickers").is_dir()} if G.GROUPS.exists() else set()
            if G.all_groups() or candidates != {legacy} or not legacy.is_dir():
                raise ValueError("Specify --group or configure group_openid.txt")
            paths = StickerPaths("legacy:zwift", legacy, legacy / "templates", legacy / "index.json", legacy / ".recent_used.json")
            token = _SCOPE.set(paths)
            try:
                yield paths
            finally:
                _SCOPE.reset(token)
            return
    if not G.plugin_enabled(goid, "sticker"):
        raise PermissionError("Sticker plugin is disabled or group is not registered")
    directory = confined_path(G.group_dir(goid), "stickers")
    paths = StickerPaths(goid, directory, directory / "templates", directory / "index.json", directory / ".recent_used.json")
    token = _SCOPE.set(paths)
    try:
        yield paths
    finally:
        _SCOPE.reset(token)


def _paths() -> StickerPaths:
    paths = _SCOPE.get()
    if paths is None:
        raise RuntimeError("Sticker operation requires a group scope")
    return paths


def group_scoped(fn):
    @functools.wraps(fn)
    def wrapped(*args, group=None, **kwargs):
        with use_group(group):
            with file_lock(_paths().stickers / ".library.lock"):
                return fn(*args, **kwargs)
    return wrapped

FONT_PATHS = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/workspace/video_studio/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

TEMPLATE_CATEGORIES = {
    "dog": ["husky_disdain", "husky_cocky", "husky_stare", "shiba_unhappy", "cheems_gun", "doge_smirk"],
    "cat": ["cat_mur", "tom_sleepy"],
    "cartoon": ["pepe_smug", "duck_laugh", "jerry_affirm", "squidward_point"],
    "panda": [
        "shock", "agree", "dalao", "ok", "smirk", "huh",
        "panda_wow", "panda_sunglasses", "panda_muscle", "panda_touch",
        "panda_knife", "panda_point", "panda_sweat",
    ],
}


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for fp in FONT_PATHS:
        if Path(fp).exists():
            return ImageFont.truetype(fp, size)
    return ImageFont.load_default()


@group_scoped
def load_recent() -> dict:
    if _paths().recent.exists():
        try:
            data = json.loads(_paths().recent.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "recent_stickers": list(data.get("recent_stickers", []))[-25:],
                    "recent_templates": list(data.get("recent_templates", []))[-25:],
                }
        except Exception:
            pass
    return {"recent_stickers": [], "recent_templates": []}


@group_scoped
def save_recent(data: dict) -> None:
    trimmed = {
        "recent_stickers": list(data.get("recent_stickers", []))[-25:],
        "recent_templates": list(data.get("recent_templates", []))[-25:],
    }
    atomic_write_json(_paths().recent, trimmed)


@group_scoped
def choose_template(template_arg: str, category: str | None = None) -> str:
    available = sorted(p.stem for p in _paths().templates.glob("*.png"))
    if not available:
        return "smirk"

    recent_data = load_recent()
    recent_tpls = recent_data["recent_templates"]

    if template_arg and template_arg != "random":
        if template_arg not in available:
            raise ValueError("Unknown sticker template")
        chosen = template_arg
    else:
        pool = available
        if category and category in TEMPLATE_CATEGORIES:
            cat_pool = [t for t in TEMPLATE_CATEGORIES[category] if t in available]
            if cat_pool:
                pool = cat_pool
        fresh = [t for t in pool if t not in recent_tpls]
        if not fresh:
            fresh = [t for t in pool if t not in recent_tpls[-4:]] or pool
        chosen = random.choice(fresh)

    return chosen


def record_template(template: str) -> None:
    recent = load_recent()
    recent["recent_templates"] = [x for x in recent["recent_templates"] if x != template] + [template]
    save_recent(recent)


def parse_lines(text: str) -> list[str]:
    lines = [
        ln.strip()
        for ln in text.replace("\\n", "\n").replace("|", "\n").splitlines()
        if ln.strip()
    ]
    if len(lines) > 2 or any(len(line) > 32 for line in lines):
        raise ValueError("Caption must be one or two short lines (up to 32 characters each); do not squeeze a paragraph into a sticker")
    return lines or ["？"]


def draw_text_lines(
    canvas: Image.Image,
    lines: list[str],
    y_start: int,
    W: int,
    text_offset: tuple[int, int] = (0, 0),
    highlight_color: tuple[int, int, int] = (15, 15, 15),
) -> None:
    draw = ImageDraw.Draw(canvas)
    y = y_start + text_offset[1]
    for ln in lines:
        fsize = 30 if len(lines) == 1 else 26
        font = load_font(fsize)
        while fsize > 14:
            bbox = draw.textbbox((0, 0), ln, font=font)
            tw = bbox[2] - bbox[0]
            if tw <= W - 16:
                break
            fsize -= 2
            font = load_font(fsize)
        bbox = draw.textbbox((0, 0), ln, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        if tw > W - 16:
            raise ValueError("Caption is too wide; shorten it or split into two lines with |")
        tx = (W - tw) // 2 + text_offset[0]
        # Subtle white halo + crisp dark text
        for ox, oy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((tx + ox, y + oy), ln, fill=(255, 255, 255), font=font)
        draw.text((tx, y), ln, fill=highlight_color, font=font)
        y += th + 10


def render_static_frame(head: Image.Image, lines: list[str], W: int = 300) -> tuple[Image.Image, int, int, int]:
    target_h = 205 if len(lines) == 1 else 190
    scale = min((W - 20) / head.width, target_h / head.height)
    nw, nh = max(1, int(head.width * scale)), max(1, int(head.height * scale))
    head_resized = head.resize((nw, nh), Image.Resampling.LANCZOS)

    H = nh + (62 if len(lines) == 1 else 92)
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    canvas.paste(head_resized, ((W - nw) // 2, 6))
    draw_text_lines(canvas, lines, nh + 10, W)
    return canvas, nw, nh, H


@group_scoped
def make_meme(
    template: str,
    text: str,
    out_path: str,
    animate: str | None = None,
    category: str | None = None,
    embed_credit_footer: bool = False,
) -> tuple[str, str]:
    chosen_tpl = choose_template(template, category=category)
    tpl_file = _paths().templates / f"{chosen_tpl}.png"
    if not tpl_file.exists():
        tpl_file = _paths().templates / "smirk.png"
    with Image.open(tpl_file) as picture:
        rgba = picture.convert("RGBA")
        head = Image.new("RGB", picture.size, "white")
        head.paste(rgba, (0, 0), rgba)
    index = json.loads(_paths().index.read_text(encoding="utf-8")) if _paths().index.exists() else {}
    license_record = index.get("template_licenses", {}).get(chosen_tpl)
    from core.sticker_pack import attribution_frame

    def provenance(out):
        if license_record:
            atomic_write_json(Path(str(out) + ".license.json"), {**license_record,
                "modified": True, "changes": "Custom caption/layout" + (" and animation" if animate else "") + (" and visible attribution footer" if embed_credit_footer else ""),
                "embed_credit_footer": embed_credit_footer,
                "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest()})
    lines = parse_lines(text)
    W = 300

    base_canvas, nw, nh, H = render_static_frame(head, lines, W=W)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not animate:
        buffer = io.BytesIO()
        if license_record:
            base_canvas = attribution_frame(base_canvas, license_record, embed_credit_footer=embed_credit_footer)
        base_canvas.save(buffer, format="PNG" if out.suffix.lower() == ".png" else "JPEG", quality=92)
        atomic_write_bytes(out, buffer.getvalue())
        provenance(out)
        record_template(chosen_tpl)
        return str(out), chosen_tpl

    # Animated GIF generation (shake | zoom | bounce)
    if out.suffix.lower() != ".gif":
        out = out.with_suffix(".gif")

    head_resized = head.resize((nw, nh), Image.Resampling.LANCZOS)
    frames: list[Image.Image] = []
    n_frames = 8

    for i in range(n_frames):
        frame = Image.new("RGB", (W, H), (255, 255, 255))
        phase = (2 * math.pi * i) / n_frames

        if animate == "shake":
            dx = int(round(6 * math.sin(i * 2.3)))
            dy = int(round(4 * math.cos(i * 3.1)))
            angle = 3.5 * math.sin(i * 2.5)
            rotated = head_resized.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(255, 255, 255))
            frame.paste(rotated, ((W - nw) // 2 + dx, max(0, 6 + dy)))
            tdx = int(round(3 * math.cos(i * 2.7)))
            tdy = int(round(2 * math.sin(i * 2.7)))
            color = (180, 15, 15) if i % 3 == 1 else (15, 15, 15)
            draw_text_lines(frame, lines, nh + 10, W, text_offset=(tdx, tdy), highlight_color=color)

        elif animate == "zoom":
            # Dramatic zoom-in pulse toward center of character face
            zoom_factor = 1.0 + 0.22 * (0.5 - 0.5 * math.cos(phase))
            zw, zh = int(nw * zoom_factor), int(nh * zoom_factor)
            zoomed = head.resize((zw, zh), Image.Resampling.LANCZOS)
            cx = max(0, (zw - nw) // 2)
            cy = max(0, int((zh - nh) * 0.38))
            cropped = zoomed.crop((cx, cy, cx + nw, cy + nh))
            frame.paste(cropped, ((W - nw) // 2, 6))
            tdy = -2 if zoom_factor > 1.12 else 0
            color = (200, 10, 10) if zoom_factor > 1.15 else (15, 15, 15)
            draw_text_lines(frame, lines, nh + 10, W, text_offset=(0, tdy), highlight_color=color)

        elif animate == "bounce":
            # Elastic squash & stretch bounce
            s = math.sin(phase)
            sx = 1.0 - 0.08 * s
            sy = 1.0 + 0.10 * s
            bw = max(1, int(nw * sx))
            bh = max(1, int(nh * sy))
            bounced = head.resize((bw, bh), Image.Resampling.LANCZOS)
            by = max(0, 6 + (nh - bh) - int(abs(s) * 8))
            if by + bh > nh + 10:
                bounced = bounced.crop((0, 0, bw, max(1, nh + 10 - by)))
            frame.paste(bounced, ((W - bw) // 2, by))
            draw_text_lines(frame, lines, nh + 10, W, text_offset=(0, int(2 * s)))

        else:
            frame = base_canvas.copy()

        if license_record:
            frame = attribution_frame(frame, license_record, embed_credit_footer=embed_credit_footer)
        # Quantize frame for clean compact GIF
        frames.append(frame.quantize(colors=128, method=Image.Quantize.MEDIANCUT))

    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=55,
        loop=0,
        optimize=True,
    )
    atomic_write_bytes(out, buffer.getvalue())
    provenance(out)
    record_template(chosen_tpl)
    return str(out), chosen_tpl


@group_scoped
def pick_sticker(query: str, prefer_gif: bool = False) -> dict:
    if not _paths().index.exists():
        return {"ok": False, "error": "index.json not found"}
    idx_data = json.loads(_paths().index.read_text(encoding="utf-8"))
    stickers = idx_data.get("ready_stickers", [])
    if not stickers:
        return {"ok": False, "error": "no ready_stickers in index.json"}

    recent_data = load_recent()
    recent_files = set(recent_data["recent_stickers"])

    # Split query into tokens
    tokens = [t.strip().lower() for t in re.split(r"[\s,|/，、]+", query) if t.strip()]
    want_gif = prefer_gif or any(k in query.lower() for k in ["动图", "gif", "会动"])

    scored = []
    for item in stickers:
        rel_file = item.get("file", "")
        full_path = confined_path(_paths().stickers, rel_file)
        if not full_path.exists():
            continue
        text_lower = (item.get("text", "") or "").lower()
        cat_lower = (item.get("category", "") or "").lower()
        tags_lower = [str(t).lower() for t in item.get("tags", [])]
        haystack = " ".join([text_lower, cat_lower, rel_file.lower()] + tags_lower)

        score = 0.0
        for tok in tokens:
            if tok in ("动图", "gif", "会动"):
                continue
            if tok in tags_lower:
                score += 4.0
            elif any(tok in tg or tg in tok for tg in tags_lower):
                score += 2.5
            if tok in text_lower:
                score += 3.0
            elif tok in haystack:
                score += 1.5
            # Character/category aliases
            if tok in ("狗", "狗子", "狗头", "二哈", "哈士奇", "柴犬", "cheems", "doge") and cat_lower == "dog":
                score += 1.5
            elif tok in ("猫", "猫猫", "猫咪", "汤姆", "喵") and cat_lower == "cat":
                score += 1.5
            elif tok in ("蛙", "悲伤蛙", "鸭", "鸭子", "杰瑞", "章鱼哥", "loopy") and cat_lower == "cartoon":
                score += 1.5

        if want_gif and item.get("is_gif"):
            score += 2.0

        is_recent = rel_file in recent_files
        scored.append((score, not is_recent, item))

    # Prefer non-recent items with highest score (and strictly prefer GIF if requested and available)
    non_recent_matches = [x for x in scored if x[1] and x[0] > 0]
    if want_gif:
        gif_nr = [x for x in non_recent_matches if x[2].get("is_gif")]
        if gif_nr:
            non_recent_matches = gif_nr
    if non_recent_matches:
        max_sc = max(x[0] for x in non_recent_matches)
        top_pool = [x[2] for x in non_recent_matches if x[0] >= max_sc * 0.75]
        chosen = random.choice(top_pool)
    else:
        any_matches = [x for x in scored if x[0] > 0]
        if any_matches:
            max_sc = max(x[0] for x in any_matches)
            top_pool = [x[2] for x in any_matches if x[0] >= max_sc * 0.75]
            chosen = random.choice(top_pool)
        else:
            return {"ok": False, "error": "no_match", "group_openid": _paths().group_openid}

    rel_file = chosen["file"]
    recent_list = recent_data["recent_stickers"]
    if rel_file in recent_list:
        recent_list.remove(rel_file)
    recent_list.append(rel_file)
    recent_data["recent_stickers"] = recent_list[-25:]
    save_recent(recent_data)

    return {
        "ok": True,
        "path": str(confined_path(_paths().stickers, rel_file)),
        "file": rel_file,
        "text": chosen.get("text", ""),
        "category": chosen.get("category", ""),
        "tags": chosen.get("tags", []),
        "is_gif": bool(chosen.get("is_gif", False)),
    }



@group_scoped
def search_candidates(query: str, top_k: int = 8) -> dict:
    """Search across ALL 142+ ready stickers AND 25 templates, returning the top candidates with their text/description and whether they were recently used."""
    if not _paths().index.exists():
        return {"ok": False, "error": "index.json not found"}
    idx_data = json.loads(_paths().index.read_text(encoding="utf-8"))
    stickers = idx_data.get("ready_stickers", [])
    templates = idx_data.get("templates", {})
    recent_data = load_recent()
    recent_files = set(recent_data["recent_stickers"][-20:])
    recent_tpls = set(recent_data["recent_templates"][-15:])

    tokens = [t.strip().lower() for t in re.split(r"[\s,|/，、]+", query) if t.strip()]

    sticker_hits = []
    for item in stickers:
        rel_file = item.get("file", "")
        full_path = confined_path(_paths().stickers, rel_file)
        if not full_path.exists():
            continue
        text_lower = (item.get("text", "") or "").lower()
        cat_lower = (item.get("category", "") or "").lower()
        tags_lower = [str(t).lower() for t in item.get("tags", [])]
        haystack = " ".join([text_lower, cat_lower, rel_file.lower()] + tags_lower)
        score = 0.0
        for tok in tokens:
            if tok in tags_lower:
                score += 4.5
            elif any(tok in tg or tg in tok for tg in tags_lower):
                score += 3.0
            if tok in text_lower:
                score += 3.5
            elif any(ch in text_lower for ch in tok if len(tok) >= 2):
                score += 1.0
            if tok in haystack:
                score += 1.5
        relevance = score
        if relevance <= 0:
            continue
        if item.get("is_gif"):
            score += 0.5
        used_recently = rel_file in recent_files
        if used_recently:
            score -= 6.0
        sticker_hits.append((score, random.random(), {
            "type": "ready_sticker",
            "path": str(full_path),
            "file": rel_file,
            "text": item.get("text", ""),
            "category": item.get("category", ""),
            "tags": item.get("tags", []),
            "is_gif": bool(item.get("is_gif", False)),
            "used_recently": used_recently,
            "score": round(score, 1),
        }))

    sticker_hits.sort(key=lambda x: (x[0], x[1]), reverse=True)

    tpl_hits = []
    for tpl_name, desc in templates.items():
        desc_lower = desc.lower()
        score = 0.0
        for tok in tokens:
            if tok in desc_lower or tok in tpl_name.lower():
                score += 4.0
            elif any(ch in desc_lower for ch in tok if len(tok) >= 2):
                score += 1.2
        used_recently = tpl_name in recent_tpls
        if score <= 0:
            continue
        if used_recently:
            score -= 8.0
        tpl_hits.append((score, random.random(), {
            "type": "template",
            "template": tpl_name,
            "description": desc,
            "used_recently": used_recently,
            "score": round(score, 1),
        }))
    tpl_hits.sort(key=lambda x: (x[0], x[1]), reverse=True)

    return {
        "ok": True,
        "query": query,
        "top_ready_stickers": [x[2] for x in sticker_hits[:top_k]],
        "top_templates": [x[2] for x in tpl_hits[:5]],
        "recent_templates_avoided": list(recent_data["recent_templates"][-10:]),
    }



@group_scoped
def add_sticker_to_index(file_arg: str, text: str, category: str = "panda", tags_str: str = "") -> dict:
    """Copy a validated image into this group's content-addressed managed library."""
    candidate = Path(file_arg)
    if not candidate.is_absolute():
        # The old CLI accepted stickers/name.gif; resolve only inside this group.
        name = candidate.name if candidate.parts[:1] == ("stickers",) else file_arg
        candidate = confined_path(_paths().stickers, name)
    if not candidate.is_file():
        return {"ok": False, "error": "sticker_not_found"}
    if candidate.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Sticker exceeds 5 MiB")
    content = candidate.read_bytes()
    if len(content) > 5 * 1024 * 1024:
        raise ValueError("Sticker exceeds 5 MiB")
    with Image.open(io.BytesIO(content)) as picture:
        image_format = picture.format
        if image_format not in {"PNG", "JPEG", "GIF", "WEBP"}:
            raise ValueError("Unsupported sticker image format")
        if picture.width * picture.height > 20_000_000:
            raise ValueError("Sticker dimensions exceed the limit")
        picture.verify()
    digest = hashlib.sha256(content).hexdigest()
    suffix = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}[image_format]
    filename = digest + suffix
    destination = confined_path(_paths().stickers, filename)
    data = json.loads(_paths().index.read_text(encoding="utf-8")) if _paths().index.exists() else {"templates": {}, "ready_stickers": []}
    stickers = data.setdefault("ready_stickers", [])
    # Existing legacy files receive a hash only during explicit ingestion, not every search.
    for item in stickers:
        existing = confined_path(_paths().stickers, item.get("file", ""))
        if not item.get("sha256") and existing.is_file():
            item["sha256"] = hashlib.sha256(existing.read_bytes()).hexdigest()
        if item.get("sha256") == digest:
            if text:
                item["text"] = text
            if tags_str:
                item["tags"] = [x for x in re.split(r"[\s,|/，、]+", tags_str) if x]
            atomic_write_json(_paths().index, data)
            return {"ok": True, "status": "duplicate_sha256_skipped", "existing_file": item["file"], "total": len(stickers)}
    atomic_write_bytes(destination, content)
    entry = {"file": filename, "sha256": digest, "original_name": candidate.name,
             "text": text or candidate.stem, "category": category,
             "tags": [x for x in re.split(r"[\s,|/，、]+", tags_str) if x] or ["群友存图"],
             "is_gif": image_format == "GIF", "source": "group_collected"}
    stickers.append(entry)
    data["summary"] = {"total_ready_stickers": len(stickers),
                       "group_collected": sum(x.get("source") == "group_collected" for x in stickers),
                       "gif_count": sum(bool(x.get("is_gif")) for x in stickers)}
    atomic_write_json(_paths().index, data)
    return {"ok": True, "status": "added", "entry": entry, "total": len(stickers)}


@group_scoped
def recommend_sticker(query: str, context: str = "", previous: str = "", seed: str = "") -> dict:
    """Pure recommendation over this group's existing assets; no send/usage mutation."""
    from core.sticker_policy import recommend
    data = json.loads(_paths().index.read_text(encoding="utf-8")) if _paths().index.exists() else {}
    items = [row for row in data.get("ready_stickers", []) if confined_path(_paths().stickers, row.get("file", "")).is_file()]
    recent = load_recent()["recent_stickers"][-8:]
    from core.delivery import DeliveryStore
    with DeliveryStore().connect() as db:
        rows = db.execute("SELECT metadata FROM reply_claims r JOIN outbox o ON o.id=r.action_id WHERE r.scope=? AND json_extract(r.metadata,'$.sticker')=1 AND o.status IN ('sent','partial') ORDER BY r.created DESC LIMIT 8",
                          ("/v2/groups/" + _paths().group_openid,)).fetchall()
    sent_hashes = {value for row in rows if (value := json.loads(row["metadata"]).get("sticker_hash"))}
    recent += [row["file"] for row in items if row.get("sha256") in sent_hashes]
    result = recommend(items, query, context, previous, seed, recent)
    if result.get("should_send"):
        result["path"] = str(confined_path(_paths().stickers, result["file"]))
    return result



def main() -> None:
    ap = argparse.ArgumentParser(description="Generate or pick custom斗图表情包")
    ap.add_argument("--template", default="random", help="Template name or 'random' (25 templates available)")
    ap.add_argument("--category", default=None, help="Optional category filter for --template random: dog | cat | cartoon | panda")
    ap.add_argument("--text", help="1-2 lines of custom caption separated by | or newline")
    ap.add_argument("--animate", choices=["shake", "zoom", "bounce"], default=None, help="Generate animated GIF sticker")
    ap.add_argument("--pick", default=None, help="Pick a ready sticker by keyword/emotion/character tag (avoids last 25 used)")
    ap.add_argument("--search", default=None, help="Search all 142+ ready stickers and 25 templates for the best contextual match")
    ap.add_argument("--prefer-gif", action="store_true", help="Prefer GIF when using --pick")
    ap.add_argument("--out", default=None, help="Output path for generated meme")
    ap.add_argument("--list", action="store_true", help="List all templates and ready stickers index")
    ap.add_argument("--add-sticker", default=None, help="Copy a validated image into this group's library with SHA-256 dedup")
    ap.add_argument("--tags", default="", help="Comma/space separated tags when using --add-sticker")
    ap.add_argument("--group", help="Group alias or openid (default configured group)")
    ap.add_argument("--recommend", help="contextual reaction advice without sending or recording selection")
    ap.add_argument("--context", default="", help="recent conversation (current query has higher weight)")
    ap.add_argument("--previous-tag", default="", help="previous reaction mood for a complementary reply chain")
    ap.add_argument("--seed", default="", help="source event identity for stable selection on retries")
    ap.add_argument("--embed-credit-footer", action="store_true", help="paint an optional credit footer; license sidecars are always retained")
    args = ap.parse_args()
    with use_group(args.group):
        run_cli(args, ap)


def run_cli(args, ap) -> None:
    if not args.out:
        args.out = str(_paths().stickers / "generated" / (uuid.uuid4().hex + ".jpg"))

    if args.add_sticker:
        res = add_sticker_to_index(args.add_sticker, args.text or "", args.category or "panda", args.tags)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.list:
        print(_paths().index.read_text(encoding="utf-8") if _paths().index.exists() else "{}")
        return

    if args.recommend:
        print(json.dumps(recommend_sticker(args.recommend, args.context, args.previous_tag, args.seed), ensure_ascii=False, indent=2))
        return

    if args.search:
        res = search_candidates(args.search)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.pick:
        res = pick_sticker(args.pick, prefer_gif=args.prefer_gif)
        print(json.dumps(res, ensure_ascii=False))
        return

    if not args.text:
        ap.error("Either --text (to generate meme) or --pick (to pick ready sticker) or --list is required")

    p, used_tpl = make_meme(
        args.template,
        args.text,
        args.out,
        animate=args.animate,
        category=args.category,
        embed_credit_footer=args.embed_credit_footer,
    )
    print(json.dumps({
        "ok": True,
        "path": p,
        "template": used_tpl,
        "animated": args.animate or False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
