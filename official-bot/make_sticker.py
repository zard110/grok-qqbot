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
import json
import math
import random
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

DIR = Path(__file__).resolve().parent
STICKER_DIR = DIR / "groups" / "zwift" / "stickers"
TPL_DIR = STICKER_DIR / "templates"
INDEX_PATH = STICKER_DIR / "index.json"
RECENT_PATH = STICKER_DIR / ".recent_used.json"

FONT_PATHS = [
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


def load_recent() -> dict:
    if RECENT_PATH.exists():
        try:
            data = json.loads(RECENT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "recent_stickers": list(data.get("recent_stickers", []))[-25:],
                    "recent_templates": list(data.get("recent_templates", []))[-25:],
                }
        except Exception:
            pass
    return {"recent_stickers": [], "recent_templates": []}


def save_recent(data: dict) -> None:
    try:
        RECENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        trimmed = {
            "recent_stickers": list(data.get("recent_stickers", []))[-25:],
            "recent_templates": list(data.get("recent_templates", []))[-25:],
        }
        RECENT_PATH.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def choose_template(template_arg: str, category: str | None = None) -> str:
    available = sorted(p.stem for p in TPL_DIR.glob("*.png"))
    if not available:
        return "smirk"

    recent_data = load_recent()
    recent_tpls = recent_data["recent_templates"]

    if template_arg and template_arg != "random":
        chosen = template_arg if template_arg in available else "smirk"
        if chosen in recent_tpls[-15:]:
            # Avoid repeating the same template even when specified by habit!
            fresh_all = [t for t in available if t not in recent_tpls[-15:]]
            if fresh_all:
                chosen = random.choice(fresh_all)
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

    if chosen in recent_tpls:
        recent_tpls.remove(chosen)
    recent_tpls.append(chosen)
    recent_data["recent_templates"] = recent_tpls[-25:]
    save_recent(recent_data)
    return chosen


def parse_lines(text: str) -> list[str]:
    lines = [
        ln.strip()
        for ln in text.replace("\\n", "\n").replace("|", "\n").splitlines()
        if ln.strip()
    ][:2]
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


def make_meme(
    template: str,
    text: str,
    out_path: str,
    animate: str | None = None,
    category: str | None = None,
) -> tuple[str, str]:
    chosen_tpl = choose_template(template, category=category)
    tpl_file = TPL_DIR / f"{chosen_tpl}.png"
    if not tpl_file.exists():
        tpl_file = TPL_DIR / "smirk.png"
    head = Image.open(tpl_file).convert("RGB")
    lines = parse_lines(text)
    W = 300

    base_canvas, nw, nh, H = render_static_frame(head, lines, W=W)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not animate:
        base_canvas.save(out, quality=92)
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

        # Quantize frame for clean compact GIF
        frames.append(frame.quantize(colors=128, method=Image.Quantize.MEDIANCUT))

    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=55,
        loop=0,
        optimize=True,
    )
    return str(out), chosen_tpl


def pick_sticker(query: str, prefer_gif: bool = False) -> dict:
    if not INDEX_PATH.exists():
        return {"ok": False, "error": "index.json not found"}
    idx_data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
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
        full_path = STICKER_DIR / rel_file
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
            # Fallback to any non-recent sticker
            pool = [x[2] for x in scored if x[1]] or [x[2] for x in scored]
            if want_gif:
                gif_pool = [x for x in pool if x.get("is_gif")]
                if gif_pool:
                    pool = gif_pool
            chosen = random.choice(pool)

    rel_file = chosen["file"]
    recent_list = recent_data["recent_stickers"]
    if rel_file in recent_list:
        recent_list.remove(rel_file)
    recent_list.append(rel_file)
    recent_data["recent_stickers"] = recent_list[-25:]
    save_recent(recent_data)

    return {
        "ok": True,
        "path": str(STICKER_DIR / rel_file),
        "file": rel_file,
        "text": chosen.get("text", ""),
        "category": chosen.get("category", ""),
        "tags": chosen.get("tags", []),
        "is_gif": bool(chosen.get("is_gif", False)),
    }



def search_candidates(query: str, top_k: int = 8) -> dict:
    """Search across ALL 142+ ready stickers AND 25 templates, returning the top candidates with their text/description and whether they were recently used."""
    if not INDEX_PATH.exists():
        return {"ok": False, "error": "index.json not found"}
    idx_data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    stickers = idx_data.get("ready_stickers", [])
    templates = idx_data.get("templates", {})
    recent_data = load_recent()
    recent_files = set(recent_data["recent_stickers"][-20:])
    recent_tpls = set(recent_data["recent_templates"][-15:])

    tokens = [t.strip().lower() for t in re.split(r"[\s,|/，、]+", query) if t.strip()]

    sticker_hits = []
    for item in stickers:
        rel_file = item.get("file", "")
        full_path = STICKER_DIR / rel_file
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



def add_sticker_to_index(file_arg: str, text: str, category: str = "panda", tags_str: str = "") -> dict:
    """Add a newly collected group sticker to index.json with filename + MD5 deduplication."""
    import hashlib
    cand = Path(file_arg)
    if not cand.is_absolute():
        cand = STICKER_DIR / cand.name
    if not cand.exists():
        cand = STICKER_DIR / Path(file_arg).name
    if not cand.exists():
        return {"ok": False, "error": f"Sticker file not found: {file_arg}"}

    rel_file = cand.name
    new_md5 = hashlib.md5(cand.read_bytes()).hexdigest()

    idx_data = json.loads(INDEX_PATH.read_text(encoding="utf-8")) if INDEX_PATH.exists() else {"summary": {}, "templates": {}, "ready_stickers": []}
    stickers = idx_data.get("ready_stickers", [])

    for item in stickers:
        existing_rel = item.get("file", "")
        if existing_rel.startswith("groups/"):
            existing_rel = Path(existing_rel).name
            item["file"] = existing_rel
        existing_p = STICKER_DIR / existing_rel
        if existing_rel == rel_file:
            if text:
                item["text"] = text
            if category:
                item["category"] = category
            if tags_str:
                item["tags"] = [t.strip() for t in re.split(r"[\s,|/，、]+", tags_str) if t.strip()]
            INDEX_PATH.write_text(json.dumps(idx_data, ensure_ascii=False, indent=1), encoding="utf-8")
            return {"ok": True, "status": "updated_existing", "file": rel_file, "total": len(stickers)}
        if existing_p.exists():
            try:
                if hashlib.md5(existing_p.read_bytes()).hexdigest() == new_md5:
                    return {"ok": True, "status": "duplicate_md5_skipped", "existing_file": existing_rel, "total": len(stickers)}
            except Exception:
                pass

    tags = [t.strip() for t in re.split(r"[\s,|/，、]+", tags_str) if t.strip()] if tags_str else ["群友存图", "斗图"]
    entry = {
        "file": rel_file,
        "text": text or rel_file,
        "category": category or "panda",
        "tags": tags,
        "is_gif": cand.suffix.lower() == ".gif",
        "source": "group_collected",
    }
    stickers.append(entry)
    idx_data["ready_stickers"] = stickers
    summary = idx_data.setdefault("summary", {})
    summary["total_ready_stickers"] = len(stickers)
    summary["group_collected"] = sum(1 for x in stickers if not str(x.get("file", "")).startswith("library/"))
    summary["gif_count"] = sum(1 for x in stickers if x.get("is_gif"))
    INDEX_PATH.write_text(json.dumps(idx_data, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "status": "added", "entry": entry, "total": len(stickers)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate or pick custom斗图表情包")
    ap.add_argument("--template", default="random", help="Template name or 'random' (25 templates available)")
    ap.add_argument("--category", default=None, help="Optional category filter for --template random: dog | cat | cartoon | panda")
    ap.add_argument("--text", help="1-2 lines of custom caption separated by | or newline")
    ap.add_argument("--animate", choices=["shake", "zoom", "bounce"], default=None, help="Generate animated GIF sticker")
    ap.add_argument("--pick", default=None, help="Pick a ready sticker by keyword/emotion/character tag (avoids last 25 used)")
    ap.add_argument("--search", default=None, help="Search all 142+ ready stickers and 25 templates for the best contextual match")
    ap.add_argument("--prefer-gif", action="store_true", help="Prefer GIF when using --pick")
    ap.add_argument("--out", default="/workspace/napcat/media/custom_sticker.jpg", help="Output path for generated meme")
    ap.add_argument("--list", action="store_true", help="List all templates and ready stickers index")
    ap.add_argument("--add-sticker", default=None, help="Add a newly collected sticker file into index.json (with MD5 dedup)")
    ap.add_argument("--tags", default="", help="Comma/space separated tags when using --add-sticker")
    args = ap.parse_args()

    if args.add_sticker:
        res = add_sticker_to_index(args.add_sticker, args.text or "", args.category or "panda", args.tags)
        print(json.dumps(res, ensure_ascii=False))
        return

    if args.list:
        print(INDEX_PATH.read_text(encoding="utf-8") if INDEX_PATH.exists() else "{}")
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
    )
    print(json.dumps({
        "ok": True,
        "path": p,
        "template": used_tpl,
        "animated": args.animate or False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
