"""Trusted, in-process plugins returning scoped actions, never sending QQ directly.

Configuration revisions are applied between dispatches. Reload drains the current
synchronous hook before replacing instances; third-party untrusted Python must
not be loaded here (a capability interface is not a Python security sandbox).
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import groups as G
from core.files import atomic_write_json, file_lock


@dataclass(frozen=True)
class PluginContext:
    group_openid: str
    config: dict
    plugin_config: dict
    revision: int


@dataclass
class HookResult:
    actions: list[dict] = field(default_factory=list)
    handled: bool = False


class Plugin:
    id = ""
    priority = 100
    version = "1"

    def start(self, context: PluginContext) -> None:
        self.context = context

    def on_event(self, event: dict) -> HookResult:
        return HookResult()

    def stop(self) -> None:
        pass


class CommandPlugin(Plugin):
    id, priority = "command", 10

    def on_event(self, event: dict) -> HookResult:
        text = str(event.get("text") or event.get("raw_message") or "").strip()
        if not (text == "/记忆" or text.startswith("/记忆 ")):
            return HookResult()
        content = text[len("/记忆"):].strip()
        if not content or content in {"列表", "查看"}:
            return HookResult([{"type": "memory.list"}], True)
        if content.startswith("忘记 "):
            return HookResult([{"type": "memory.delete", "memory_id": content[3:].strip()}], True)
        return HookResult([{"type": "memory.add", "text": content}], True)


class KnowledgePlugin(Plugin):
    id, priority = "kb", 20

    def on_event(self, event: dict) -> HookResult:
        text = str(event.get("text") or event.get("raw_message") or "").strip()
        if text.startswith("/知识 "):
            return HookResult([{"type": "knowledge.search", "query": text[4:].strip()}], True)
        return HookResult()


class StickerPlugin(Plugin):
    id, priority = "sticker", 30

    def on_event(self, event: dict) -> HookResult:
        text = str(event.get("text") or event.get("raw_message") or "").strip()
        if text.startswith("/表情 "):
            return HookResult([{"type": "sticker.search", "query": text[4:].strip()}], True)
        return HookResult()


class DigestPlugin(Plugin):
    id, priority = "digest", 40

    def on_event(self, event: dict) -> HookResult:
        feature = event.get("feature")
        if event.get("post_type") == "schedule" and feature in {"daily_ai_news", "x_info_push"}:
            if self.context.config["features"].get(feature, False):
                return HookResult([{"type": "routine.digest", "feature": feature}], True)
        return HookResult()


class ReplyPlugin(Plugin):
    id, priority = "reply", 100

    def on_event(self, event: dict) -> HookResult:
        if (event.get("dialogue") or {}).get("route_plugin") == self.id:
            return HookResult([{"type": "routine.reply", "mode": "continuation_candidate"}], True)
        if event.get("post_type") == "message" and event.get("at_me"):
            return HookResult([{"type": "routine.reply", "mode": "at"}], True)
        return HookResult()


class NaturalReplyPlugin(Plugin):
    id, priority = "natural_reply", 110

    def on_event(self, event: dict) -> HookResult:
        if (event.get("dialogue") or {}).get("route_plugin") == self.id:
            return HookResult([{"type": "routine.reply", "mode": "continuation_candidate"}], True)
        if event.get("post_type") == "message" and not event.get("at_me"):
            return HookResult([{"type": "routine.reply", "mode": "natural"}], True)
        return HookResult()


def reserve_quota(goid: str, plugin_id: str, event_id: str = "", *, now: float | None = None) -> bool:
    """Reserve actions atomically across workers; repeated event IDs cost once."""
    cfg = G.load_config(goid)
    if not cfg or not G.plugin_enabled(goid, plugin_id):
        return False
    date = time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))
    directory = G.group_dir(goid)
    with file_lock(directory / ".quota.lock"):
        path = directory / "plugin-usage.json"
        try:
            usage = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            usage = {}
        except (OSError, ValueError):
            return False
        if usage.get("date") != date:
            usage = {"date": date, "total": 0, "plugins": {}, "events": []}
        key = hashlib.sha256(f"{plugin_id}:{event_id}".encode()).hexdigest() if event_id else ""
        if key and key in usage["events"]:
            return True
        group_limit = cfg.get("quotas", {}).get("max_actions_per_day")
        limit = cfg["plugin_bindings"].get(plugin_id, {}).get("quota", {}).get("max_actions_per_day")
        count = usage["plugins"].get(plugin_id, 0)
        if (group_limit is not None and usage["total"] >= group_limit) or (limit is not None and count >= limit):
            return False
        usage["total"] += 1
        usage["plugins"][plugin_id] = count + 1
        if key:
            usage["events"] = (usage["events"] + [key])[-2000:]
        atomic_write_json(path, usage)
        return True


class PluginRegistry:
    def __init__(self, *, builtins: bool = True):
        self._factories: dict[str, Callable[[], Plugin]] = {}
        self._instances: dict[tuple[str, str], tuple[str, Plugin]] = {}
        self._lock = threading.RLock()
        if builtins:
            for plugin in (CommandPlugin, KnowledgePlugin, StickerPlugin, DigestPlugin, ReplyPlugin, NaturalReplyPlugin):
                self.register(plugin)

    def register(self, factory: Callable[[], Plugin]) -> None:
        plugin = factory()
        if not G.PLUGIN_RE.fullmatch(plugin.id):
            raise ValueError("Invalid plugin ID")
        with self._lock:
            if plugin.id in self._factories:
                raise ValueError("Plugin already registered; use reload")
            self._factories[plugin.id] = factory

    def reload(self, plugin_id: str, factory: Callable[[], Plugin]) -> None:
        replacement = factory()
        if replacement.id != plugin_id:
            raise ValueError("Plugin identity cannot change during reload")
        with self._lock:
            self._stop_instances(plugin_id)
            self._factories[plugin_id] = factory

    def _stop_instances(self, plugin_id: str | None = None) -> None:
        for key in list(self._instances):
            if plugin_id is None or key[1] == plugin_id:
                _, instance = self._instances.pop(key)
                instance.stop()

    def stop(self) -> None:
        with self._lock:
            self._stop_instances()

    def list_plugins(self) -> list[dict]:
        with self._lock:
            return [{"id": plugin.id, "version": plugin.version, "priority": plugin.priority,
                     "trusted_only": True} for factory in self._factories.values() for plugin in [factory()]]

    def dispatch(self, event: dict) -> dict:
        goid = event.get("group_openid") or ""
        if not goid:
            return {"handled": False, "actions": [], "errors": []}
        cfg = G.load_config(goid)
        if not cfg or not cfg["enabled"]:
            with self._lock:
                for key in list(self._instances):
                    if key[0] == goid:
                        self._instances.pop(key)[1].stop()
            return {"handled": True, "actions": [], "errors": ["group_disabled"]}
        text = str(event.get("text") or event.get("raw_message") or "").strip()
        reserved = {"/记忆": "command", "/知识": "kb", "/表情": "sticker"}
        route = reserved.get(text.split(maxsplit=1)[0] if text else "") or (event.get("dialogue") or {}).get("route_plugin")
        if route and not G.plugin_enabled(goid, route):
            return {"handled": True, "actions": [], "errors": ["plugin_disabled"]}
        errors = []
        with self._lock:
            plugins = sorted((factory() for factory in self._factories.values()), key=lambda p: (p.priority, p.id))
            for prototype in plugins:
                key = (goid, prototype.id)
                if not G.plugin_enabled(goid, prototype.id):
                    old = self._instances.pop(key, None)
                    if old:
                        old[1].stop()
                    continue
                signature = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
                current = self._instances.get(key)
                try:
                    if current is None or current[0] != signature:
                        if current:
                            self._instances.pop(key)[1].stop()
                        context = PluginContext(goid, copy.deepcopy(cfg),
                            copy.deepcopy(cfg["plugin_bindings"].get(prototype.id, {}).get("config", {})), cfg["revision"])
                        prototype.start(context)
                        self._instances[key] = (signature, prototype)
                    instance = self._instances[key][1]
                    result = instance.on_event(copy.deepcopy(event))
                    if not result.handled and not result.actions:
                        continue
                    actions = []
                    for action in result.actions:
                        kind = action.get("type", "")
                        permission = {"memory.add": "memory.write", "memory.delete": "memory.delete"}.get(kind, kind)
                        G.authorize(goid, prototype.id, permission, str(event.get("member_openid") or ""))
                        actions.append({**action, "group_openid": goid, "plugin_id": prototype.id,
                                        "config_revision": cfg["revision"]})
                    if actions and not reserve_quota(goid, prototype.id, str(event.get("event_id") or event.get("msg_id") or "")):
                        return {"handled": True, "actions": [], "errors": errors + ["quota_exceeded"]}
                    # One owner per event; commands cannot also trigger generic replies.
                    return {"handled": True, "actions": actions, "errors": errors}
                except Exception as exc:
                    errors.append(f"{prototype.id}:{type(exc).__name__}")
                    # A rejected command must not fall through to an unrestricted routine.
                    return {"handled": True, "actions": [], "errors": errors}
        return {"handled": False, "actions": [], "errors": errors}


default_registry = PluginRegistry()


def dispatch(event: dict) -> dict:
    return default_registry.dispatch(event)
