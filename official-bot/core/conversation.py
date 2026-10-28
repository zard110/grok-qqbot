"""Short-lived clarification state; the model decides semantics, SQLite owns claims.

Questions and inbound candidates use the delivery database so a continuation and
its outbound action are reserved/finished in one transaction. No keyword matching.
"""
from __future__ import annotations

import hashlib
import json
import time

TTL = 900
MAX_ROUNDS = 2
POLICY = (
    "判断是否介入和是否追问由你结合语义与上下文决定，trigger 只是节奏提示。"
    "先检查上下文已有信息；只在缺失条件会实质改变结论时追问一个关键问题。"
    "可以合理假设就说明假设并先回答；未知客观事实应检索或承认不确定。"
    "pending 仅是同一提问人的待续任务，不证明新消息在回答它；转话题不要强行关联。"
    "明确延续才在 decision 中填写 pending.id，补充后继续原任务，不重复问已知条件。"
    "最多两轮澄清，用户没回答不催问；信息仍不足时说明限制，不猜关键事实。"
    "普通闲聊无必要则 ignore。选项可省略，最多四个，始终接受自由文字。"
    "普通问答使用 send_group.py --decision @JSON文件 --msg-id 当前msg_id；"
    "按钮回调用 --event-id 当前event_id。JSON: {action: answer|clarify|ignore, intent:request|casual,"
    " text:正文, question:追问(仅clarify), options:[可选项], known:已有条件摘要,"
    " conversation_id:明确延续的pending.id或空串, restart:是否明确换新任务(默认false)}。"
    "新任务需要追问且有旧pending时，填写旧id并restart:true，保留新需求。"
    "不将JSON发到群中，不自动--force。"
)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS conversation_inputs(
          scope TEXT NOT NULL,event_id TEXT NOT NULL,actor TEXT NOT NULL,
          text TEXT NOT NULL,reply_to TEXT NOT NULL,context TEXT NOT NULL,
          question_id TEXT NOT NULL DEFAULT '',created REAL NOT NULL,
          PRIMARY KEY(scope,event_id));
        CREATE INDEX IF NOT EXISTS conversation_input_time ON conversation_inputs(created);
        CREATE TABLE IF NOT EXISTS conversation_questions(
          id TEXT PRIMARY KEY,scope TEXT NOT NULL,actor TEXT NOT NULL,
          source_event TEXT NOT NULL,request TEXT NOT NULL,known TEXT NOT NULL,
          question TEXT NOT NULL,options TEXT NOT NULL,plugin_id TEXT NOT NULL,
          round INTEGER NOT NULL,send_action TEXT NOT NULL UNIQUE,
          answer_action TEXT,status TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
          history TEXT NOT NULL DEFAULT '[]');
        CREATE INDEX IF NOT EXISTS conversation_actor ON conversation_questions(scope,actor,status,expires);
        CREATE INDEX IF NOT EXISTS conversation_expiry ON conversation_questions(expires);
    """)
    db.execute("BEGIN IMMEDIATE")
    if "history" not in {row[1] for row in db.execute("PRAGMA table_info(conversation_questions)")}:
        db.execute("ALTER TABLE conversation_questions ADD COLUMN history TEXT NOT NULL DEFAULT '[]'")
    db.commit()


def _string(value, maximum, *, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError("Invalid or oversized conversation text")
    return value.strip()


def parse_decision(raw):
    if not isinstance(raw, dict) or set(raw) - {"action", "intent", "text", "question", "options", "known", "conversation_id", "restart"}:
        raise ValueError("Invalid decision fields")
    action = raw.get("action")
    if not isinstance(action, str) or action not in {"answer", "clarify", "ignore"}:
        raise ValueError("decision.action must be answer, clarify or ignore")
    intent = raw.get("intent", "casual" if action == "ignore" else None)
    if not isinstance(intent, str) or intent not in {"request", "casual"}:
        raise ValueError("decision.intent must be request or casual (semantic model judgment)")
    result = {"action": action, "intent": intent, "text": _string(raw.get("text", ""), 6000, required=action == "answer"),
              "question": _string(raw.get("question", ""), 300, required=action == "clarify"),
              "known": _string(raw.get("known", ""), 1200),
              "conversation_id": _string(raw.get("conversation_id", ""), 64)}
    options = raw.get("options", [])
    if not isinstance(options, list) or len(options) > 4:
        raise ValueError("Use at most four choices")
    result["options"] = [_string(option, 40, required=True) for option in options]
    result["restart"] = raw.get("restart", False)
    if type(result["restart"]) is not bool or (result["restart"] and (not result["conversation_id"] or action != "clarify")):
        raise ValueError("restart requires clarify and the pending conversation_id")
    if len(set(result["options"])) != len(options):
        raise ValueError("Choices must be distinct")
    if action != "clarify" and (result["question"] or options):
        raise ValueError("Only clarification can contain question/options")
    return result


def question_id(scope, source, decision):
    return hashlib.sha256(json.dumps([scope, source, decision], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def render(decision):
    parts = [decision["text"]]
    if decision["action"] == "clarify":
        parts.append(decision["question"])
        parts.extend(f"{i + 1}. {option}" for i, option in enumerate(decision["options"]))
        if decision["options"]:
            parts.append("可以回复序号，也可以直接补充；还没确定也可以说。")
    return "\n".join(part for part in parts if part)


def keyboard(identifier, options):
    buttons = [{"id": f"clarify_{i}", "render_data": {"label": option, "visited_label": option, "style": 1},
                "action": {"type": 1, "permission": {"type": 2},
                           "data": f"clarify:{identifier}:{i}", "unsupport_tips": "请直接回复文字或序号"}}
               for i, option in enumerate(options)]
    return {"rows": [{"buttons": buttons[i:i + 2]} for i in range(0, len(buttons), 2)]}


def _public(row):
    return {key: (json.loads(row[key]) if key in {"options", "history"} else row[key]) for key in
            ("id", "request", "known", "question", "options", "history", "round", "expires", "plugin_id")}


def _pending(db, scope, actor, now):
    return db.execute("""SELECT * FROM conversation_questions WHERE scope=? AND actor=?
        AND status='waiting' AND answer_action IS NULL AND expires>? ORDER BY created DESC LIMIT 1""",
        (scope, actor, now)).fetchone()


def record_input(store, scope, event, context):
    """Persist actor/source binding before forwarding. Candidates are not answers."""
    actor = str(event.get("member_openid") or event.get("user_openid") or "")
    source = str(event.get("msg_id") or event.get("message_id") or event.get("event_id") or "")
    text = str(event.get("text_only") or event.get("raw_message") or "")[:8000]
    now = time.time()
    result = {"policy": POLICY, "pending": None, "route_plugin": "", "valid": True}
    if not actor or not source or event.get("from_bot"):
        return result
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM conversation_inputs WHERE created<?", (now - 86400,))
        db.execute("DELETE FROM conversation_questions WHERE expires<?", (now - 86400,))
        old = db.execute("SELECT * FROM conversation_inputs WHERE scope=? AND event_id=?", (scope, source)).fetchone()
        pending = _pending(db, scope, actor, now)
        data = str(event.get("button_data") or "")
        if data.startswith("clarify:"):
            try:
                _, identifier, choice = data.split(":")
                index = int(choice)
                if not pending or pending["id"] != identifier or not 0 <= index < len(json.loads(pending["options"])):
                    raise ValueError("Stale or foreign choice")
                text = json.loads(pending["options"])[index]
                event["text_only"] = text
            except (ValueError, TypeError):
                return {**result, "valid": False}
        if old:
            if old["actor"] != actor or old["text"] != text:
                raise ValueError("Inbound identity reused with different content")
            # Never rebind an old event to a newer question.
            pending = pending if pending and pending["id"] == old["question_id"] else None
        else:
            db.execute("INSERT INTO conversation_inputs VALUES(?,?,?,?,?,?,?,?)",
                       (scope, source, actor, text, str(event.get("reply_to") or "")[:300],
                        json.dumps(context, ensure_ascii=False), pending["id"] if pending else "", now))
        if pending:
            result.update(pending=_public(pending), route_plugin=pending["plugin_id"])
    return result


def prepare(store, scope, source, decision):
    """Read-only preview; reserve() repeats binding checks inside the transaction."""
    with store.connect() as db:
        incoming = db.execute("SELECT * FROM conversation_inputs WHERE scope=? AND event_id=?", (scope, source)).fetchone()
        if not incoming or incoming["created"] < time.time() - TTL:
            raise ValueError("Decision requires a recent gateway-recorded source event")
        context = json.loads(incoming["context"])
        plugin_id = "reply" if context.get("trigger") in {"at", "command"} else "natural_reply"
        parent = None
        if decision["conversation_id"]:
            parent = _parent(db, scope, incoming, decision["conversation_id"], time.time(), preview=True)
            plugin_id = parent["plugin_id"]
        continuing = parent is not None and not decision["restart"]
        history = (json.loads(parent["history"]) + [{"question": parent["question"],
                   "options": json.loads(parent["options"]), "answer": incoming["text"][:2000]}])[-MAX_ROUNDS:] if continuing else []
        return {"decision": decision, "source": source, "scope": scope, "actor": incoming["actor"],
                "request": parent["request"] if continuing else incoming["text"][:4000],
                "known": decision["known"] or (parent["known"] if continuing else ""),
                "round": parent["round"] + 1 if continuing else 1,
                "history": history,
                "plugin_id": plugin_id, "question_id": question_id(scope, source, decision),
                "context": context}


def _parent(db, scope, incoming, identifier, now, *, preview=False):
    parent = db.execute("SELECT * FROM conversation_questions WHERE id=? AND scope=? AND actor=?",
                        (identifier, scope, incoming["actor"])).fetchone()
    if (not parent or incoming["question_id"] != identifier
            or (not preview and (parent["status"] != "waiting" or parent["expires"] <= now))):
        raise ValueError("Question is expired, closed, or not bound to this user/event")
    return parent


def reserve(db, scope, action_id, turn, now):
    """Runs inside DeliveryStore.reserve_action's BEGIN IMMEDIATE."""
    decision = turn["decision"]
    incoming = db.execute("SELECT * FROM conversation_inputs WHERE scope=? AND event_id=?", (scope, turn["source"])).fetchone()
    if not incoming or incoming["actor"] != turn["actor"] or incoming["created"] < now - TTL:
        raise ValueError("Conversation input expired")
    if decision["conversation_id"]:
        parent = _parent(db, scope, incoming, decision["conversation_id"], now)
        if parent["answer_action"] and parent["answer_action"] != action_id:
            raise ValueError("Question already has a response in progress")
        db.execute("UPDATE conversation_questions SET answer_action=? WHERE id=?", (action_id, parent["id"]))
    if decision["action"] != "clarify":
        return
    if turn["round"] > MAX_ROUNDS:
        raise ValueError("Clarification round limit; answer with explicit limits or assumptions")
    busy = db.execute("""SELECT id FROM conversation_questions WHERE scope=? AND actor=?
        AND status IN ('draft','waiting','unknown') AND expires>? AND id!=? AND send_action!=? LIMIT 1""",
        (scope, turn["actor"], now, decision["conversation_id"], action_id)).fetchone()
    if busy:
        raise ValueError("A question is already pending for this user; continue it or wait for expiry")
    db.execute("""INSERT INTO conversation_questions
        (id,scope,actor,source_event,request,known,question,options,plugin_id,round,send_action,answer_action,status,created,expires,history)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,'draft',?,?,?)
        ON CONFLICT(id) DO UPDATE SET status='draft',expires=excluded.expires""",
        (turn["question_id"], scope, turn["actor"], turn["source"], turn["request"], turn["known"],
         decision["question"], json.dumps(decision["options"], ensure_ascii=False), turn["plugin_id"],
         turn["round"], action_id, now, now + TTL, json.dumps(turn["history"], ensure_ascii=False)))


def finish(db, action_id, status, now):
    """Question visibility and answer consumption follow confirmed send status."""
    if status not in {"sent", "failed", "partial", "unknown"}:
        return
    db.execute("UPDATE conversation_questions SET status=?,expires=? WHERE send_action=?",
               ("waiting" if status == "sent" else "failed" if status == "failed" else "unknown", now + TTL, action_id))
    if status == "sent":
        db.execute("UPDATE conversation_questions SET status='answered' WHERE answer_action=?", (action_id,))
    elif status == "failed":
        db.execute("UPDATE conversation_questions SET answer_action=NULL WHERE answer_action=?", (action_id,))
