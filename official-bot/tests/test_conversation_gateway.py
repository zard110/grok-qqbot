import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_gateway as gateway_tests
from core import conversation as C, reply_policy
from core.delivery import DeliveryStore
from core.memory import MemoryStore
from core.plugins import ReplyPlugin

gateway = gateway_tests.gateway


class ConversationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DeliveryStore(self.root / "runtime.sqlite3")
        self.group = "A" * 32
        self.scope = "/v2/groups/" + self.group
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(gateway, "DIR", self.root))
        self.stack.enter_context(patch.object(gateway, "DeliveryStore", return_value=self.store))
        self.stack.enter_context(patch.object(gateway.G, "group_dir", return_value=self.root / "group"))
        self.stack.enter_context(patch.object(gateway.G, "load_config", return_value={"revision": 1}))
        self.stack.enter_context(patch.object(gateway.G, "plugin_enabled", return_value=True))
        action = {"type": "routine.reply", "group_openid": self.group, "plugin_id": "reply", "config_revision": 1}
        self.dispatch = self.stack.enter_context(patch("core.plugins.dispatch", return_value={"actions": [action], "errors": []}))
        self.stack.enter_context(patch("core.action_runner.run_actions", return_value=[]))
        self.stack.enter_context(patch.object(gateway, "rd", side_effect=lambda p: "https://example.com/test" if str(p).endswith("url") else "fixture-key"))
        class Response(io.BytesIO):
            status = 202
        self.opener = Mock()
        self.opener.open.side_effect = lambda *args, **kwargs: Response(b"{}")
        self.stack.enter_context(patch.object(gateway.urllib.request, "build_opener", return_value=self.opener))
        first = {"msg_id": "first", "member_openid": "alice", "text_only": "这五个工具哪个适合我", "at_me": True}
        C.record_input(self.store, self.scope, first, reply_policy.event_context(first))
        decision = C.parse_decision({"action": "clarify", "intent": "request", "question": "哪类场景？", "options": ["后台", "官网"]})
        turn = C.prepare(self.store, self.scope, "first", decision)
        self.question = turn["question_id"]
        self.store.reserve_action("ask", self.scope, conversation_turn=turn)
        self.store.action_status("ask", "sent")

    def event(self, source="answer", **extra):
        return {"post_type": "message", "group_openid": self.group, "msg_id": source,
                "member_openid": "alice", "text_only": "第二个", "at_me": False,
                "features": {"at_reply": True, "natural_reply": False, "buttons": True}, **extra}

    def test_pending_reply_reaches_model_without_at_or_keywords(self):
        event = self.event()
        tokens = reply_policy.topic_tokens(event["text_only"])
        self.store.admit_natural(self.group, reply_policy.digest(tokens), tokens, 12)
        self.assertEqual(gateway.forward(event), "accepted")
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["dialogue"]["pending"]["id"], self.question)
        self.assertEqual(payload["context"]["messages"][-1]["actor"], "alice")
        self.assertEqual(ReplyPlugin().on_event(payload).actions[0]["mode"], "continuation_candidate")

    def test_group_and_reply_feature_disable_still_gate_continuations(self):
        for extra in ({"group_enabled": False}, {"features": {"at_reply": False, "natural_reply": True}}):
            self.assertEqual(gateway.forward(self.event(**extra)), "skipped")
        self.opener.open.assert_not_called()

    def test_callback_is_scoped_and_its_choice_enters_memory(self):
        data = f"clarify:{self.question}:1"
        bad = self.event("", post_type="interaction", event_id="bad", member_openid="bob", button_data=data)
        self.assertEqual(gateway.forward(bad), "skipped")
        good = self.event("", post_type="interaction", event_id="click", button_data=data)
        self.assertEqual(gateway.forward(good), "accepted")
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["text"], "官网")
        self.assertEqual(payload["reply_context"]["source_event_id"], "click")
        self.assertEqual(MemoryStore(self.root).messages(self.group)[-1]["text"], "官网")

    def test_foreign_user_message_cannot_receive_pending_context(self):
        self.assertEqual(gateway.forward(self.event(member_openid="bob")), "skipped")
        self.assertIsNone(self.dispatch.call_args.args[0]["dialogue"]["pending"])
