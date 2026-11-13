import importlib.util
import io
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
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


class _Raw:
    def __init__(self, sock=None):
        self.sock = sock
        self.shutdowns = []
        self.closes = 0

    def fileno(self):
        return self.sock.fileno()

    def shutdown(self, how):
        self.shutdowns.append(how)
        if self.sock is not None:
            return self.sock.shutdown(how)

    def close(self):
        self.closes += 1
        if self.sock is not None:
            self.sock.close()

    def send(self, data, flags=0):
        return self.sock.send(data, flags)


class _Web:
    """Stand-in for websocket.WebSocket. The original close() writes a frame."""

    def __init__(self, raw):
        self.sock = raw
        self.connected = True
        self.close_frames = 0

    def close(self, *_args, **_kwargs):
        if not self.connected:
            return
        self.connected = False
        self.close_frames += 1
        payload = struct_close_frame()
        self.sock.send(payload)


class _App:
    def __init__(self, web):
        self.sock = web
        self.keep_running = True
        self._bridge_generation = 1
        self.sent = []

    def send(self, data, opcode=1):
        self.sent.append(data)
        if self.sock and self.sock.sock is not None:
            self.sock.sock.send(data)


def struct_close_frame():
    # Masked 2-byte WebSocket close body. Production corruption of 32 bytes was
    # this frame wrapped in one TLS record.
    return b"\x88\x82" + b"\x00\x00\x00\x00" + b"\x03\xe8"


def _bridge():
    bridge = gateway.Bridge.__new__(gateway.Bridge)
    bridge._send_lock = threading.RLock()
    bridge._generation = 1
    bridge._dead = False
    bridge.seq = 4
    bridge.heartbeat_ms = 1000
    bridge.last_hb_sent = 0
    return bridge


def _tls_pair():
    directory = tempfile.mkdtemp()
    key = os.path.join(directory, "key.pem")
    cert = os.path.join(directory, "cert.pem")
    subprocess.check_call(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-keyout", key, "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.check_hostname = False
    client_ctx.verify_mode = ssl.CERT_NONE
    left, right = socket.socketpair()
    left.settimeout(2)
    right.settimeout(2)

    def serve():
        wrapped = None
        try:
            wrapped = server_ctx.wrap_socket(left, server_side=True)
            wrapped.settimeout(2)
            try:
                wrapped.recv(8)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            try:
                (wrapped or left).close()
            except OSError:
                pass

    threading.Thread(target=serve, daemon=True).start()
    client = client_ctx.wrap_socket(right, server_hostname="localhost")
    client.settimeout(2)
    return client


@unittest.skipUnless(shutil.which("openssl"), "openssl required to reproduce the TLS fd recycle")
class TlsFdReuseTests(unittest.TestCase):
    def test_os_close_lets_a_later_tls_write_replace_a_reused_file(self):
        client = _tls_pair()
        fd = client.fileno()
        os.close(fd)
        self.assertEqual(client.fileno(), fd)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 128)
            got = os.open(path, os.O_RDWR)
            try:
                if got != fd:
                    os.dup2(got, fd)
                    os.close(got)
                    got = None
                client.send(b'{"op": 1, "d": 50}')
                self.assertEqual(path.read_bytes()[:3], b"\x17\x03\x03")
            finally:
                if got is not None:
                    os.close(got)
                try:
                    client.close()
                except OSError:
                    pass

    def test_kill_socket_keeps_the_tls_fd_and_does_not_stamp_a_new_file(self):
        client = _tls_pair()
        fd = client.fileno()
        raw = _Raw(client)
        web = _Web(raw)
        app = _App(web)
        bridge = _bridge()
        bridge._kill_socket(app, "no heartbeat ACK")
        self.assertEqual(raw.closes, 0)
        self.assertEqual(raw.shutdowns, [socket.SHUT_RDWR])
        self.assertFalse(web.connected)
        self.assertFalse(app.keep_running)
        self.assertTrue(bridge._dead)
        self.assertEqual(client.fileno(), fd)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.sqlite3"
            path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 128)
            got = os.open(path, os.O_RDWR)
            try:
                self.assertNotEqual(got, fd)
                with self.assertRaises(OSError):
                    client.send(b'{"op": 1, "d": 50}')
                self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
                self.assertEqual(web.close_frames, 0)
                # Reader thread teardown. This must close, and must not emit a close frame.
                app.sock.close()
                self.assertEqual(raw.closes, 1)
                self.assertEqual(web.close_frames, 0)
                self.assertTrue(path.read_bytes().startswith(b"SQLite format 3\x00"))
            finally:
                os.close(got)


class SocketKillTests(unittest.TestCase):
    def test_kill_does_not_close_or_send_and_owner_close_is_silent(self):
        bridge = _bridge()
        left, right = socket.socketpair()
        right.setblocking(False)
        raw = _Raw(left)
        web = _Web(raw)
        app = _App(web)
        bridge._kill_socket(app, "SIGUSR1")
        self.assertEqual(raw.closes, 0)
        self.assertEqual(web.close_frames, 0)
        with self.assertRaises(ConnectionError):
            bridge._send(app, {"op": 1, "d": 4})
        self.assertEqual(app.sent, [])
        app.sock.close()
        self.assertEqual(raw.closes, 1)
        self.assertEqual(web.close_frames, 0)
        try:
            peer = right.recv(64)
        except BlockingIOError:
            peer = b""
        # EOF is acceptable. A WebSocket close frame would be non-empty.
        self.assertEqual(peer, b"")
        right.close()

    def test_kill_waits_until_an_inflight_send_finishes(self):
        bridge = _bridge()
        raw = _Raw()
        web = _Web(raw)
        app = _App(web)
        release = threading.Event()
        order = []

        def send(data, opcode=1):
            order.append("send")
            self.assertTrue(release.wait(2))
            order.append("sent")

        app.send = send

        def shutdown(how):
            order.append("shutdown")
            raw.shutdowns.append(how)

        raw.shutdown = shutdown
        worker = threading.Thread(target=lambda: bridge._send(app, {"op": 1, "d": 9}))
        worker.start()
        deadline = time.monotonic() + 2
        while "send" not in order and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("send", order)
        killer = threading.Thread(target=lambda: bridge._kill_socket(app, "watchdog"))
        killer.start()
        time.sleep(0.05)
        self.assertEqual(raw.closes, 0)
        self.assertNotIn("shutdown", order)
        release.set()
        worker.join(2)
        killer.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(killer.is_alive())
        self.assertEqual(order, ["send", "sent", "shutdown"])
        self.assertEqual(raw.closes, 0)
        app.sock.close()
        self.assertEqual(raw.closes, 1)

    def test_stale_generation_kill_does_not_poison_the_new_connection(self):
        bridge = _bridge()
        bridge._generation = 2
        app = _App(_Web(_Raw()))
        app._bridge_generation = 1
        bridge._kill_socket(app, "previous connection")
        self.assertFalse(bridge._dead)
        self.assertFalse(app.keep_running)
        self.assertEqual(app.sock.sock.closes, 0)

    def test_heartbeat_skips_send_after_a_long_monotonic_gap(self):
        bridge = _bridge()
        bridge.seq = 46
        bridge._send = Mock()
        stop = threading.Event()
        clock = {"now": 50.0}

        def monotonic():
            return clock["now"]

        def wait(timeout):
            if clock["now"] == 50.0:
                clock["now"] = 50.0 + timeout + gateway.Bridge.STALE_HEARTBEAT_SLACK + 5
                return False
            stop.set()
            return True

        stop.wait = wait
        with patch.object(gateway.time, "monotonic", monotonic):
            bridge._heartbeat_loop(Mock(), stop)
        bridge._send.assert_not_called()

    def test_heartbeat_sends_when_the_wait_matches_the_interval(self):
        bridge = _bridge()
        bridge.seq = 8
        bridge._send = Mock()
        stop = threading.Event()
        clock = {"now": 50.0}
        ws = Mock()

        def monotonic():
            return clock["now"]

        def wait(timeout):
            if clock["now"] == 50.0:
                clock["now"] = 50.0 + timeout
                return False
            stop.set()
            return True

        stop.wait = wait
        with patch.object(gateway.time, "monotonic", monotonic):
            bridge._heartbeat_loop(ws, stop)
        bridge._send.assert_called_once_with(ws, {"op": 1, "d": 8})

    def test_health_publication_exits_when_sqlite_header_is_tls(self):
        bridge = gateway.Bridge.__new__(gateway.Bridge)
        bridge.connected = False
        bridge.last_ack = 0
        bridge.last_event_at = 0
        bridge.last_dispatch_at = 0
        bridge.q = Mock(last_error=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "runtime.sqlite3"
            database.write_bytes(b"\x17\x03\x03" + b"\x00" * 48)
            bridge.store = Mock(path=database)
            with patch.object(gateway, "DIR", root), patch("core.delivery.os._exit") as stop:
                bridge.write_health()
        stop.assert_called_once_with(4)
