"""Offline configuration/concurrency tests; all state lives in temporary directories."""
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import groups as G
from core.files import confined_path, file_lock

A, B = "A" * 32, "B" * 32


class GroupFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in {"DIR": self.root, "GROUPS": self.root / "groups",
                            "DEFAULT_DIR": self.root / "groups" / "_default",
                            "C2C_DIR": self.root / "c2c", "SYSTEM_DIR": self.root / "_system"}.items():
            context = patch.object(G, name, value)
            context.start()
            self.addCleanup(context.stop)
        G._LAST_GOOD.clear()
        G.ensure_group(A, "First")
        G.ensure_group(B, "Second")


class GroupTests(GroupFixture):
    def test_corrupt_policy_is_disabled_and_keeps_last_good_metadata(self):
        G.update_config(A, enabled=False, style="known good")
        (G.group_dir(A) / "config.json").write_text("{", encoding="utf-8")
        cfg = G.load_config(A)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["style"], "known good")
        self.assertIn("config_error", cfg)
        with self.assertRaises(G.ConfigError):
            G.update_config(A, enabled=True)

    def test_bad_boolean_is_not_truthy_permission(self):
        with self.assertRaises(G.ConfigError):
            G.update_config(A, enabled="false")
        with self.assertRaises(G.ConfigError):
            G.update_config(A, features={"image_gen": "false"})

    def test_missing_existing_config_does_not_recreate_an_enabled_group(self):
        (G.group_dir(A) / "config.json").unlink()
        cfg, created = G.ensure_group(A)
        self.assertFalse(created)
        self.assertFalse(cfg["enabled"])
        self.assertFalse((G.group_dir(A) / "config.json").exists())

    def test_concurrent_compare_and_swap_allows_one_writer(self):
        revision = G.load_config(A)["revision"]
        def save(index):
            try:
                G.update_config(A, expected_revision=revision, style=f"edit-{index}")
                return True
            except G.ConfigConflict:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(save, range(12))), 1)
        self.assertEqual(G.load_config(A)["revision"], revision + 1)

    def test_alias_and_registry(self):
        G.update_config(A, aliases=["one"])
        self.assertEqual(G.resolve("one"), A)
        with self.assertRaises(G.ConfigConflict):
            G.update_config(B, aliases=["ONE"])
        registry = json.loads((G.GROUPS / "registry.json").read_text())
        self.assertEqual(len(registry["groups"]), 2)

    def test_plugin_switch_and_admin_permission(self):
        self.assertTrue(G.plugin_enabled(A, "command"))
        with self.assertRaises(PermissionError):
            G.authorize(A, "command", "memory.write", "user")
        G.update_config(A, admins=["user"])
        G.authorize(A, "command", "memory.write", "user")
        G.set_plugin_enabled(A, "command", False)
        self.assertFalse(G.plugin_enabled(A, "command"))
        G.set_plugin_enabled(A, "sticker", True)
        self.assertTrue(G.plugin_enabled(A, "sticker"))

    def test_path_boundaries(self):
        with self.assertRaises(ValueError):
            G.group_dir("../outside")
        with self.assertRaises(ValueError):
            confined_path(self.root / "group", "../group-evil/file")
        with self.assertRaises(ValueError):
            confined_path(self.root, self.root / "absolute")

    def test_lock_timeout_and_reentrancy(self):
        path = self.root / "test.lock"
        with file_lock(path, timeout=0):
            with file_lock(path, timeout=0):
                pass
            def competing():
                with file_lock(path, timeout=0):
                    pass
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(TimeoutError):
                    pool.submit(competing).result()

    def test_lock_blocks_a_separate_process(self):
        path = self.root / "process.lock"
        script = """import sys
sys.path.insert(0, sys.argv[1])
from core.files import file_lock
try:
    with file_lock(sys.argv[2], timeout=0):
        sys.exit(3)
except TimeoutError:
    sys.exit(0)
"""
        with file_lock(path):
            result = subprocess.run([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(path)],
                                    capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
