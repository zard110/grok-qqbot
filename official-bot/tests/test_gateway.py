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
