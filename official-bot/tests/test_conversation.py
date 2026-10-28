import concurrent.futures
import json
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import conversation as C, reply_policy
from core.delivery import DeliveryStore, ReplySuppressed


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DeliveryStore(Path(self.temp.name) / "runtime.sqlite3")
        self.scope = "/v2/groups/test-group"

    def incoming(self, source, text="对比这五个工具", actor="alice", scope=None, **extra):
        event = {"post_type": "message", "msg_id": source, "member_openid": actor,
                 "text_only": text, "at_me": True, **extra}
        return C.record_input(self.store, scope or self.scope, event, reply_policy.event_context(event))

    def decision(self, action="clarify", parent="", **extra):
        raw = {"action": action, "intent": "request", "conversation_id": parent,
               **({"question": "主要做哪类界面？", "options": ["后台", "官网"], "known": "比较原来的五个工具"}
                  if action == "clarify" else {"text": "按后台场景继续比较"}), **extra}
        return C.parse_decision(raw)

    def reserve(self, source, action, decision):
        turn = C.prepare(self.store, self.scope, source, decision)
        meta = reply_policy.metadata("continuation" if decision["conversation_id"] else "at", source, C.render(decision), [])
        return self.store.reserve_action(action, self.scope, action, reply_meta=meta,
                                        limits=reply_policy.settings(), conversation_turn=turn)

    def ask(self, source="first", action="ask", parent=""):
        self.incoming(source)
        decision = self.decision(parent=parent)
        self.reserve(source, action, decision)
        self.store.action_status(action, "sent")
        return C.question_id(self.scope, source, decision)

    def test_text_continuation_preserves_task_and_consumes_only_after_send(self):
        identifier = self.ask()
        pending = self.incoming("second", "第二个，偏 Ant Design", at_me=False)["pending"]
        self.assertEqual(pending["id"], identifier)
        self.assertEqual(pending["request"], "对比这五个工具")
        self.assertEqual(pending["known"], "比较原来的五个工具")
        answer = self.decision("answer", identifier)
        self.reserve("second", "answer", answer)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM conversation_questions").fetchone()[0], "waiting")
        self.store.action_status("answer", "sent")
        self.assertIsNone(self.incoming("third")["pending"])
        self.assertFalse(self.reserve("second", "answer", answer)[0])

    def test_scope_and_actor_binding(self):
        identifier = self.ask()
        self.assertIsNone(self.incoming("bob", actor="bob")["pending"])
        self.assertIsNone(self.incoming("other-group", scope="/v2/groups/other")["pending"])
        with self.assertRaises(ValueError):
            C.prepare(self.store, self.scope, "bob", self.decision("answer", identifier))

    def test_button_owner_expiry_and_closed_question(self):
        identifier = self.ask()
        data = f"clarify:{identifier}:1"
        self.assertFalse(self.incoming("foreign", actor="bob", button_data=data)["valid"])
        valid = self.incoming("click", button_data=data)
        self.assertEqual(valid["pending"]["id"], identifier)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT text FROM conversation_inputs WHERE event_id='click'").fetchone()[0], "官网")
            db.execute("UPDATE conversation_questions SET expires=0")
        self.assertFalse(self.incoming("expired", button_data=data)["valid"])

    def test_old_event_is_not_rebound_to_new_question(self):
        self.incoming("old")
        self.ask()
        self.assertIsNone(self.incoming("old")["pending"])

    def test_unknown_send_never_opens_question_or_auto_replays(self):
        self.incoming("first")
        decision = self.decision()
        self.reserve("first", "ask", decision)
        self.store.action_status("ask", "unknown")
        self.assertIsNone(self.incoming("second")["pending"])
        self.assertFalse(self.reserve("first", "ask", decision)[0])

    def test_definite_answer_failure_releases_question_claim(self):
        identifier = self.ask()
        self.incoming("answer")
        self.reserve("answer", "failed-answer", self.decision("answer", identifier))
        self.store.action_status("failed-answer", "failed")
        self.assertEqual(self.incoming("next")["pending"]["id"], identifier)

    def test_concurrent_different_sources_cannot_consume_same_question(self):
        identifier = self.ask()
        for source in ("one", "two"):
            self.incoming(source)
        decision = self.decision("answer", identifier)
        def worker(source):
            try:
                return self.reserve(source, "answer-" + source, decision)[0]
            except ReplySuppressed:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ("one", "two")))
        self.assertEqual(sum(results), 1)

    def test_round_limit_rolls_back_parent_claim_and_outbox(self):
        first = self.ask()
        second = self.ask("second", "ask-again", first)
        pending = self.incoming("third")["pending"]
        self.assertEqual(pending["history"][0]["answer"], "对比这五个工具")
        self.assertEqual(pending["history"][0]["question"], "主要做哪类界面？")
        with self.assertRaises(ReplySuppressed):
            self.reserve("third", "too-many", self.decision(parent=second))
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT answer_action FROM conversation_questions WHERE id=?", (second,)).fetchone()[0])
            self.assertIsNone(db.execute("SELECT 1 FROM outbox WHERE id='too-many'").fetchone())

    def test_current_input_cannot_be_used_as_reply_to_another_question(self):
        identifier = self.ask()
        self.incoming("other", actor="bob")
        with self.assertRaises(ValueError):
            self.reserve("other", "wrong-user", self.decision("answer", identifier))
        with self.assertRaises(ValueError):
            self.reserve("missing", "invented", self.decision())

    def test_expired_question_rejected_even_if_model_kept_snapshot(self):
        identifier = self.ask()
        self.incoming("answer")
        with self.store.connect() as db:
            db.execute("UPDATE conversation_questions SET expires=0")
        with self.assertRaises(ReplySuppressed):
            self.reserve("answer", "stale", self.decision("answer", identifier))

    def test_malformed_decisions_are_not_sent(self):
        for raw in ({"action": []}, {"action": "answer", "intent": []},
                    {"action": "clarify", "intent": "request"},
                    {"action": "answer", "text": "no semantic intent"},
                    {"action": "answer", "intent": "request", "text": "ok", "force": True},
                    {"action": "clarify", "intent": "request", "question": "?", "options": ["a"] * 5}):
            with self.assertRaises(ValueError):
                C.parse_decision(raw)
        self.assertEqual(C.parse_decision({"action": "ignore"})["action"], "ignore")

    def test_open_question_does_not_add_unnecessary_choice_instructions(self):
        decision = C.parse_decision({"action": "clarify", "intent": "request", "question": "预算大概多少？"})
        self.assertEqual(C.render(decision), "预算大概多少？")

    def test_second_root_question_does_not_replace_waiting_question(self):
        identifier = self.ask()
        self.incoming("other-task")
        with self.assertRaises(ReplySuppressed):
            self.reserve("other-task", "second-root", self.decision())
        self.assertEqual(self.incoming("check")["pending"]["id"], identifier)

    def test_explicit_task_switch_replaces_only_after_success(self):
        identifier = self.ask()
        self.incoming("new-task", "换个话题，准备一份骑行计划")
        decision = self.decision(parent=identifier, restart=True, known="新目标：骑行计划", question="每周可以骑几天？")
        turn = C.prepare(self.store, self.scope, "new-task", decision)
        self.assertEqual(turn["round"], 1)
        self.assertEqual(turn["request"], "换个话题，准备一份骑行计划")
        self.reserve("new-task", "new-question", decision)
        self.store.action_status("new-question", "sent")
        current = self.incoming("reply", "三天")["pending"]
        self.assertEqual(current["request"], turn["request"])
        self.assertNotEqual(current["id"], identifier)
