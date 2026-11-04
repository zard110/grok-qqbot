import importlib.util
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("gateway_under_test", Path(__file__).resolve().parents[1] / "official-ws-bridge.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class GatewayTests(unittest.TestCase):
    def test_non_admin_memory_command_is_delegated_with_errors(self):
        class Response(io.BytesIO):
            status = 202
        opener = Mock()
        opener.open.return_value = Response(b"{}")
        with tempfile.TemporaryDirectory() as directory, patch.object(gateway, "DIR", Path(directory)), \
             patch.object(gateway.G, "group_dir", return_value=Path(directory) / "group"), \
             patch("core.plugins.dispatch", return_value={"actions": [], "errors": ["command:PermissionError"]}), \
             patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/test" if str(path).endswith("url") else "fixture-key"), \
             patch.object(gateway.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(gateway.forward({"post_type": "message", "group_openid": "A" * 32,
                "text_only": "/记忆 我要记住", "delivery_id": "command", "at_me": False,
                "features": {"natural_reply": False}}), "accepted")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload["plugin_errors"], ["command:PermissionError"])
        self.assertFalse(payload["local_actions_completed"])
        self.assertEqual(payload["reply_context"]["trigger"], "command")

    def test_natural_burst_gates_webhook_but_at_bypasses_it(self):
        from core.delivery import DeliveryStore
        class Response(io.BytesIO):
            status = 202
        opener = Mock()
        opener.open.side_effect = lambda *a, **kw: Response(b"{}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DeliveryStore(root / "runtime.sqlite3")
            group = "A" * 32
            action = {"type": "routine.reply", "group_openid": group, "plugin_id": "natural_reply", "config_revision": 1}
            with patch.object(gateway, "DIR", root), patch.object(gateway, "DeliveryStore", return_value=store), \
                 patch.object(gateway.G, "group_dir", return_value=root / "group"), \
                 patch.object(gateway.G, "load_config", return_value={"revision": 1}), \
                 patch.object(gateway.G, "plugin_enabled", return_value=True), \
                 patch("core.plugins.dispatch", return_value={"actions": [action], "errors": []}), \
                 patch("core.action_runner.run_actions", return_value=[]), \
                 patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/test" if str(path).endswith("url") else "fixture-key"), \
                 patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                event = {"post_type": "message", "group_openid": group, "text_only": "骑行训练",
                         "msg_id": "one", "features": {"natural_reply": True, "at_reply": True}}
                self.assertEqual(gateway.forward(dict(event)), "accepted")
                self.assertEqual(gateway.forward({**event, "msg_id": "two"}), "skipped")
                self.assertEqual(gateway.forward({**event, "msg_id": "three", "at_me": True}), "accepted")
            self.assertEqual(opener.open.call_count, 2)

    def test_persistence_failure_does_not_advance_resume_cursor(self):
        bridge = gateway.Bridge.__new__(gateway.Bridge)
        bridge.seq = 5
        bridge.q = Mock()
        bridge.q.put.side_effect = OSError("disk full")
        bridge._kill_socket = Mock()
        with self.assertRaises(OSError):
            bridge.on_message(Mock(), '{"op":0,"t":"GROUP_MESSAGE_CREATE","s":6,"d":{}}')
        self.assertEqual(bridge.seq, 5)
        bridge._kill_socket.assert_called_once()

    def test_interaction_ack_is_submitted_without_dispatch_wait(self):
        bridge = gateway.Bridge.__new__(gateway.Bridge)
        bridge.seq, bridge.q = 5, Mock()
        bridge.ack_slots = threading.BoundedSemaphore(1)
        bridge.ack_pool, bridge.write_health = Mock(), Mock()
        bridge.on_message(Mock(), '{"op":0,"t":"INTERACTION_CREATE","s":6,"d":{"id":"interaction"}}')
        bridge.q.put.assert_called_once()
        bridge.ack_pool.submit.assert_called_once_with(bridge._ack_interaction, "interaction")
        self.assertEqual(bridge.seq, 6)

    def test_reference_cache_is_scoped(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(gateway, "REF_CACHE_FILE", Path(directory) / "refs.json"), patch.object(gateway, "_ref_cache", {}):
            gateway.ref_cache_put("same", "Alice", "A secret", "A")
            gateway.ref_cache_put("same", "Bob", "B secret", "B")
            self.assertEqual(gateway.ref_lookup("same", "A")["text"], "A secret")
            self.assertEqual(gateway.ref_lookup("same", "B")["text"], "B secret")

    def test_coffee_step_is_answered_locally_and_not_forwarded(self):
        group = "A" * 32
        action = {"type": "coffee.step", "step": "start", "group_openid": group,
                  "plugin_id": "luckin_coffee", "config_revision": 1}
        opener = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"post_type": "message", "group_openid": group, "member_openid": "member",
                       "text_only": "想喝咖啡", "msg_id": "incoming", "delivery_id": "coffee",
                       "at_me": True, "features": {"at_reply": True}}
            with patch.object(gateway, "DIR", root), \
                 patch.object(gateway.G, "group_dir", return_value=root / group), \
                 patch("core.plugins.dispatch", return_value={"handled": True, "actions": [action], "errors": []}), \
                 patch("core.action_runner.run_actions", return_value=[{"type": "coffee.step", "result": {"status": "started"}}]), \
                 patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/webhook-test" if str(path).endswith("url") else "fixture-key"), \
                 patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                self.assertEqual(gateway.forward(payload), "accepted")
        opener.open.assert_not_called()

    def test_plugin_memory_action_runs_before_webhook_and_context_is_bounded(self):
        from core import action_runner
        from core.memory import MemoryStore
        group = "A" * 32
        cfg = {"enabled": True, "revision": 1}
        action = {"type": "memory.add", "text": "meet at noon", "group_openid": group,
                  "plugin_id": "command", "config_revision": 1}
        class Response(io.BytesIO):
            status = 202
        opener = Mock()
        opener.open.return_value = Response(b"{}")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"post_type": "message", "group_openid": group, "member_openid": "admin",
                       "text_only": "/记忆 meet at noon", "msg_id": "incoming", "delivery_id": "stable",
                       "at_me": True, "features": {"at_reply": True}}
            with patch.object(gateway, "DIR", root), patch.object(action_runner, "DATA_DIR", root), \
                 patch.object(gateway.G, "group_dir", return_value=root / group), \
                 patch.object(gateway.G, "load_config", return_value=cfg), \
                 patch.object(gateway.G, "plugin_enabled", return_value=True), \
                 patch.object(gateway.G, "authorize") as authorization, \
                 patch("core.plugins.dispatch", return_value={"handled": True, "actions": [action], "errors": []}), \
                 patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/webhook-test" if str(path).endswith("url") else "fixture-key"), \
                 patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                self.assertEqual(gateway.forward(payload), "accepted")
            request = opener.open.call_args.args[0]
            sent = json.loads(request.data)
            self.assertTrue(sent["local_actions_completed"])
            self.assertEqual(sent["plugin_results"][0]["type"], "memory.add")
            self.assertEqual(sent["context"]["scope"], group)
            self.assertEqual(MemoryStore(root).search_memories(group)[0]["value"], "meet at noon")
            authorization.assert_called_once_with(group, "command", "memory.write", "admin")

    def test_rejected_plugin_cannot_fall_through_to_cloud(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(gateway, "DIR", Path(directory)), \
             patch("core.plugins.dispatch", return_value={"handled": True, "actions": [], "errors": ["denied"]}), \
             patch.object(gateway.urllib.request, "build_opener") as network:
            outcome = gateway.forward({"post_type": "message", "group_openid": "A" * 32,
                "text_only": "/记忆 secret", "delivery_id": "reject", "at_me": True})
            self.assertEqual(outcome, "skipped")
            network.assert_not_called()
