import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from test_groups import A, B, GroupFixture
import groups as G
import make_sticker as S


class StickerTests(GroupFixture):
    def setUp(self):
        super().setUp()
        for group in (A, B):
            G.update_config(group, features={**G.load_config(group)["features"], "image_gen": True})

    def image(self, name, color):
        path = self.root / name
        Image.new("RGB", (20, 20), color).save(path)
        return path

    def test_asset_copy_and_concurrent_group_isolation(self):
        first = self.image("same.png", "red")
        a = S.add_sticker_to_index(str(first), "red fruit", tags_str="red", group=A)
        self.image("same.png", "blue")
        b = S.add_sticker_to_index(str(first), "blue ocean", tags_str="blue", group=B)
        self.assertNotEqual(a["entry"]["file"], b["entry"]["file"])
        first.unlink()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda g: S.pick_sticker("red" if g == A else "blue", group=g), [A, B] * 5))
        for group, result in zip([A, B] * 5, results):
            self.assertTrue(result["ok"])
            self.assertIn(group, result["path"])
        self.assertFalse(S.pick_sticker("unmatched", group=A)["ok"])

    def test_concurrent_ingestion_preserves_all_entries_and_deduplicates(self):
        paths = [self.image(f"image-{i}.png", (i * 10, 0, 0)) for i in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda p: S.add_sticker_to_index(str(p), p.stem, group=A), paths))
        index = G.group_dir(A) / "stickers" / "index.json"
        self.assertEqual(len(json.loads(index.read_text())["ready_stickers"]), 12)
        result = S.add_sticker_to_index(str(paths[0]), "same bytes", group=A)
        self.assertEqual(result["status"], "duplicate_sha256_skipped")
        self.assertEqual(len(json.loads(index.read_text())["ready_stickers"]), 12)

    def test_explicit_template_is_stable_and_recent_is_scoped(self):
        for group in (A, B):
            folder = G.group_dir(group) / "stickers" / "templates"
            folder.mkdir(parents=True)
            Image.new("RGB", (30, 30), "red").save(folder / "custom.png")
        with S.use_group(A):
            S.save_recent({"recent_templates": ["custom"]})
        self.assertEqual(S.choose_template("custom", group=A), "custom")
        self.assertEqual(S.load_recent(group=B)["recent_templates"], [])

    def test_disabled_group_and_malformed_image_are_rejected(self):
        path = self.root / "fake.png"
        path.write_text("not an image")
        with self.assertRaises(Exception):
            S.add_sticker_to_index(str(path), "bad", group=A)
        G.update_config(A, enabled=False)
        with self.assertRaises(PermissionError):
            S.search_candidates("test", group=A)

    def test_legacy_cli_without_group_or_environment_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            library = root / "groups" / "zwift" / "stickers"
            library.mkdir(parents=True)
            Image.new("RGB", (20, 20), "red").save(library / "red.png")
            (library / "index.json").write_text(json.dumps({"ready_stickers": [
                {"file": "red.png", "text": "red", "tags": ["red"]}]}), encoding="utf-8")
            env = {**os.environ, "QQBOT_DATA_DIR": str(root)}
            env.pop("QQBOT_LEGACY_ZWIFT", None)
            script = str(Path(__file__).resolve().parents[1] / "make_sticker.py")
            result = subprocess.run([sys.executable, script, "--pick", "red"], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["ok"])
            explicit = subprocess.run([sys.executable, script, "--group", A, "--pick", "red"], env=env,
                                      capture_output=True, text=True, timeout=10)
            self.assertNotEqual(explicit.returncode, 0)


if __name__ == "__main__":
    unittest.main()
