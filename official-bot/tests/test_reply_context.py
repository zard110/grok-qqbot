import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.reply_context import recover, MAX_BYTES


class ReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.group = "A" * 32

    def forward(self, **extra):
        return {"group_openid": self.group, "msg_id": "wanted", "text_only": "拿其它ui设计的skill对比一下", **extra}

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_exact_forward_match_and_reclassifying_old_natural_hint(self):
        self.write("last-forward.json", self.forward(reply_context={"source_event_id": "wanted", "trigger": "natural",
            "topic_text": "帮我给这5个打个分", "topic_key": "stable-topic"}))
        context = recover(self.root, self.group, "wanted")
        self.assertEqual(context["trigger"], "question")
        self.assertEqual(context["topic_key"], "stable-topic")
        self.assertEqual(context["topic_text"], "帮我给这5个打个分")

    def test_raw_gateway_log_fallback_preserves_at_and_matches_id(self):
        self.write("last-forward.json", self.forward(msg_id="newer"))
        raw = {"t": "GROUP_AT_MESSAGE_CREATE", "d": {"group_openid": self.group, "id": "wanted", "content": "<@bot> 继续"}}
        (self.root / "last-events.jsonl").write_text(json.dumps(raw) + '\n{"torn":', encoding="utf-8")
        context = recover(self.root, self.group, "wanted")
        self.assertEqual(context["trigger"], "at")
        self.assertEqual(context["topic_text"], "继续")
        raw["t"] = "GROUP_MESSAGE_CREATE"
        raw["d"]["content"] = "来个详细的对比，结合x上的评价"
        self.write("last-events.jsonl", raw)
        self.assertEqual(recover(self.root, self.group, "wanted")["trigger"], "question")

    def test_malformed_unmatched_cross_group_and_wrong_context_are_ignored(self):
        for data in ([], self.forward(msg_id="other"), self.forward(group_openid="B" * 32),
                     self.forward(message_id="conflict"), self.forward(reply_context={"source_event_id": "other"})):
            with self.subTest(data=data):
                self.write("last-forward.json", data)
                self.assertIsNone(recover(self.root, self.group, "wanted"))
        (self.root / "last-forward.json").write_text("broken", encoding="utf-8")
        self.assertIsNone(recover(self.root, self.group, "wanted"))

    def test_oversized_snapshot_uses_bounded_jsonl_tail(self):
        (self.root / "last-forward.json").write_bytes(b" " * (MAX_BYTES + 1))
        with (self.root / "last-events.jsonl").open("wb") as stream:
            stream.write(b"x" * (MAX_BYTES + 100))
            stream.write(b"\n" + json.dumps(self.forward(at_me=True)).encode() + b"\n")
        self.assertEqual(recover(self.root, self.group, "wanted")["trigger"], "at")
