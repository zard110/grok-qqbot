import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import Mock, patch
from core.delivery import DeliveryStore, snapshot

spec = importlib.util.spec_from_file_location("sender_under_test", Path(__file__).resolve().parents[1] / "send_group.py")
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(sender, "DIR", self.root))
        self.stack.enter_context(patch.object(sender.G, "resolve", return_value="A" * 32))
        self.stack.enter_context(patch.object(sender.G, "group_dir", return_value=self.root / "group"))
        self.stack.enter_context(patch.object(sender.G, "load_config", return_value={"enabled": True, "features": {}}))
        self.store = DeliveryStore(self.root / "runtime.sqlite3")
        self.stack.enter_context(patch.object(sender, "DeliveryStore", return_value=self.store))
        self.api = self.stack.enter_context(patch.object(sender, "api", return_value=(200, {"id": "sent-1"})))
        self.token = self.stack.enter_context(patch.object(sender, "get_token", side_effect=AssertionError("offline test must not read credentials")))

    def run_cli(self, *args):
        output = io.StringIO()
        with patch("sys.argv", ["send_group.py", *args]), contextlib.redirect_stdout(output):
            try:
                sender.main()
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, json.loads(output.getvalue())

    def test_force_cannot_replay_action(self):
        self.assertEqual(self.run_cli("--text", "hello", "--msg-id", "incoming", "--force")[0], 0)
        code, result = self.run_cli("--text", "hello", "--msg-id", "incoming", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(result["duplicate"])
        self.assertEqual(self.api.call_count, 1)
        self.assertEqual(snapshot(self.store.path)["outbox"], {"sent": 1})

    def test_partial_send_is_recorded_and_not_replayed(self):
        image = self.root / "image.png"
        image.write_bytes(b"fixture")
        self.api.side_effect = [(200, {"id": "part-one"}), (500, {"error": "upstream"})]
        with patch.object(sender, "upload", return_value={"file_info": "fixture-media"}):
            code, result = self.run_cli("--image", str(image), "--markdown", "caption", "--msg-id", "incoming", "--force")
            self.assertEqual(code, 5)
            self.assertEqual(result["status"], "partial")
            self.run_cli("--image", str(image), "--markdown", "caption", "--msg-id", "incoming", "--force")
        self.assertEqual(self.api.call_count, 2)

    def test_every_dry_run_path_avoids_network(self):
        for args in (("--recall", "id"), ("--dry-credentials",), ("--upload-only", "--image", "missing.png")):
            code, result = self.run_cli(*args, "--dry-run")
            self.assertEqual(code, 0)
            self.assertTrue(result["dry_run"])
        self.api.assert_not_called()
        self.token.assert_not_called()

    def test_unknown_send_does_not_become_success(self):
        from core.qq_client import QQTransportError
        self.api.side_effect = QQTransportError("outcome unknown")
        code, result = self.run_cli("--text", "hi", "--action-id", "stable", "--force")
        self.assertEqual(code, 5)
        self.assertEqual(result["status"], "unknown")
        self.run_cli("--text", "hi", "--action-id", "stable", "--force")
        self.assertEqual(self.api.call_count, 1)
