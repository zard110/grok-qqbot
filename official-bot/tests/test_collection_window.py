"""Fixed collection window: full wait, no extension, bypass, restart, and the legacy path."""
import concurrent.futures
import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.collection import CollectionScheduler, armed_window_seconds, is_collection_bypass
from core.delivery import DeliveryStore, GroupDispatcher, ReplySuppressed, coalesce_same_sender, event_identity
from core.reply_policy import metadata, settings, topic_anchors, topic_tokens
import groups as G

spec = importlib.util.spec_from_file_location(
    "collection_gateway_under_test", Path(__file__).resolve().parents[1] / "official-ws-bridge.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)

GROUP = "A" * 32
OTHER = "B" * 32


def qq_event(group, mid, member, text, nick, at=False):
    return {
        "t": "GROUP_AT_MESSAGE_CREATE" if at else "GROUP_MESSAGE_CREATE",
        "id": "evt-" + mid,
        "d": {
            "group_openid": group,
            "id": mid,
            "content": text,
            "author": {"member_openid": member, "username": nick},
        },
    }


def payload_for(group, mid, member, text, nick, delivery_id):
    return {
        "post_type": "message",
        "group_openid": group,
        "group_enabled": True,
        "features": {"natural_reply": True, "at_reply": True},
        "text_only": text,
        "raw_message": text,
        "msg_id": mid,
        "message_id": mid,
        "member_openid": member,
        "user_id": 7,
        "sender": {"nickname": nick, "user_id": 7},
        "at_me": False,
        "delivery_id": delivery_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "attachments": [],
    }


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "runtime.sqlite3")
        self.scope = "group:" + GROUP

    def park(self, when, mid, member, text, nick, window=10.0, group=GROUP):
        event = qq_event(group, mid, member, text, nick)
        with patch("core.delivery.time.time", return_value=when):
            identity = self.store.enqueue(event)
        self.assertEqual(event_identity(event)[1], "group:" + group)
        self.assertTrue(self.store.claim(identity))
        parked = self.store.enroll_collection(
            scope="group:" + group, delivery_id=identity,
            payload=payload_for(group, mid, member, text, nick, identity),
            window_seconds=window)
        return identity, parked

    def statuses(self):
        with self.store.connect() as db:
            return {row["id"]: row["status"] for row in db.execute("SELECT id, status FROM inbox")}

    def windows(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT generation, opened, deadline, status, anchor_id, window_seconds FROM collection_windows ORDER BY generation")]


class FixedWindowTests(StoreFixture):
    def test_settings_default_off_and_distinct_from_debounce(self):
        value = settings({})
        self.assertEqual(value["collection_window_seconds"], 0)
        self.assertEqual(value["natural_repeat_seconds"], 0)
        self.assertEqual(value["inbound_debounce"], 12)
        self.assertEqual(settings({"reply_policy": {"collection_window_seconds": 10}})["collection_window_seconds"], 10)
        G.validate_config({"reply_policy": {"collection_window_seconds": 10}})
        for bad in (-1, True, "10", float("nan")):
            with self.assertRaises(ValueError):
                settings({"reply_policy": {"collection_window_seconds": bad}})
        with self.assertRaises(G.ConfigError):
            G.validate_config({"reply_policy": {"collection_window_seconds": -1}})

    def test_feature_stays_off_for_disabled_and_only_at_groups(self):
        armed = {"enabled": True, "features": {"natural_reply": True},
                 "reply_policy": {"collection_window_seconds": 10}}
        self.assertEqual(armed_window_seconds(armed), 10)
        self.assertEqual(armed_window_seconds({**armed, "enabled": False}), 0)
        self.assertEqual(armed_window_seconds({**armed, "features": {"natural_reply": False}}), 0)
        self.assertEqual(armed_window_seconds({**armed, "reply_policy": {"collection_window_seconds": 0}}), 0)
        self.assertEqual(armed_window_seconds(None), 0)
        self.assertTrue(is_collection_bypass({"at_me": True, "text_only": "在吗"}))
        self.assertTrue(is_collection_bypass({"text_only": "/附近 咖啡"}))
        self.assertTrue(is_collection_bypass({"text_only": "  /记忆 查看"}))
        self.assertFalse(is_collection_bypass({"at_me": False, "text_only": "怎么安排间歇"}))
        self.assertFalse(is_collection_bypass({"text_only": "今天练爬坡"}))

    def test_full_window_forwards_once_with_every_message(self):
        sent = []
        clock = {"t": 0.0}
        sched = CollectionScheduler(self.store, lambda payload: sent.append(payload) or "accepted",
                                    clock=lambda: clock["t"], start=False)
        first, parked = self.park(0, "m0", "alice", "今天练爬坡", "Alice")
        self.park(2, "m2", "bob", "怎么安排间歇", "Bob")
        self.park(8, "m8", "alice", "我想骑一小时", "Alice")
        self.assertEqual(parked["deadline"], 10)
        self.assertEqual(self.windows()[0]["deadline"], 10)
        clock["t"] = 9.9
        sched.poll()
        self.assertEqual(sent, [])
        self.assertEqual(set(self.statuses().values()), {"collecting"})
        clock["t"] = 10
        sched.poll()
        sched.poll()
        self.assertEqual(len(sent), 1)
        body = sent[0]
        self.assertEqual(body["delivery_id"], first)
        self.assertEqual(body["collection"]["anchor_delivery_id"], first)
        self.assertEqual(body["collection"]["deadline"], 10)
        self.assertEqual(body["collection"]["window_seconds"], 10)
        self.assertEqual(body["msg_id"], "m8")
        self.assertFalse(body["at_me"])
        self.assertEqual([item["text"] for item in body["collection"]["messages"]],
                         ["今天练爬坡", "怎么安排间歇", "我想骑一小时"])
        self.assertEqual([item["nickname"] for item in body["collection"]["messages"]], ["Alice", "Bob", "Alice"])
        self.assertIn("Bob: 怎么安排间歇", body["text_only"])
        self.assertEqual(set(self.statuses().values()), {"accepted"})

    def test_later_arrivals_do_not_extend_the_deadline_and_the_next_window_is_separate(self):
        sent = []
        clock = {"t": 0.0}
        sched = CollectionScheduler(self.store, lambda payload: sent.append(json.loads(json.dumps(payload))) or "accepted",
                                    clock=lambda: clock["t"], start=False)
        self.park(0, "m0", "alice", "m0", "Alice")
        self.park(3, "m3", "bob", "m3", "Bob")
        self.park(6, "m6", "cara", "m6", "Cara")
        self.park(9, "m9", "alice", "m9", "Alice")
        self.assertEqual([row["deadline"] for row in self.windows()], [10])
        boundary, parked = self.park(10, "m10", "bob", "m10", "Bob")
        self.assertEqual(parked["deadline"], 20)
        self.assertEqual(parked["anchor_id"], boundary)
        self.assertEqual([(row["opened"], row["deadline"], row["status"]) for row in self.windows()],
                         [(0, 10, "flushing"), (10, 20, "open")])
        clock["t"] = 10
        sched.poll()
        self.assertEqual(len(sent), 1)
        self.assertEqual([item["text"] for item in sent[0]["collection"]["messages"]], ["m0", "m3", "m6", "m9"])
        clock["t"] = 19.5
        sched.poll()
        self.assertEqual(len(sent), 1)
        self.park(15, "m15", "alice", "m15", "Alice")
        self.assertEqual(self.windows()[1]["deadline"], 20)
        clock["t"] = 20
        sched.poll()
        self.assertEqual(len(sent), 2)
        self.assertEqual([item["text"] for item in sent[1]["collection"]["messages"]], ["m10", "m15"])
        self.assertEqual(sent[1]["delivery_id"], boundary)
        sched.poll()
        self.assertEqual(len(sent), 2)

    def test_continuous_stream_closes_on_the_original_deadlines(self):
        sent = []
        clock = {"t": 0.0}
        sched = CollectionScheduler(self.store, lambda payload: sent.append(payload) or "accepted",
                                    clock=lambda: clock["t"], start=False)
        for stamp in range(35):
            self.park(stamp, f"m{stamp}", "alice" if stamp % 2 == 0 else "bob", f"m{stamp}", "A" if stamp % 2 == 0 else "B")
        rows = self.windows()
        self.assertEqual([(row["opened"], row["deadline"]) for row in rows],
                         [(0, 10), (10, 20), (20, 30), (30, 40)])
        seen = []
        for moment in (10, 20, 30):
            clock["t"] = moment
            sched.poll()
            seen.extend(item["text"] for item in sent[-1]["collection"]["messages"])
        self.assertEqual(seen, [f"m{stamp}" for stamp in range(30)])
        self.assertEqual(len(sent), 3)
        clock["t"] = 39
        sched.poll()
        self.assertEqual(len(sent), 3)
        clock["t"] = 40
        sched.poll()
        self.assertEqual([item["text"] for item in sent[-1]["collection"]["messages"]],
                         [f"m{stamp}" for stamp in range(30, 35)])
        flat = [item["text"] for batch in sent for item in batch["collection"]["messages"]]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(set(self.statuses().values()), {"accepted"})

    def test_other_group_is_not_pulled_into_the_batch(self):
        sent = []
        clock = {"t": 0.0}
        sched = CollectionScheduler(self.store, lambda payload: sent.append(payload) or "accepted",
                                    clock=lambda: clock["t"], start=False)
        self.park(0, "a", "alice", "甲", "Alice")
        self.park(0, "b", "bob", "乙", "Bob", group=OTHER)
        clock["t"] = 10
        sched.poll()
        self.assertEqual({body["group_openid"] for body in sent}, {GROUP, OTHER})
        self.assertEqual({item["text"] for body in sent for item in body["collection"]["messages"]}, {"甲", "乙"})
        self.assertTrue(all(len(body["collection"]["messages"]) == 1 for body in sent))

    def test_restart_keeps_the_deadline_and_does_not_resend(self):
        sent = []

        def flusher(payload):
            sent.append(payload["delivery_id"])
            return "accepted"

        clock = {"t": 0.0}
        first, _parked = self.park(0, "m0", "alice", "甲", "Alice")
        self.park(4, "m4", "bob", "乙", "Bob")
        restarted = DeliveryStore(self.store.path)
        restarted.recover()
        self.assertEqual(set(self.statuses().values()), {"collecting"})
        self.assertEqual(restarted.lease_due_collections(9), [])
        sched = CollectionScheduler(restarted, flusher, clock=lambda: clock["t"], start=False)
        clock["t"] = 10
        sched.poll()
        self.assertEqual(sent, [first])
        DeliveryStore(self.store.path).recover()
        self.assertEqual(DeliveryStore(self.store.path).lease_due_collections(10), [])
        clock["t"] = 11
        sched.store = DeliveryStore(self.store.path)
        sched.poll()
        self.assertEqual(sent, [first])

    def test_crash_during_flush_retries_the_same_anchor_once(self):
        first, _parked = self.park(0, "m0", "alice", "甲", "Alice")
        self.park(1, "m1", "bob", "乙", "Bob")
        leased = self.store.lease_due_collections(10)
        self.assertEqual(len(leased), 1)
        self.assertTrue(self.store.begin_collection_flush(leased[0]["scope"], leased[0]["generation"]))
        recovered = DeliveryStore(self.store.path)
        recovered.recover()
        again = recovered.lease_due_collections(10)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["anchor_id"], first)
        self.assertEqual([item["text_only"] for item in again[0]["members"]], ["甲", "乙"])
        self.assertTrue(recovered.begin_collection_flush(again[0]["scope"], again[0]["generation"]))
        recovered.complete_collection(again[0]["scope"], again[0]["generation"], "accepted")
        recovered.recover()
        self.assertEqual(recovered.lease_due_collections(10), [])
        self.assertEqual(set(self.statuses().values()), {"accepted"})

    def test_terminal_unknown_is_not_sent_again(self):
        calls = []
        clock = {"t": 10.0}
        self.park(0, "m0", "alice", "甲", "Alice")
        sched = CollectionScheduler(self.store, lambda payload: calls.append(payload) or "unknown",
                                    clock=lambda: clock["t"], start=False)
        sched.poll()
        sched.poll()
        self.assertEqual(len(calls), 1)
        self.assertEqual(set(self.statuses().values()), {"unknown"})

    def test_duplicate_event_and_repeat_enroll_stay_single(self):
        event = qq_event(GROUP, "same", "alice", "甲", "Alice")
        with patch("core.delivery.time.time", return_value=0):
            first = self.store.enqueue(event)
            self.assertEqual(first, self.store.enqueue(event))
        self.store.claim(first)
        payload = payload_for(GROUP, "same", "alice", "甲", "Alice", first)
        self.store.enroll_collection(scope=self.scope, delivery_id=first, payload=payload, window_seconds=10)
        again = self.store.enroll_collection(scope=self.scope, delivery_id=first, payload=payload, window_seconds=10)
        self.assertEqual(again["ordinal"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_members").fetchone()[0], 1)

    def test_concurrent_enroll_is_one_window_and_lease_does_not_steal_it_early(self):
        errors = []
        identities = []
        lock = threading.Lock()
        barrier = threading.Barrier(17)

        def enroll(index):
            try:
                barrier.wait(2)
                event = qq_event(GROUP, f"c{index}", "alice" if index % 2 == 0 else "bob", f"t{index}", "N")
                identity = self.store.enqueue(event)
                self.assertTrue(self.store.claim(identity))
                self.store.enroll_collection(
                    scope=self.scope, delivery_id=identity,
                    payload=payload_for(GROUP, f"c{index}", event["d"]["author"]["member_openid"], f"t{index}", "N", identity),
                    window_seconds=30)
                with lock:
                    identities.append(identity)
            except Exception as exc:
                errors.append(repr(exc))

        def poll():
            try:
                barrier.wait(2)
                for _ in range(40):
                    if self.store.lease_due_collections(time.time()):
                        errors.append("early flush")
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=enroll, args=(index,)) for index in range(16)]
        threads.append(threading.Thread(target=poll))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(identities), 16)
        rows = self.windows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["deadline"] - rows[0]["opened"], 30)
        clock = rows[0]["deadline"]
        leased = self.store.lease_due_collections(clock)
        self.assertEqual(len(leased), 1)
        self.assertEqual({item["delivery_id"] for item in leased[0]["members"]}, set(identities))
        self.store.complete_collection(leased[0]["scope"], leased[0]["generation"], "accepted")
        self.assertEqual(self.store.lease_due_collections(clock), [])

    def test_live_scheduler_waits_out_the_window(self):
        sent = []

        def flusher(payload):
            sent.append((time.time(), payload))
            return "accepted"

        _first, parked = self.park(time.time(), "m0", "alice", "甲", "Alice", window=1.0)
        self.park(time.time(), "m1", "bob", "乙", "Bob", window=1.0)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_windows").fetchone()[0], 1)
        sched = CollectionScheduler(self.store, flusher, start=True)
        self.addCleanup(sched.close)
        sched.wake()
        with self.store.connect() as db:
            deadline = db.execute("SELECT deadline FROM collection_windows").fetchone()["deadline"]
        self.assertEqual(deadline, parked["deadline"])
        limit = time.time() + 3
        while not sent and time.time() < limit:
            time.sleep(0.02)
        self.assertEqual(len(sent), 1)
        self.assertGreaterEqual(sent[0][0], deadline - 0.05)
        self.assertEqual([item["text"] for item in sent[0][1]["collection"]["messages"]], ["甲", "乙"])
        time.sleep(0.2)
        self.assertEqual(len(sent), 1)


class LegacyCoalesceTests(StoreFixture):
    def test_claim_followup_stops_at_another_sender(self):
        """The old 3.5s path cannot see past a different sender, so it is not a group window."""
        anchor = self.store.enqueue(qq_event(GROUP, "anchor", "alice", "甲", "Alice"))
        self.store.claim(anchor)
        other = self.store.enqueue(qq_event(GROUP, "other", "bob", "乙", "Bob"))
        same = self.store.enqueue(qq_event(GROUP, "same", "alice", "丙", "Alice"))
        clock = {"t": 0.0}
        absorbed = []
        coalesce_same_sender(
            self.store, self.scope, "alice", wait_sec=3.5,
            absorb=lambda event: absorbed.append(event["id"]) or True,
            now=lambda: clock["t"], sleep=lambda _seconds: clock.__setitem__("t", clock["t"] + 0.25))
        self.assertEqual(absorbed, [])
        self.assertGreaterEqual(clock["t"], 3.5)
        self.assertLess(clock["t"], 4)
        with self.store.connect() as db:
            pending = [row["id"] for row in db.execute("SELECT id FROM inbox WHERE status='pending' ORDER BY rowid")]
        self.assertEqual(pending, [other, same])

    def test_same_sender_followups_slide_the_deadline(self):
        """New same-sender arrivals replace the deadline with now+1.5s, so the wait is not fixed."""
        anchor = self.store.enqueue(qq_event(GROUP, "anchor", "alice", "甲", "Alice"))
        self.store.claim(anchor)
        clock = {"t": 0.0}
        fed = {"n": 0}
        absorbed_at = []

        def absorb(_event):
            absorbed_at.append(clock["t"])
            return True

        def pause(_seconds):
            clock["t"] += 0.25
            if clock["t"] <= 4:
                fed["n"] += 1
                self.store.enqueue(qq_event(GROUP, f"f{fed['n']}", "alice", "续", "Alice"))

        coalesce_same_sender(self.store, self.scope, "alice", wait_sec=3.5, absorb=absorb,
                             now=lambda: clock["t"], sleep=pause)
        self.assertTrue(absorbed_at)
        self.assertTrue(any(stamp > 3.5 for stamp in absorbed_at))

    def test_admit_natural_is_leading_edge_suppression(self):
        """inbound_debounce forwards the first hit and drops the rest. It does not collect."""
        tokens = ["骑行", "训练"]
        self.assertTrue(self.store.admit_natural(self.scope, "topic", tokens, 12))
        self.assertFalse(self.store.admit_natural(self.scope, "topic", tokens, 12))
        self.assertTrue(self.store.admit_natural("group:" + OTHER, "topic", tokens, 12))


def _bridge(store, cfg, forwards):
    bridge = gateway.Bridge.__new__(gateway.Bridge)
    bridge.store = store
    bridge.bot_openid = "bot-openid"
    bridge.our_group = ""
    bridge.collector = CollectionScheduler(store, bridge._flush_collected, clock=lambda: cfg["clock"], start=False)
    bridge.q = GroupDispatcher(store, bridge._dispatch, workers=2)
    return bridge


class BridgeWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "group").mkdir()
        self.store = DeliveryStore(self.root / "runtime.sqlite3")
        self.forwards = []
        self.lock = threading.Lock()
        self.cfg = {
            "enabled": True,
            "name": "测试群",
            "aliases": ["room"],
            "revision": 1,
            "features": {"natural_reply": True, "at_reply": True, "media_understanding": True},
            "reply_policy": {"collection_window_seconds": 30, "inbound_debounce": 12},
            "group_openid": GROUP,
            "clock": time.time(),
        }

    def tearDown(self):
        if hasattr(self, "bridge"):
            self.bridge.q.close()
            self.bridge.collector.close()

    def start(self):
        def fake_forward(payload):
            with self.lock:
                self.forwards.append(json.loads(json.dumps(payload)))
            return "accepted"

        self.forward_patch = patch.object(gateway, "forward", side_effect=fake_forward)
        self.forward_patch.start()
        self.addCleanup(self.forward_patch.stop)
        self.bridge = _bridge(self.store, self.cfg, self.forwards)
        patches = [
            patch.object(gateway, "DIR", self.root),
            patch.object(gateway, "group_cfg", lambda goid, extra=None: self.cfg),
            patch.object(gateway.G, "group_dir", lambda goid: self.root / "group"),
            patch.object(gateway, "REF_CACHE_FILE", self.root / "refs.json"),
            patch.object(gateway, "coalesce_same_sender", side_effect=AssertionError("legacy coalesce slept the dispatcher")),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def wait_until(self, predicate, timeout=3):
        limit = time.time() + timeout
        while time.time() < limit:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out with forwards={len(self.forwards)} inbox={self.inbox()}")

    def inbox(self):
        with self.store.connect() as db:
            return [(row["status"], row["error"]) for row in db.execute("SELECT status, error FROM inbox ORDER BY rowid")]

    def test_at_and_slash_bypass_under_load_without_sleeping_the_dispatcher(self):
        self.start()
        for index in range(12):
            self.bridge.q.put(qq_event(GROUP, f"o{index}", "alice" if index % 2 == 0 else "bob",
                                       "怎么安排间歇" if index == 3 else f"普通{index}", "Alice" if index % 2 == 0 else "Bob"))
        self.bridge.q.put(qq_event(GROUP, "at", "carol", "<@bot> 在吗", "Carol", at=True))
        self.bridge.q.put(qq_event(GROUP, "slash", "bob", "/附近 咖啡", "Bob"))
        for index in range(12, 24):
            self.bridge.q.put(qq_event(GROUP, f"o{index}", "alice", f"普通{index}", "Alice"))
        self.wait_until(lambda: sum(1 for item in self.forwards if item.get("at_me") or str(item.get("text_only", "")).startswith("/")) == 2
                        and sum(1 for status, _error in self.inbox() if status == "collecting") == 24)
        early = list(self.forwards)
        self.assertEqual(len(early), 2)
        self.assertTrue(any(item.get("at_me") for item in early))
        self.assertTrue(any(str(item.get("text_only", "")).startswith("/附近") for item in early))
        self.assertTrue(all("collection" not in item for item in early))
        self.cfg["clock"] = time.time() + 100
        self.bridge.collector.clock = lambda: self.cfg["clock"]
        self.bridge.collector.poll()
        batches = [item for item in self.forwards if "collection" in item]
        self.assertEqual(len(batches), 1)
        texts = [item["text"] for item in batches[0]["collection"]["messages"]]
        self.assertEqual(len(texts), 24)
        self.assertIn("怎么安排间歇", texts)
        self.assertNotIn("/附近 咖啡", "\n".join(texts))
        self.assertTrue(all(not item.get("at_me") for item in batches[0]["collection"]["messages"]))
        self.assertGreaterEqual(len({item["member_openid"] for item in batches[0]["collection"]["messages"]}), 2)
        self.bridge.collector.poll()
        self.assertEqual(len([item for item in self.forwards if "collection" in item]), 1)
        self.bridge.q.put(qq_event(GROUP, "next", "bob", "下一窗", "Bob"))
        self.wait_until(lambda: sum(1 for status, _error in self.inbox() if status == "collecting") == 1)
        with self.store.connect() as db:
            deadlines = [row["deadline"] for row in db.execute("SELECT deadline FROM collection_windows WHERE status!='closed'")]
        self.assertEqual(len(deadlines), 1)
        self.assertNotEqual(deadlines[0], batches[0]["collection"]["deadline"])
        self.cfg["clock"] = deadlines[0]
        self.bridge.collector.poll()
        batches = [item for item in self.forwards if "collection" in item]
        self.assertEqual([item["text"] for item in batches[-1]["collection"]["messages"]], ["下一窗"])

    def test_disabled_window_keeps_legacy_path_and_only_at_groups_do_not_collect(self):
        cases = (
            {"reply_policy": {"collection_window_seconds": 0}, "features": {"natural_reply": True, "at_reply": True, "media_understanding": True}},
            {"reply_policy": {"collection_window_seconds": 10}, "features": {"natural_reply": False, "at_reply": True, "media_understanding": True}},
            {"enabled": False, "reply_policy": {"collection_window_seconds": 10},
             "features": {"natural_reply": True, "at_reply": True, "media_understanding": True}},
        )
        for override in cases:
            with self.subTest(override=override):
                self.forwards.clear()
                self.cfg.update({"enabled": True, "features": {"natural_reply": True, "at_reply": True, "media_understanding": True},
                                 "reply_policy": {"collection_window_seconds": 30}})
                self.cfg.update(override)
                self.seq = getattr(self, "seq", 0) + 1
                calls = []
                real = gateway.coalesce_same_sender

                def spy(*args, **kwargs):
                    calls.append(kwargs.get("wait_sec"))
                    return real(*args, **kwargs)

                with patch.object(gateway, "SAME_SENDER_FOLLOWUP_SECONDS", 0), \
                     patch.object(gateway, "forward", side_effect=lambda payload: self.forwards.append(payload) or "accepted"), \
                     patch.object(gateway, "DIR", self.root), \
                     patch.object(gateway, "group_cfg", lambda goid, extra=None: self.cfg), \
                     patch.object(gateway.G, "group_dir", lambda goid: self.root / "group"), \
                     patch.object(gateway, "REF_CACHE_FILE", self.root / "refs.json"), \
                     patch.object(gateway, "coalesce_same_sender", side_effect=spy):
                    bridge = gateway.Bridge.__new__(gateway.Bridge)
                    bridge.store = self.store
                    bridge.bot_openid = "bot-openid"
                    bridge.our_group = ""
                    bridge.collector = CollectionScheduler(self.store, bridge._flush_collected, start=False)
                    bridge.q = GroupDispatcher(self.store, bridge._dispatch, workers=1)
                    try:
                        bridge.q.put(qq_event(GROUP, f"plain-{self.seq}", "alice", "今天练爬坡", "Alice"))
                        self.wait_until(lambda: len(self.forwards) == 1)
                    finally:
                        bridge.q.close()
                        bridge.collector.close()
                self.assertEqual(calls, [0])
                self.assertNotIn("collection", self.forwards[0])
                with self.store.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM collection_members").fetchone()[0], 0)
                self.forwards.clear()


class ForwardGateTests(unittest.TestCase):
    def test_debounce_does_not_swallow_a_collected_batch(self):
        class Response(io.BytesIO):
            status = 202

        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b"{}")
        group = GROUP
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DeliveryStore(root / "runtime.sqlite3")
            action = {"type": "routine.reply", "group_openid": group, "plugin_id": "natural_reply", "config_revision": 1}
            with patch.object(gateway, "DIR", root), patch.object(gateway, "DeliveryStore", return_value=store), \
                 patch.object(gateway.G, "group_dir", return_value=root / "group"), \
                 patch.object(gateway.G, "load_config", return_value={"revision": 1, "reply_policy": {"inbound_debounce": 100}}), \
                 patch.object(gateway.G, "plugin_enabled", return_value=True), \
                 patch("core.plugins.dispatch", return_value={"actions": [action], "errors": []}), \
                 patch("core.action_runner.run_actions", return_value=[]), \
                 patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/test" if str(path).endswith("url") else "fixture-key"), \
                 patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                natural = {"post_type": "message", "group_openid": group, "text_only": "骑行训练",
                           "msg_id": "one", "member_openid": "alice", "delivery_id": "one",
                           "features": {"natural_reply": True, "at_reply": True}}
                self.assertEqual(gateway.forward(dict(natural)), "accepted")
                batch = {**natural, "msg_id": "batch-last", "delivery_id": "anchor",
                         "text_only": "Alice: 骑行训练\nBob: 骑行训练",
                         "collection": {"window_seconds": 10, "messages": [{"text": "骑行训练"}, {"text": "骑行训练"}],
                                        "anchor_delivery_id": "anchor", "deadline": 10, "opened": 0, "generation": 1}}
                self.assertEqual(gateway.forward(batch), "accepted")
                self.assertEqual(gateway.forward({**natural, "msg_id": "three", "delivery_id": "three"}), "skipped")
            self.assertEqual(opener.open.call_count, 2)
            request = opener.open.call_args_list[1].args[0]
            self.assertEqual(request.get_header("Idempotency-key"), "anchor")
            body = json.loads(request.data)
            self.assertEqual(len(body["collection"]["messages"]), 2)

    def test_only_at_and_disabled_groups_still_do_not_forward_a_batch(self):
        opener = Mock()
        action = {"type": "routine.reply", "group_openid": GROUP, "plugin_id": "natural_reply", "config_revision": 1}
        for features, enabled in (({"natural_reply": False, "at_reply": True}, True),
                                  ({"natural_reply": True, "at_reply": True}, False)):
            with self.subTest(enabled=enabled):
                with tempfile.TemporaryDirectory() as directory, patch.object(gateway, "DIR", Path(directory)), \
                     patch("core.plugins.dispatch", return_value={"actions": [action], "errors": []}), \
                     patch("core.action_runner.run_actions", return_value=[]), \
                     patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                    outcome = gateway.forward({
                        "post_type": "message", "group_openid": GROUP, "group_enabled": enabled,
                        "features": features, "text_only": "Alice: 今天练爬坡", "msg_id": "m",
                        "member_openid": "alice", "delivery_id": "anchor", "at_me": False,
                        "collection": {"messages": [{"text": "今天练爬坡"}], "window_seconds": 10,
                                       "anchor_delivery_id": "anchor", "deadline": 1, "opened": 0, "generation": 1},
                    })
                self.assertEqual(outcome, "skipped")
        opener.open.assert_not_called()


class RepeatGuardTests(unittest.TestCase):
    """Opt-in burst claim. Groups that leave natural_repeat_seconds at 0 are unchanged."""

    TOPIC = "瓶盖漂流3D已经挤满了"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "runtime.sqlite3")
        self.limits = settings()
        self.limits["natural_repeat_seconds"] = 180
        self.limits["natural_cooldown"] = 0
        self.limits["topic_window"] = 0
        self.limits["content_window"] = 0
        self.limits["question_cooldown"] = 0

    def reserve(self, identity, source, topic, trigger="natural", scope="room"):
        meta = metadata(trigger, source, "人好多，挤满了", [], topic_text=topic)
        return self.store.reserve_action(identity, scope, identity, reply_meta=meta, limits=self.limits)

    def test_repeat_stays_off_unless_the_group_opts_in(self):
        self.limits["natural_repeat_seconds"] = 0
        self.assertTrue(self.reserve("one", "s1", self.TOPIC)[0])
        self.assertTrue(self.reserve("two", "s2", "瓶盖漂流人也太多了")[0])
        self.assertTrue(self.store.claim_natural_forward(
            scope="room", batch_id="a", sources=["s1"], tokens=topic_tokens(self.TOPIC),
            anchors=topic_anchors(self.TOPIC), weak=False, window=0))

    def test_concurrent_wakes_and_answers_collapse_to_one(self):
        text = self.TOPIC
        tokens, anchors = topic_tokens(text), topic_anchors(text)

        def wake(index):
            return self.store.claim_natural_forward(
                scope="room", batch_id=f"b{index}", sources=[f"s{index}"],
                tokens=tokens, anchors=anchors, weak=False, window=180)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            admitted = list(pool.map(wake, range(12)))
        self.assertEqual(sum(admitted), 1)
        self.assertFalse(self.store.claim_natural_forward(
            scope="room", batch_id="image", sources=["img"], tokens=[], anchors=[],
            weak=True, window=180))
        self.assertTrue(self.store.claim_natural_forward(
            scope="other", batch_id="elsewhere", sources=["z"], tokens=topic_tokens("今晚去香山看日落"),
            anchors=topic_anchors("今晚去香山看日落"), weak=False, window=180))

        send_scope = "/v2/groups/" + "A" * 32
        self.store.claim_natural_forward(
            scope="A" * 32, batch_id="batch", sources=["m1", "m2", "m3"],
            tokens=tokens, anchors=anchors, weak=False, window=180)

        def answer(index):
            try:
                return self.reserve(f"a{index}", f"n{index}", self.TOPIC, scope=send_scope)[0]
            except ReplySuppressed:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(answer, range(8)))
        self.assertEqual(sum(bool(item) for item in answers), 1)

    def test_batch_mates_images_and_questions_have_different_outcomes(self):
        tokens, anchors = topic_tokens(self.TOPIC), topic_anchors(self.TOPIC)
        self.assertTrue(self.store.claim_natural_forward(
            scope="room", batch_id="batch", sources=["text", "image", "agree"],
            tokens=tokens, anchors=anchors, weak=False, window=180))
        self.assertTrue(self.reserve("first", "text", self.TOPIC)[0])
        self.store.action_status("first", "pending")
        with self.assertRaisesRegex(ReplySuppressed, "topic_recently_answered"):
            self.reserve("paraphrase", "agree", "人也太多了吧")
        with self.assertRaisesRegex(ReplySuppressed, "topic_recently_answered"):
            self.reserve("picture", "image", "[图片]")
        self.assertTrue(self.reserve("asked", "q1", "明天团骑怎么安排", trigger="question")[0])
        self.assertTrue(self.reserve("mention", "at1", self.TOPIC, trigger="at")[0])
        self.assertTrue(self.reserve("other-topic", "hill", "今晚去香山看日落")[0])

    def test_inflight_blocks_and_unknown_does_not(self):
        self.assertTrue(self.reserve("live", "s1", self.TOPIC)[0])
        with self.assertRaisesRegex(ReplySuppressed, "topic_recently_answered"):
            self.reserve("again", "s2", "瓶盖漂流这会儿挤爆了")
        self.store.action_status("live", "unknown")
        self.assertTrue(self.reserve("after-unknown", "s3", "瓶盖漂流这会儿挤爆了")[0])

    def test_lease_survives_restart_and_expires_on_its_original_deadline(self):
        tokens, anchors = topic_tokens(self.TOPIC), topic_anchors(self.TOPIC)
        self.assertTrue(self.store.claim_natural_forward(
            scope="room", batch_id="b", sources=["s"], tokens=tokens, anchors=anchors, weak=False, window=30))
        restarted = DeliveryStore(self.store.path)
        self.assertFalse(restarted.claim_natural_forward(
            scope="room", batch_id="later", sources=["s2"], tokens=tokens, anchors=anchors, weak=False, window=30))
        with patch("core.delivery.time.time", return_value=time.time() + 31):
            self.assertTrue(restarted.claim_natural_forward(
                scope="room", batch_id="after", sources=["s3"], tokens=tokens, anchors=anchors, weak=False, window=30))

    def test_unsent_wake_can_be_released(self):
        tokens, anchors = topic_tokens(self.TOPIC), topic_anchors(self.TOPIC)
        self.store.claim_natural_forward(
            scope="room", batch_id="b", sources=["s"], tokens=tokens, anchors=anchors, weak=False, window=30)
        self.store.release_natural_lease("room", "b")
        self.assertTrue(self.store.claim_natural_forward(
            scope="room", batch_id="retry", sources=["s2"], tokens=tokens, anchors=anchors, weak=False, window=30))

    def test_forward_keeps_one_burst_and_still_delivers_a_question(self):
        class Response(io.BytesIO):
            status = 202

        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: Response(b"{}")
        group = "C" * 32
        action = {"type": "routine.reply", "group_openid": group, "plugin_id": "natural_reply", "config_revision": 1}
        policy = {"natural_repeat_seconds": 180, "inbound_debounce": 0, "collection_window_seconds": 10}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = DeliveryStore(root / "runtime.sqlite3")
            with patch.object(gateway, "DIR", root), patch.object(gateway, "DeliveryStore", return_value=store), \
                 patch.object(gateway.G, "group_dir", return_value=root / "group"), \
                 patch.object(gateway.G, "load_config", return_value={"revision": 1, "reply_policy": policy}), \
                 patch.object(gateway.G, "plugin_enabled", return_value=True), \
                 patch("core.plugins.dispatch", return_value={"actions": [action], "errors": []}), \
                 patch("core.action_runner.run_actions", return_value=[]), \
                 patch.object(gateway, "rd", side_effect=lambda path: "https://example.com/test" if str(path).endswith("url") else "fixture-key"), \
                 patch.object(gateway.urllib.request, "build_opener", return_value=opener):
                batch = {"post_type": "message", "group_openid": group, "delivery_id": "anchor",
                         "msg_id": "agree", "text_only": "甲: 瓶盖漂流3D已经挤满了\n乙: [图片]\n丙: 确实",
                         "features": {"natural_reply": True, "at_reply": True},
                         "collection": {"window_seconds": 10, "anchor_delivery_id": "anchor", "generation": 1,
                                        "opened": 0, "deadline": 10,
                                        "messages": [
                                            {"text": "瓶盖漂流3D已经挤满了", "msg_id": "text"},
                                            {"text": "[图片]", "msg_id": "image"},
                                            {"text": "确实", "msg_id": "agree"}]}}
                self.assertEqual(gateway.forward(batch), "accepted")
                follow = {"post_type": "message", "group_openid": group, "delivery_id": "again",
                          "msg_id": "again", "text_only": "瓶盖漂流人也太多了",
                          "features": {"natural_reply": True, "at_reply": True}}
                self.assertEqual(gateway.forward(dict(follow)), "skipped")
                question = {**follow, "delivery_id": "ask", "msg_id": "ask", "text_only": "明天团骑怎么安排"}
                mentioned = {**follow, "delivery_id": "at", "msg_id": "at", "text_only": "瓶盖漂流3D已经挤满了", "at_me": True}
                other = {**follow, "delivery_id": "hill", "msg_id": "hill", "text_only": "今晚去香山看日落"}
                self.assertEqual(gateway.forward(question), "accepted")
                self.assertEqual(gateway.forward(mentioned), "accepted")
                self.assertEqual(gateway.forward(other), "accepted")
                quiet = {"revision": 1, "reply_policy": {"natural_repeat_seconds": 0, "inbound_debounce": 0}}
                with patch.object(gateway.G, "load_config", return_value=quiet):
                    self.assertEqual(gateway.forward({**follow, "group_openid": "D" * 32, "delivery_id": "d1", "msg_id": "d1"}), "accepted")
                    self.assertEqual(gateway.forward({**follow, "group_openid": "D" * 32, "delivery_id": "d2", "msg_id": "d2",
                                                      "text_only": "确实"}), "accepted")
        self.assertEqual(opener.open.call_count, 6)


if __name__ == "__main__":
    unittest.main()
