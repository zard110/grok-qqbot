import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.delivery import DeliveryStore, GroupDispatcher, event_identity, snapshot


def event(group, identity):
    return {"t": "GROUP_MESSAGE_CREATE", "id": identity, "d": {"group_openid": group, "content": "test"}}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "runtime.sqlite3")

    def test_durable_dedupe_and_crash_unknown(self):
        first = self.store.enqueue(event("A", "one"))
        self.assertEqual(first, self.store.enqueue(event("A", "one")))
        self.assertEqual(len(self.store.pending()), 1)
        self.assertTrue(self.store.claim(first))
        self.store.recover()
        self.assertEqual(snapshot(self.store.path)["inbox"], {"unknown": 1})
        self.assertEqual(self.store.pending(), [])

    def test_outbound_reservation_cannot_be_bypassed(self):
        self.assertTrue(self.store.reserve_action("one", "group:A")[0])
        self.store.action_status("one", "partial", {"parts": 1})
        fresh, previous = self.store.reserve_action("one", "group:A")
        self.assertFalse(fresh)
        self.assertEqual(previous["status"], "partial")
        with self.assertRaises(ValueError):
            self.store.reserve_action("one", "group:B")
        self.assertTrue(self.store.reserve_action("explicit", "group:A", "content-one")[0])
        with self.assertRaises(ValueError):
            self.store.reserve_action("explicit", "group:A", "different-content")

    def test_group_serialization_and_cross_group_progress(self):
        release, slow_entered, other_finished = threading.Event(), threading.Event(), threading.Event()
        completed = []
        def handler(value):
            identity = value["id"]
            if identity == "a1":
                slow_entered.set()
                release.wait(3)
            if identity == "b1":
                other_finished.set()
            completed.append(identity)
            return "accepted"
        dispatcher = GroupDispatcher(self.store, handler, workers=2)
        self.addCleanup(dispatcher.close)
        self.addCleanup(release.set)
        dispatcher.put(event("A", "a1"))
        self.assertTrue(slow_entered.wait(2))
        dispatcher.put(event("A", "a2"))
        dispatcher.put(event("B", "b1"))
        self.assertTrue(other_finished.wait(2))
        self.assertNotIn("a2", completed)
        release.set()
        deadline = time.monotonic() + 3
        while len(completed) < 3 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertLess(completed.index("a1"), completed.index("a2"))

    def test_pending_scan_does_not_starve_other_groups(self):
        for i in range(300):
            self.store.enqueue(event("A", str(i)))
        self.store.enqueue(event("B", "different"))
        self.assertEqual(len(self.store.pending()), 2)
