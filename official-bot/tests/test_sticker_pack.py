import json
import unittest
from pathlib import Path
from PIL import Image
from test_groups import A, B, GroupFixture
from core.sticker_pack import install, verified_assets
from core.sticker_policy import recommend
import groups as G
import make_sticker as S


class StickerPackTests(GroupFixture):
    def setUp(self):
        super().setUp()
        G.update_config(A, features={**G.load_config(A)["features"], "image_gen": True})
        self.directory = G.group_dir(A) / "stickers"

    def test_source_hashes_licenses_and_idempotent_install(self):
        self.assertEqual(len(verified_assets()), 20)
        result = install(self.directory)
        self.assertEqual(result["added"], 20)
        self.assertEqual(install(self.directory)["added"], 0)
        data = json.loads((self.directory / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["templates"]), 20)
        self.assertEqual(len({x["sha256"] for x in data["ready_stickers"]}), 20)
        self.assertTrue(all(x["license"] == "CC-BY-SA-4.0" and x["source_url"] and x["source_sha256"] for x in data["ready_stickers"]))
        self.assertFalse((G.group_dir(B) / "stickers").exists())

    def test_deterministic_context_and_serious_conversation(self):
        install(self.directory)
        first = S.recommend_sticker("笑死我了 哈哈", seed="event", group=A)
        self.assertTrue(first["should_send"])
        self.assertEqual(first, S.recommend_sticker("笑死我了 哈哈", seed="event", group=A))
        self.assertTrue(Path(first["path"]).is_file())
        self.assertFalse(S.recommend_sticker("哈哈", context="朋友住院了", group=A)["should_send"])
        self.assertFalse(S.recommend_sticker("irrelevant phrase", group=A)["should_send"])
        self.assertFalse((self.directory / ".recent_used.json").exists())

    def test_reply_chain_selects_complementary_reaction(self):
        items = [{"file": "laugh", "mood": "laugh", "tags": []}, {"file": "clap", "mood": "clap", "tags": []}]
        self.assertEqual(recommend(items, "", previous="cool")["file"], "clap")

    def test_transparent_template_static_and_gif_keep_credit_and_provenance(self):
        install(self.directory)
        for animation, extension in ((None, ".png"), ("bounce", ".gif")):
            out = self.root / ("caption" + extension)
            S.make_meme("openmoji_laugh", "这波可以|给你鼓掌", str(out), animate=animation, group=A)
            with Image.open(out) as picture:
                self.assertEqual(picture.convert("RGB").getpixel((0, 0)), (255, 255, 255))
                self.assertGreaterEqual(picture.width, 360)
                if animation:
                    self.assertGreater(picture.n_frames, 1)
            metadata = json.loads(Path(str(out) + ".license.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["modified"])
            self.assertEqual(metadata["license"], "CC-BY-SA-4.0")

    def test_caption_never_silently_drops_extra_lines(self):
        for text in ("one|two|three", "长文" * 30):
            with self.assertRaises(ValueError):
                S.parse_lines(text)
