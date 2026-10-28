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
        self.authorization = self.stack.enter_context(patch.object(sender.G, "authorize"))
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
        self.run_cli("--text", "hi", "--action-id", "stable")
        self.assertEqual(self.api.call_count, 1)
        # Production compatibility: an operator may explicitly retry uncertainty.
        code, result = self.run_cli("--text", "hi", "--action-id", "stable", "--force")
        self.assertEqual(code, 5)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(self.api.call_count, 2)

    def test_markdown_definite_rejection_falls_back_once(self):
        self.api.side_effect = [(400, {"message": "markdown not supported"}), (200, {"id": "plain"})]
        code, result = self.run_cli("--markdown", "## 结论", "--msg-id", "source")
        self.assertEqual(code, 0)
        payloads = [call.args[2].copy() for call in self.api.call_args_list]
        self.assertEqual([item["msg_type"] for item in payloads], [2, 0])
        self.assertEqual(payloads[0]["msg_seq"], payloads[1]["msg_seq"])
        self.assertIn("format_fallback", result["results"][0])

    def test_http_success_is_not_replayed_due_to_unobserved_client(self):
        self.assertEqual(self.run_cli("--markdown", "## hello")[0], 0)
        self.assertEqual(self.api.call_count, 1)

    def test_at_can_answer_during_natural_cooldown_but_same_source_cannot(self):
        self.assertEqual(self.run_cli("--text", "first", "--trigger", "natural", "--source-event-id", "first")[0], 0)
        self.assertEqual(self.run_cli("--text", "answer", "--trigger", "at", "--source-event-id", "second")[0], 0)
        code, result = self.run_cli("--text", "other answer", "--trigger", "at", "--source-event-id", "second")
        self.assertEqual(code, 3)
        self.assertEqual(result["reason"], "source_event_already_claimed")
        self.assertEqual(self.api.call_count, 2)

    def test_long_reply_parts_use_distinct_stable_sequences(self):
        args = ("--text", "abcdef" * 60, "--max-chars", "180", "--msg-id", "long", "--force")
        self.assertEqual(self.run_cli(*args)[0], 0)
        sequences = [call.args[2]["msg_seq"] for call in self.api.call_args_list]
        self.assertGreater(len(sequences), 1)
        self.assertEqual(len(sequences), len(set(sequences)))
        self.run_cli(*args)
        self.assertEqual(self.api.call_count, len(sequences))

    def test_trigger_cannot_bypass_disabled_plugin(self):
        self.authorization.side_effect = PermissionError("plugin off")
        with self.assertRaises(PermissionError):
            self.run_cli("--text", "hello", "--trigger", "natural")
        self.api.assert_not_called()

    def test_different_explicit_questions_can_receive_same_short_answer(self):
        for source in ("first", "second"):
            self.assertEqual(self.run_cli("--text", "可以", "--trigger", "at", "--source-event-id", source)[0], 0)
        self.assertEqual(self.api.call_count, 2)

    def test_recent_history_uses_bounded_suffix_and_tolerates_torn_line(self):
        from core.files import tail_jsonl
        path = self.root / "history.jsonl"
        path.write_bytes(b'x' * 2000 + b'\n{"content":"latest","ts":1}\n{"broken":')
        self.assertEqual(tail_jsonl(path, max_bytes=100), [{"content": "latest", "ts": 1}])

    def test_sticker_delay_is_short_and_only_for_natural_replies(self):
        picture = self.root / "sticker.png"
        picture.write_bytes(b"offline fixture")
        with patch.object(sender, "upload", return_value={"file_info": "fixture"}), patch.object(sender.time, "sleep") as delay:
            self.assertEqual(self.run_cli("--image", str(picture), "--sticker", "--trigger", "natural")[0], 0)
        self.assertEqual(delay.call_count, 1)
        self.assertGreaterEqual(delay.call_args.args[0], .7)
        self.assertLess(delay.call_args.args[0], 1.8)
