"""Execute registry-authorized local actions with explicit scope and outcome."""
from __future__ import annotations

import groups as G
from .memory import MemoryStore
from .settings import DATA_DIR


def run_actions(event: dict, actions: list[dict]) -> list[dict]:
    store = MemoryStore(DATA_DIR)
    results = []
    for ordinal, action in enumerate(actions):
        kind = action["type"]
        scope = action["group_openid"]
        if scope != event.get("group_openid"):
            raise ValueError("Action scope differs from event scope")
        permission = {"memory.add": "memory.write", "memory.delete": "memory.delete"}.get(kind, kind)
        G.authorize(scope, action["plugin_id"], permission, str(event.get("member_openid") or ""))
        cfg = G.load_config(scope)
        if not cfg or cfg["revision"] != action["config_revision"]:
            raise ValueError("Configuration changed before action execution")
        if kind.startswith("routine."):
            continue
        if kind == "memory.list":
            value = store.search_memories(scope, limit=100)
        elif kind == "memory.add":
            operation = str(event.get("delivery_id") or event.get("msg_id") or event.get("event_id") or "")
            if not operation:
                raise ValueError("Memory mutation requires a stable event identity")
            value = store.apply_command(scope, operation + ":" + str(ordinal), kind, text=action["text"], actor=str(event.get("member_openid") or ""))
        elif kind == "memory.delete":
            operation = str(event.get("delivery_id") or event.get("msg_id") or event.get("event_id") or "")
            if not operation:
                raise ValueError("Memory mutation requires a stable event identity")
            value = store.apply_command(scope, operation + ":" + str(ordinal), kind, memory_id=action["memory_id"], actor=str(event.get("member_openid") or ""))
        elif kind == "knowledge.search":
            import kb
            failures = kb.refresh_index(scope, cfg, store)
            value = {"hits": store.search_documents(scope, action["query"], limit=8), "index_errors": failures}
        elif kind == "sticker.search":
            import make_sticker
            value = make_sticker.search_candidates(action["query"], group=scope)
            value["recommendation"] = make_sticker.recommend_sticker(action["query"],
                context=str(event.get("text_only") or ""), seed=str(event.get("delivery_id") or event.get("msg_id") or ""), group=scope)
        elif kind in ("media.poster", "media.ppt"):
            from . import sensenova
            key = ":".join([scope, str(event.get("msg_id") or event.get("event_id") or event.get("delivery_id") or ""), str(ordinal)])
            args = {k: action[k] for k in ("prompt", "topic", "slides", "extra", "name") if k in action}
            value = sensenova.start_job(kind.split(".", 1)[1], args, key)
        elif kind == "coffee.step":
            from . import luckin
            value = luckin.start_step(scope, event)
        elif kind == "amap.query":
            from . import amap
            value = amap.start_query(scope, event)
        elif kind == "media.usage":
            value = {"usage": "/海报 <活动/需求描述>" if action.get("kind") == "poster" else "/PPT <主题> [N页]"}
        else:
            raise ValueError("Unknown local action")
        results.append({"type": kind, "ok": True, "result": value})
    return results
