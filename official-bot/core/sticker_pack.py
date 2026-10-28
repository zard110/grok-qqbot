"""Offline, hash-verified installation of a reviewed, attributed reaction pack."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from .files import atomic_write_bytes, atomic_write_json, atomic_write_text, confined_path, file_lock

PACK = Path(__file__).resolve().parents[1] / "assets" / "openmoji-reactions"


def attribution_frame(image: Image.Image, record: dict) -> Image.Image:
    """Keep a legible credit with shared images, including edited/animated output."""
    width = max(360, image.width)
    canvas = Image.new("RGB", (width, image.height + 48), "white")
    rgba = image.convert("RGBA")
    canvas.paste(rgba, ((width - image.width) // 2, 0), rgba)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=11)
    draw.text((7, image.height + 4), "OpenMoji contributors | CC BY-SA 4.0 | " + record.get("codepoint", ""), fill="black", font=font)
    draw.text((7, image.height + 18), "openmoji.org | creativecommons.org/licenses/by-sa/4.0", fill="black", font=font)
    draw.text((7, image.height + 32), "Edited: caption / layout / credit; see source ledger", fill="black", font=font)
    return canvas


def verified_assets(pack: Path = PACK):
    manifest = json.loads((pack / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("license") != "CC-BY-SA-4.0":
        raise ValueError("Unrecognized sticker pack or license")
    assets, seen = [], set()
    for row in manifest["assets"]:
        content = confined_path(pack, row["file"]).read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest != row["sha256"] or digest in seen or row["license"] != manifest["license"]:
            raise ValueError("Sticker pack checksum, duplication or license mismatch")
        with Image.open(io.BytesIO(content)) as image:
            if image.format != "PNG" or image.width * image.height > 20_000_000:
                raise ValueError("Invalid pack image")
            image.verify()
        seen.add(digest)
        assets.append((row, content))
    return assets


def install(destination: Path, pack: Path = PACK) -> dict:
    assets = verified_assets(pack)  # Validate the entire pack before publishing files.
    license_text = (pack / "LICENSE.txt").read_text(encoding="utf-8")
    with file_lock(destination / ".library.lock"):
        path = destination / "index.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        ready = data.setdefault("ready_stickers", [])
        templates = data.setdefault("templates", {})
        licenses = data.setdefault("template_licenses", {})
        added = 0
        for record, content in assets:
            template = record["template"]
            atomic_write_bytes(confined_path(destination, "templates/" + template + ".png"), content)
            templates.setdefault(template, record["description"] + "；" + "、".join(record["tags"]))
            licenses[template] = record
            # Ready-to-send copy embeds attribution so the credit travels with it.
            buffer = io.BytesIO()
            with Image.open(io.BytesIO(content)) as picture:
                attribution_frame(picture, record).save(buffer, format="PNG")
            shared = buffer.getvalue()
            digest = hashlib.sha256(shared).hexdigest()
            filename = digest + ".png"
            atomic_write_bytes(confined_path(destination, filename), shared)
            existing = next((x for x in ready if x.get("sha256") == digest), None)
            fields = {**record, "file": filename, "sha256": digest, "source_sha256": record["sha256"],
                      "text": record["description"], "category": "openmoji", "is_gif": False,
                      "source": "openmoji-reactions", "modified": True, "changes": "White background and visible attribution footer"}
            if existing is None:
                ready.append(fields)
                added += 1
            else:
                existing.update({key: value for key, value in fields.items() if key not in {"text", "tags"}})
        data["summary"] = {"total_ready_stickers": len(ready), "template_count": len(templates)}
        atomic_write_json(path, data)
        atomic_write_text(destination / "OPENMOJI-LICENSE.txt", license_text)
        atomic_write_json(destination / "openmoji-sources.json", [row for row, _ in assets])
    return {"ok": True, "added": added, "pack_assets": len(assets), "templates": len(templates), "directory": str(destination)}
