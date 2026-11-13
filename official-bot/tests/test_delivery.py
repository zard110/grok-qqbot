import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.collection import CollectionScheduler
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

    def _smash(self, payload: bytes | None = None):
        for suffix in ("-wal", "-shm"):
            side = Path(str(self.store.path) + suffix)
            if side.exists():
                side.unlink()
        body = payload if payload is not None else (b"\x17\x03\x03\x00\x20" + b"\xab" * 32 + b"\x00" * 64)
        self.store.path.write_bytes(body)

    def test_tls_header_is_quarantined_and_a_new_store_opens(self):
        self.store.enqueue(event("A", "kept-until-overwrite"))
        wal = Path(str(self.store.path) + "-wal")
        wal.write_bytes(b"wal-pages")
        original = self.store.path.read_bytes()
        record = b"\x17\x03\x03\x00\x10" + b"\xcd" * 16
        self.store.path.write_bytes(record + original[len(record):])
        # Bridge startup uses fatal_on_corrupt and must come back, not exit.
        revived = DeliveryStore(self.store.path, fatal_on_corrupt=True)
        self.assertTrue(revived.path.read_bytes().startswith(b"SQLite format 3\x00"))
        backups = sorted(
            path for path in self.store.path.parent.glob("runtime.sqlite3.corrupt-bak-*")
            if not path.name.endswith(("-wal", "-shm"))
        )
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].read_bytes().startswith(b"\x17\x03\x03"))
        self.assertEqual(Path(str(backups[0]) + "-wal").read_bytes(), b"wal-pages")
        self.assertFalse(wal.exists())
        revived.enqueue(event("B", "after-recovery"))
        self.assertEqual(len(revived.pending()), 1)
        # A healthy file must stay where it is across a later open.
        DeliveryStore(revived.path)
        leftovers = [
            path for path in revived.path.parent.glob("runtime.sqlite3.corrupt-bak-*")
            if not path.name.endswith(("-wal", "-shm"))
        ]
        self.assertEqual(len(leftovers), 1)

    def test_live_bridge_does_not_rebuild_a_tls_header_it_exits(self):
        self._smash()
        with patch("core.delivery.os._exit") as stop:
            with self.assertRaises(sqlite3.DatabaseError):
                DeliveryStore(self.store.path, fatal_on_corrupt=True, recover_tls_header=False)
        stop.assert_called_once_with(4)
        self.assertTrue(self.store.path.read_bytes().startswith(b"\x17\x03\x03"))
        self.assertEqual(list(self.store.path.parent.glob("*.corrupt-bak-*")), [])

    def test_other_sqlite_errors_do_not_abort_the_process(self):
        store = DeliveryStore(self.store.path, fatal_on_corrupt=True)
        with patch("core.delivery.os._exit") as stop:
            with self.assertRaises(sqlite3.OperationalError):
                with store.connect() as db:
                    db.execute("SELECT * FROM missing_table")
        stop.assert_not_called()

    def test_dispatcher_exits_instead_of_dropping_events(self):
        self._smash()
        seen = threading.Event()
        codes = []

        def fake_exit(code):
            codes.append(code)
            seen.set()

        with patch("core.delivery.os._exit", side_effect=fake_exit):
            dispatcher = GroupDispatcher(self.store, lambda _event: "accepted", workers=1)
            self.addCleanup(dispatcher.close)
            self.assertTrue(seen.wait(2), codes)
        self.assertIn(4, codes)

    def test_collection_poll_exits_when_the_store_is_not_a_database(self):
        self._smash()
        scheduler = CollectionScheduler(self.store, lambda _payload: "accepted", start=False)
        with patch("core.delivery.os._exit") as stop:
            scheduler.poll()
        stop.assert_called_once_with(4)
