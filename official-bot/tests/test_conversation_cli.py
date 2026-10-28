import json
import unittest
from unittest.mock import patch

import test_delivery_cli as cli
from core import conversation as C, reply_policy


class ConversationCLITests(unittest.TestCase):
    setUp = cli.SenderTests.setUp
    run_cli = cli.SenderTests.run_cli

    def event(self, source, text="比较一下", actor="alice", at=True, **extra):
        event = {"msg_id": source, "member_openid": actor, "text_only": text, "at_me": at, **extra}
        return C.record_input(self.store, "/v2/groups/" + "A" * 32, event, reply_policy.event_context(event))

    def decision(self, action="clarify", parent="", **extra):
        return json.dumps({"action": action, "intent": "request", "conversation_id": parent,
                          **({"question": "用在什么场景？", "options": ["后台", "官网"]} if action == "clarify" else {"text": "按后台场景给出比较"}),
                          **extra}, ensure_ascii=False)

    def test_ask_then_short_answer_bypasses_cooldown_and_duplicate_is_safe(self):
        self.event("one")
        self.assertEqual(self.run_cli("--decision", self.decision(), "--msg-id", "one")[0], 0)
        context = self.event("two", "第二个", at=False)
        answer = self.decision("answer", context["pending"]["id"])
        args = ("--decision", answer, "--msg-id", "two")
        self.assertEqual(self.run_cli(*args)[0], 0)
        self.assertTrue(self.run_cli(*args)[1]["duplicate"])
        self.assertEqual(self.api.call_count, 2)
        self.authorization.assert_called_with("A" * 32, "reply", "message.send")

    def test_plain_choices_are_default_and_include_free_text(self):
        self.event("one")
        code, result = self.run_cli("--decision", self.decision(), "--msg-id", "one", "--choice-buttons", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(result["plan"][0]["msg_type"], 0)
        self.assertIn("1. 后台", result["plan"][0]["content"])
        self.assertIn("直接补充", result["plan"][0]["content"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM conversation_questions").fetchone()[0], 0)
        self.api.assert_not_called()

    def test_verified_choices_fallback_only_on_definite_format_rejection(self):
        self.event("one")
        cfg = {"enabled": True, "features": {"buttons": True}, "message_format": {"markdown_verified": True}}
        self.api.side_effect = [(400, {"message": "keyboard not supported"}), (200, {"id": "plain"})]
        with patch.object(cli.sender.G, "load_config", return_value=cfg):
            code, result = self.run_cli("--decision", self.decision(), "--msg-id", "one", "--choice-buttons")
        self.assertEqual(code, 0)
        self.assertIn("keyboard", self.api.call_args_list[0].args[2])
        self.assertEqual(self.api.call_args_list[1].args[2]["msg_type"], 0)
        self.assertIsNotNone(self.event("two")["pending"])

    def test_semantic_request_without_keywords_uses_question_policy(self):
        self.event("initial")
        self.assertEqual(self.run_cli("--text", "闲聊回复", "--trigger", "natural", "--msg-id", "initial")[0], 0)
        self.event("request", "轮到剩下的三个了", at=False)
        code, result = self.run_cli("--decision", self.decision("answer"), "--msg-id", "request")
        self.assertEqual(code, 0)
        self.assertEqual(self.api.call_count, 2)
        with self.store.connect() as db:
            meta = json.loads(db.execute("SELECT metadata FROM reply_claims ORDER BY created DESC LIMIT 1").fetchone()[0])
        self.assertEqual(meta["trigger"], "question")

    def test_silence_and_missing_source_do_not_send(self):
        code, result = self.run_cli("--decision", '{"action":"ignore"}')
        self.assertEqual(code, 0)
        self.assertTrue(result["skipped"])
        with self.assertRaises(ValueError):
            self.run_cli("--decision", self.decision(), "--msg-id", "invented")
        self.api.assert_not_called()

    def test_unknown_question_send_never_becomes_active(self):
        self.event("one")
        self.api.return_value = (500, {})
        self.assertEqual(self.run_cli("--decision", self.decision(), "--msg-id", "one")[0], 5)
        self.assertIsNone(self.event("two")["pending"])
