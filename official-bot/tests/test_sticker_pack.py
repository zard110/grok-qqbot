import json
import unittest
from pathlib import Path
from PIL import Image
from test_groups import A, B, GroupFixture
from core.sticker_pack import install, verified_assets
from core.sticker_policy import recommend, infer_mood
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

    def test_transparent_template_static_and_gif_default_to_sidecar_credit(self):
        install(self.directory)
        for animation, extension in ((None, ".png"), ("bounce", ".gif")):
            out = self.root / ("caption" + extension)
            S.make_meme("openmoji_laugh", "这波可以|给你鼓掌", str(out), animate=animation, group=A)
            with Image.open(out) as picture:
                self.assertEqual(picture.convert("RGB").getpixel((0, 0)), (255, 255, 255))
                self.assertEqual(picture.width, 300)
                if animation:
                    self.assertGreater(picture.n_frames, 1)
            metadata = json.loads(Path(str(out) + ".license.json").read_text(encoding="utf-8"))
            self.assertTrue(metadata["modified"])
            self.assertFalse(metadata["embed_credit_footer"])
            self.assertEqual(metadata["license"], "CC-BY-SA-4.0")

    def test_explicit_footer_and_reinstall_preserve_local_assets_and_labels(self):
        # Start from the previous release's pixel-footer layout.
        install(self.directory, embed_credit_footer=True)
        index = self.directory / "index.json"
        data = json.loads(index.read_text(encoding="utf-8"))
        first = data["ready_stickers"][0]
        first["text"], first["tags"] = "本群备注", ["本群标签"]
        old_file = self.directory / first["file"]
        native = {"file": "local.png", "category": "panda", "tags": ["哈哈"], "text": "本土"}
        data["ready_stickers"].append(native)
        index.write_text(json.dumps(data), encoding="utf-8")
        result = install(self.directory)
        self.assertEqual((result["added"], result["updated"]), (0, 20))
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(len(data["ready_stickers"]), 21)
        self.assertEqual(data["ready_stickers"][-1], native)
        first = data["ready_stickers"][0]
        self.assertEqual((first["text"], first["tags"]), ("本群备注", ["本群标签"]))
        with Image.open(self.directory / first["file"]) as picture, Image.open(old_file) as old:
            self.assertEqual(picture.size, (618, 618))
            self.assertEqual(old.size, (618, 666))
        sidecar = json.loads((self.directory / (first["file"] + ".license.json")).read_text(encoding="utf-8"))
        self.assertFalse(sidecar["embed_credit_footer"])
        self.assertTrue(sidecar["author"] and sidecar["source_url"] and sidecar["license_url"])
        self.assertEqual(install(self.directory)["updated"], 0)
        for animation, extension in ((None, ".png"), ("bounce", ".gif")):
            path = self.root / ("credited" + extension)
            S.make_meme("openmoji_laugh", "可以", str(path), animate=animation, embed_credit_footer=True, group=A)
            with Image.open(path) as picture:
                self.assertEqual(picture.width, 360)

    def test_legacy_moods_derive_from_caption_tags_and_category(self):
        cases = {"搞笑": "laugh", "哈哈": "laugh", "笑死": "laugh", "质疑": "doubt", "鄙视": "doubt",
                 "就这": "doubt", "二哈": "doubt", "吃瓜": "watch", "围观": "watch", "暗中观察": "watch",
                 "大佬": "approve", "抱拳": "approve", "佩服": "approve", "点赞": "approve",
                 "鼓掌": "clap", "牛逼": "clap", "震惊": "shock", "卧槽": "shock", "破防": "cry",
                 "裂开": "cry", "小丑": "cry", "哭": "cry", "汗": "sweat", "尴尬": "sweat",
                 "摸鱼": "sweat", "猫": "cat", "喵": "cat", "狗": "dog", "汪": "dog", "柴犬": "dog", "Cheems": "dog"}
        for word, mood in cases.items():
            for field in ("text", "category", "tags"):
                with self.subTest(word=word, field=field):
                    self.assertEqual(infer_mood({field: [word] if field == "tags" else word}), mood)
        self.assertEqual(infer_mood({"mood": "cool", "text": "哭"}), "cool")
        self.assertEqual(infer_mood({"category": "dog", "text": "鼓掌"}), "clap")
        self.assertEqual(infer_mood({"text": "education application"}), "")

    def test_native_priority_chain_and_no_irrelevant_native_boost(self):
        local = {"file": "panda.png", "category": "panda", "tags": ["鼓掌"], "text": "牛逼"}
        emoji = {"file": "emoji.png", "category": "openmoji", "tags": ["鼓掌"], "mood": "clap"}
        result = recommend([emoji, local], "鼓掌", previous="cool")
        self.assertEqual((result["file"], result["mood"]), ("panda.png", "clap"))
        self.assertEqual(recommend([local], "", previous="cool")["file"], "panda.png")
        self.assertFalse(recommend([local], "无关内容")["should_send"])
        self.assertFalse(recommend([local], "鼓掌", context="朋友住院了")["should_send"])

    def test_caption_never_silently_drops_extra_lines(self):
        for text in ("one|two|three", "长文" * 30):
            with self.assertRaises(ValueError):
                S.parse_lines(text)
