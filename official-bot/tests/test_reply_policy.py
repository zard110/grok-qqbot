import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.delivery import DeliveryStore, ReplySuppressed
from core.reply_policy import metadata, settings, event_context, topic_tokens


class ReplyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "runtime.sqlite3")
        self.limits = settings()

    def reserve(self, identity, trigger="natural", source="", text="hello", topic="骑行 训练 功率", scope="g", **kw):
        return self.store.reserve_action(identity, scope, text, reply_meta=metadata(trigger, source, text, [], topic_text=topic, sticker=kw.pop("sticker", False)), limits=self.limits, **kw)

    def test_concurrent_routines_claim_same_source_once(self):
        def attempt(i):
            try:
                return self.reserve(str(i), "at", "event", text=str(i))[0]
            except ReplySuppressed:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(12))), 1)

    def test_source_survives_unknown_and_arbitrary_age(self):
        self.reserve("first", "at", "source")
        self.store.action_status("first", "unknown")
        with self.store.connect() as db:
            db.execute("UPDATE reply_claims SET created=0")
        with self.assertRaisesRegex(ReplySuppressed, "source_event"):
            self.reserve("retry-other-routine", "at", "source", text="different", force=True)

    def test_same_content_topic_and_independent_at_thresholds(self):
        self.reserve("one", source="one", text="Hello!")
        self.store.action_status("one", "sent")
        with self.assertRaisesRegex(ReplySuppressed, "duplicate_content"):
            self.reserve("two", source="two", text="hello")
        self.assertTrue(self.reserve("at", "at", "explicit", text="Hello!")[0])
        with self.store.connect() as db:
            db.execute("UPDATE reply_claims SET created=?", (time.time() - 150,))
            db.execute("UPDATE outbox SET updated=?", (time.time() - 150,))
        with self.assertRaisesRegex(ReplySuppressed, "topic_recently"):
            self.reserve("topic", source="topic", text="a rewording")
        self.assertTrue(self.reserve("other-group", source="one", scope="other")[0])

    def test_unknown_failed_do_not_consume_cooldown_and_force_is_explicit(self):
        self.reserve("one", source="one")
        self.store.action_status("one", "unknown")
        self.assertEqual(self.store.recent_actions("g"), [])
        self.assertTrue(self.reserve("two", source="two", text="different")[0])
        self.store.action_status("two", "failed")
        self.assertFalse(self.reserve("one", source="one")[0])
        self.assertTrue(self.reserve("one", source="one", force=True)[0])
        with self.assertRaises(ValueError):
            self.reserve("one", source="one", text="changed", force=True)

    def test_live_pending_and_success_are_not_forced(self):
        self.reserve("one")
        self.assertFalse(self.reserve("one", force=True)[0])
        self.store.action_status("one", "sent")
        self.assertFalse(self.reserve("one", force=True)[0])

    def test_sticker_pacing_and_hourly_limit(self):
        self.reserve("s1", "at", "s1", sticker=True)
        self.store.action_status("s1", "sent")
        with self.assertRaisesRegex(ReplySuppressed, "sticker_cooldown"):
            self.reserve("s2", "at", "s2", text="other", sticker=True)
        self.limits["sticker_cooldown"] = 0
        self.limits["sticker_hourly_limit"] = 1
        with self.assertRaisesRegex(ReplySuppressed, "sticker_hourly_quota"):
            self.reserve("s2", "at", "s2", text="other", sticker=True)

    def test_inbound_burst_debounce_is_durable_and_scoped(self):
        tokens = topic_tokens("骑行 训练 功率")
        self.assertTrue(self.store.admit_natural("g", "topic", tokens, 12))
        other = DeliveryStore(self.store.path)
        self.assertFalse(other.admit_natural("g", "topic", tokens, 12))
        self.assertTrue(other.admit_natural("other", "topic", tokens, 12))
        with patch("core.delivery.time.time", return_value=time.time() + 20):
            self.assertTrue(other.admit_natural("g", "topic", tokens, 12))

    def test_event_context_distinguishes_triggers_and_stable_identity(self):
        event = {"msg_id": "one", "group_openid": "group", "text_only": "怎么设置？"}
        self.assertEqual(event_context(event)["trigger"], "question")
        self.assertEqual(event_context({**event, "at_me": True})["trigger"], "at")
        self.assertEqual(event_context({**event, "text_only": "/记忆"})["trigger"], "command")
        self.assertEqual(event_context({**event, "text_only": "哈哈"})["action_id"], event_context(event)["action_id"])

    def test_invalid_policy_is_rejected(self):
        for value in (float("nan"), -1, True, "120"):
            with self.assertRaises(ValueError):
                settings({"reply_policy": {"natural_cooldown": value}})
