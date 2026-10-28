"""Verify the privacy scanner itself never echoes a detected secret."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
BASH = (Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if os.name == "nt" else Path(shutil.which("bash") or "/missing/bash"))


@unittest.skipUnless(BASH.is_file() and shutil.which("git"), "Git and Bash required")
class SecretScanTests(unittest.TestCase):
    def scan(self, root):
        return subprocess.run([str(BASH), str(ROOT / "tools/secret-scan.sh")], cwd=root,
                              capture_output=True, text=True, timeout=30)

    def prepare(self, root):
        subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True)

    def test_secret_is_detected_without_echoing_the_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            secret = uuid.uuid4().hex
            credential = root / "official-bot/appsecret"
            credential.parent.mkdir()
            credential.write_text(secret)
            (root / "example.txt").write_text("Authorization: " + "Bearer " + secret)
            subprocess.run(["git", "add", "example.txt"], cwd=root, check=True)
            result = self.scan(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("values hidden", result.stdout)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_runtime_database_is_rejected_and_plain_source_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            (root / "safe.py").write_text("print('hello')\n")
            subprocess.run(["git", "add", "safe.py"], cwd=root, check=True)
            self.assertEqual(self.scan(root).returncode, 0)
            (root / "memory.sqlite3").write_text("runtime data")
            subprocess.run(["git", "add", "memory.sqlite3"], cwd=root, check=True)
            result = self.scan(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("forbidden path", result.stdout)


if __name__ == "__main__":
    unittest.main()
