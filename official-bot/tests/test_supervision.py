import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

BOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT))
from core.supervisor import restart_delay
from core import supervisor

spec = importlib.util.spec_from_file_location("watchdog_test_module", BOT / "local-watchdog.py")
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class SupervisionTests(unittest.TestCase):
    def test_competing_supervisor_does_not_start_a_second_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, data = root / "code", root / "data"
            code.mkdir()
            (code / "official-ws-bridge.py").write_text("import time\ntime.sleep(60)\n")
            stop = threading.Event()
            outcomes = []
            with patch.object(supervisor, "DATA_DIR", data), patch.dict(os.environ, {
                "QQBOT_CODE_DIR": str(code), "QQBOT_DATA_DIR": str(data),
            }):
                worker = threading.Thread(target=lambda: outcomes.append(supervisor.run("bridge", stop)))
                worker.start()
                try:
                    deadline = time.monotonic() + 10
                    state = {}
                    while time.monotonic() < deadline:
                        try:
                            state = json.loads((data / "supervisor-bridge.json").read_text())
                        except (OSError, ValueError):
                            pass
                        if state.get("status") == "running":
                            break
                        time.sleep(0.05)
                    self.assertEqual(state.get("status"), "running")
                    competitor = subprocess.run(
                        [sys.executable, str(BOT / "core/supervisor.py"), "bridge"],
                        capture_output=True, timeout=10, env=os.environ.copy(),
                    )
                    self.assertEqual(competitor.returncode, 0, competitor.stderr)
                    unchanged = json.loads((data / "supervisor-bridge.json").read_text())
                    self.assertEqual(unchanged["child_pid"], state["child_pid"])
                finally:
                    stop.set()
                    worker.join(timeout=25)
                self.assertFalse(worker.is_alive())
                self.assertEqual(outcomes, [0])

    def test_restart_budget_is_bounded(self):
        self.assertEqual(restart_delay(1), 5)
        self.assertGreater(restart_delay(4), restart_delay(3))
        self.assertEqual(restart_delay(100), 300)

    def test_tail_read_is_bounded_and_retains_latest_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge.log"
            path.write_text("old\n" * 10000 + "latest\nready\n", encoding="utf-8")
            self.assertEqual(watchdog.tail_lines(path, 2, max_bytes=100), ["latest", "ready"])

    def test_missing_process_waits_for_outage_threshold(self):
        now = time.time()
        state = {}
        with patch.object(watchdog, "ensure_forwarder_files"), \
             patch.object(watchdog, "get_bridge_pids", return_value=[]), \
             patch.object(watchdog, "start_bridge") as start, \
             patch.object(watchdog, "wlog"), \
             patch.object(watchdog, "parse_bridge_log", return_value=(999999, [], [])), \
             patch.object(watchdog, "send_webhook_alert") as alert, \
             patch.object(watchdog.time, "sleep"), \
             patch.object(watchdog.time, "time", return_value=now):
            watchdog.check_once(state)
            start.assert_called_once()
            alert.assert_not_called()
            self.assertEqual(state["gateway_status"], "missing")
            state["unhealthy_since"] = now - watchdog.ALERT_FAIL_SEC - 1
            watchdog.check_once(state)
            alert.assert_called_once()

    def test_duplicate_pids_cleans_up_older_duplicates_and_keeps_newest(self):
        with patch.object(watchdog, "ensure_forwarder_files"), \
             patch.object(watchdog, "get_bridge_pids", return_value=[123, 456]), \
             patch.object(watchdog, "parse_bridge_log", return_value=(10.0, [], [])), \
             patch.object(watchdog, "wlog"), \
             patch.object(watchdog.os, "kill") as kill:
            state = {}
            watchdog.check_once(state)
            kill.assert_called_once_with(123, watchdog.signal.SIGTERM)
            self.assertEqual(state["bridge_pid"], 456)


if __name__ == "__main__":
    unittest.main()
