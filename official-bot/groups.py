#!/usr/bin/env python3
"""Validated group configuration, atomic edits, and scoped plugin policy."""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path
from core.files import atomic_write_json, atomic_write_text, confined_path, file_lock
from core.settings import DATA_DIR

DIR = DATA_DIR
GROUPS = DIR / "groups"
DEFAULT_DIR = GROUPS / "_default"
C2C_DIR = DIR / "c2c"
SYSTEM_DIR = DIR / "_system"
OPENID_RE = re.compile(r"^[0-9A-F]{32}$")
PLUGIN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
BUILTIN_PLUGINS = ("command", "kb", "sticker", "digest", "reply", "natural_reply")
_LAST_GOOD: dict[str, dict] = {}

FALLBACK_DEFAULT = {
    "name": "", "aliases": [], "group_openid": "", "qq_group_id": None,
    "enabled": True, "revision": 0,
    "features": {"natural_reply": False, "at_reply": True, "daily_ai_news": False,
                 "x_info_push": False, "image_gen": False, "buttons": False,
                 "welcome": False, "media_understanding": True, "knowledge": True},
    "style": "礼貌、简洁，用中文回答；只在被 @ 时回复。",
    "admins": [], "knowledge": {"docs_dir": "knowledge/", "repos": []},
    "auto_created": True, "plugin_bindings": {}, "quotas": {"max_actions_per_day": 500},
}


class ConfigError(ValueError):
    pass


class ConfigConflict(ConfigError):
    pass


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read valid configuration: {path.name}") from exc


def _validate_quota(quota: dict) -> None:
    if not isinstance(quota, dict):
        raise ConfigError("Quota must be an object")
    for value in quota.values():
        if type(value) not in (int, float) or value < 0 or value != value or value == float("inf"):
            raise ConfigError("Quota values must be finite nonnegative numbers")


def validate_config(raw: dict) -> None:
    if not isinstance(raw, dict):
        raise ConfigError("Group configuration must be an object")
    for key in ("enabled", "auto_created"):
        if key in raw and type(raw[key]) is not bool:
            raise ConfigError(f"{key} must be boolean")
    for key in ("name", "style", "group_openid", "created_at", "added_by"):
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"{key} must be text")
    for key in ("aliases", "admins", "enabled_plugins"):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ConfigError(f"{key} must be a list of nonempty strings")
        if len(value) != len(set(x.casefold() for x in value)):
            raise ConfigError(f"{key} contains duplicates")
    for plugin in raw.get("enabled_plugins", []):
        if not PLUGIN_RE.fullmatch(plugin):
            raise ConfigError("Invalid plugin ID")
    if type(raw.get("revision", 0)) is not int or raw.get("revision", 0) < 0:
        raise ConfigError("revision must be a nonnegative integer")
    if raw.get("qq_group_id") is not None and (type(raw["qq_group_id"]) is not int or raw["qq_group_id"] <= 0):
        raise ConfigError("qq_group_id must be a positive integer or null")
    features = raw.get("features", {})
    if not isinstance(features, dict) or any(type(v) is not bool for v in features.values()):
        raise ConfigError("features must contain boolean values")
    bindings = raw.get("plugin_bindings", {})
    if not isinstance(bindings, dict):
        raise ConfigError("plugin_bindings must be an object")
    for plugin, binding in bindings.items():
        if not PLUGIN_RE.fullmatch(plugin) or not isinstance(binding, dict):
            raise ConfigError("Invalid plugin binding")
        if "enabled" in binding and type(binding["enabled"]) is not bool:
            raise ConfigError("Plugin enabled must be boolean")
        if not isinstance(binding.get("config", {}), dict):
            raise ConfigError("Plugin config must be an object")
        permissions = binding.get("permissions", [])
        if not isinstance(permissions, list) or any(not isinstance(p, str) for p in permissions):
            raise ConfigError("Plugin permissions must be a string list")
        _validate_quota(binding.get("quota", {}))
    _validate_quota(raw.get("quotas", {}))
    knowledge = raw.get("knowledge", {})
    if not isinstance(knowledge, dict) or not isinstance(knowledge.get("repos", []), list):
        raise ConfigError("knowledge must contain a repository list")
    if "docs_dir" in knowledge and not isinstance(knowledge["docs_dir"], str):
        raise ConfigError("knowledge.docs_dir must be text")
    for repo in knowledge.get("repos", []):
        if not isinstance(repo, dict) or not isinstance(repo.get("url"), str):
            raise ConfigError("Each repository needs a URL")


def _merged(raw: dict) -> dict:
    cfg = copy.deepcopy(FALLBACK_DEFAULT)
    for key, value in raw.items():
        if key in ("features", "plugin_bindings", "quotas"):
            cfg[key].update(value)
        elif not key.startswith("_"):
            cfg[key] = copy.deepcopy(value)
    cfg.setdefault("enabled_plugins", list(BUILTIN_PLUGINS))
    return cfg


def default_config() -> dict:
    raw = _read_json(DEFAULT_DIR / "config.json", {})
    validate_config(raw)
    return _merged(raw)


def group_dir(goid: str) -> Path:
    if not isinstance(goid, str) or not OPENID_RE.fullmatch(goid.upper()):
        raise ConfigError("Group ID must be 32 hexadecimal characters")
    return confined_path(GROUPS, goid.upper())


def load_config(goid: str) -> dict | None:
    path = group_dir(goid) / "config.json"
    if not path.exists() and not path.parent.exists():
        return None
    key = str(path)
    try:
        if not path.exists():
            raise ConfigError("Existing group configuration is missing")
        raw = _read_json(path)
        validate_config(raw)
        cfg = default_config()
        cfg.update({k: copy.deepcopy(v) for k, v in raw.items() if k not in ("features", "plugin_bindings", "quotas")})
        for field in ("features", "plugin_bindings", "quotas"):
            cfg[field].update(raw.get(field, {}))
        cfg["group_openid"] = goid.upper()
        cfg["auto_created"] = raw.get("auto_created", False)
        validate_config(cfg)
        _LAST_GOOD[key] = copy.deepcopy(cfg)
        return cfg
    except ConfigError as exc:
        cfg = copy.deepcopy(_LAST_GOOD.get(key, _merged({})))
        cfg.update(group_openid=goid.upper(), enabled=False, config_error=str(exc))
        return cfg


def _assert_aliases(goid: str, cfg: dict) -> None:
    own = {x.casefold() for x in cfg.get("aliases", [])}
    if cfg.get("name"):
        own.add(cfg["name"].casefold())
    for existing in all_groups():
        if existing["group_openid"] == goid:
            continue
        other = {x.casefold() for x in existing.get("aliases", [])}
        if existing.get("name"):
            other.add(existing["name"].casefold())
        if own & other:
            raise ConfigConflict("Group name or alias already belongs to another group")


def _write_registry() -> None:
    # Derived index, never a second configuration authority.
    atomic_write_json(GROUPS / "registry.json", {
        "schema_version": 1,
        "groups": [{"group_openid": c["group_openid"], "name": c.get("name", ""),
                    "aliases": c.get("aliases", []), "revision": c["revision"]} for c in all_groups()],
    })


def ensure_group(goid: str, name: str = "", extra: dict | None = None) -> tuple[dict, bool]:
    gd = group_dir(goid)
    with file_lock(GROUPS / ".registry.lock"):
        cfg = load_config(goid)
        if cfg is not None:
            return cfg, False
        cfg = default_config()
        cfg.update(group_openid=goid.upper(), name=name or f"未命名群-{goid[:6]}",
                   auto_created=True, created_at=time.strftime("%Y-%m-%d %H:%M:%S"), revision=1)
        cfg.update(extra or {})
        cfg["group_openid"] = goid.upper()
        cfg["revision"] = 1
        validate_config(cfg)
        _assert_aliases(goid.upper(), cfg)
        (gd / "knowledge").mkdir(parents=True, exist_ok=True)
        atomic_write_json(gd / "config.json", cfg)
        if not (gd / "prompt.md").exists() and (DEFAULT_DIR / "prompt.md").exists():
            atomic_write_text(gd / "prompt.md", (DEFAULT_DIR / "prompt.md").read_text(encoding="utf-8"))
        if not (gd / "notes.md").exists():
            atomic_write_text(gd / "notes.md", f"# {cfg['name']} 群笔记\n\n（自动创建；记录本群事实与约定）\n")
        _write_registry()
        return load_config(goid), True


def update_config(goid: str, expected_revision: int | None = None, **fields) -> dict:
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise ConfigError("Expected revision must be a nonnegative integer")
    with file_lock(GROUPS / ".registry.lock"):
        cfg = load_config(goid)
        if cfg is None or cfg.get("config_error"):
            raise ConfigError("Cannot update missing or invalid group configuration")
        if expected_revision is not None and cfg["revision"] != expected_revision:
            raise ConfigConflict("Configuration changed; reload before saving")
        if "group_openid" in fields and fields["group_openid"] != goid.upper():
            raise ConfigError("Group identity cannot be changed")
        if "revision" in fields or "config_error" in fields:
            raise ConfigError("Revision and diagnostics are managed fields")
        cfg.update(copy.deepcopy(fields))
        cfg["revision"] += 1
        validate_config(cfg)
        _assert_aliases(goid.upper(), cfg)
        atomic_write_json(group_dir(goid) / "config.json", cfg)
        _write_registry()
        return load_config(goid)


def all_groups() -> list[dict]:
    result = []
    if GROUPS.exists():
        for directory in sorted(GROUPS.iterdir()):
            if directory.is_dir() and OPENID_RE.fullmatch(directory.name):
                cfg = load_config(directory.name)
                if cfg:
                    result.append(cfg)
    return result


def default_group_openid() -> str:
    value = (DIR / "group_openid.txt").read_text(encoding="utf-8").strip().upper()
    group_dir(value)
    return value


def resolve(ref: str | None) -> str:
    if not ref:
        return default_group_openid()
    value = ref.strip()
    if OPENID_RE.fullmatch(value.upper()):
        return value.upper()
    matches = []
    for cfg in all_groups():
        names = [x.casefold() for x in cfg.get("aliases", [])] + [cfg.get("name", "").casefold()]
        if value.casefold() in names or (cfg.get("qq_group_id") and str(cfg["qq_group_id"]) == value):
            matches.append(cfg["group_openid"])
    if len(matches) == 1:
        return matches[0]
    raise ConfigError("Ambiguous group alias" if matches else f"Unknown group: {value!r}")


def plugin_enabled(goid: str, plugin_id: str) -> bool:
    cfg = load_config(goid)
    if not cfg or not cfg["enabled"] or plugin_id not in cfg["enabled_plugins"]:
        return False
    if not cfg["plugin_bindings"].get(plugin_id, {}).get("enabled", True):
        return False
    features = cfg["features"]
    gates = {"kb": "knowledge", "sticker": "image_gen", "reply": "at_reply", "natural_reply": "natural_reply"}
    if plugin_id == "digest":
        return features.get("daily_ai_news", False) or features.get("x_info_push", False)
    return features.get(gates[plugin_id], False) if plugin_id in gates else True


def update_plugin_binding(goid: str, plugin_id: str, binding: dict, expected_revision: int | None = None) -> dict:
    if not PLUGIN_RE.fullmatch(plugin_id):
        raise ConfigError("Invalid plugin ID")
    with file_lock(GROUPS / ".registry.lock"):
        cfg = load_config(goid)
        if cfg is None:
            raise ConfigError("Unknown group")
        bindings = copy.deepcopy(cfg["plugin_bindings"])
        bindings[plugin_id] = copy.deepcopy(binding)
        enabled = list(cfg["enabled_plugins"])
        if binding.get("enabled", True) and plugin_id not in enabled:
            enabled.append(plugin_id)
        if not binding.get("enabled", True) and plugin_id in enabled:
            enabled.remove(plugin_id)
        features = dict(cfg["features"])
        feature = {"kb": "knowledge", "sticker": "image_gen", "reply": "at_reply", "natural_reply": "natural_reply"}.get(plugin_id)
        if feature and "enabled" in binding:
            features[feature] = binding["enabled"]
        return update_config(goid, expected_revision, plugin_bindings=bindings, enabled_plugins=enabled, features=features)


def set_plugin_enabled(goid: str, plugin_id: str, enabled: bool, expected_revision: int | None = None) -> dict:
    with file_lock(GROUPS / ".registry.lock"):
        cfg = load_config(goid)
        if cfg is None:
            raise ConfigError("Unknown group")
        binding = copy.deepcopy(cfg["plugin_bindings"].get(plugin_id, {}))
        binding["enabled"] = enabled
        return update_plugin_binding(goid, plugin_id, binding, expected_revision)


def authorize(goid: str, plugin_id: str, action: str, actor: str = "", *, owner: bool = False) -> None:
    cfg = load_config(goid)
    if not cfg or not plugin_enabled(goid, plugin_id):
        raise PermissionError("Group or plugin is disabled")
    privileged = action in {"config.write", "memory.write", "memory.delete", "knowledge.write", "plugins.write"}
    if privileged and not owner and actor not in cfg["admins"]:
        raise PermissionError("This action requires a group administrator")
    allowed = cfg["plugin_bindings"].get(plugin_id, {}).get("permissions")
    if allowed is not None and action not in allowed:
        raise PermissionError("Plugin capability is not granted")
