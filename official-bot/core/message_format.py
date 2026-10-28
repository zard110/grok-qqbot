"""Conservative QQ presentation: capability-gated auto Markdown, bounded parts."""
from __future__ import annotations

import re

DEFAULTS = {"markdown_verified": False, "tables_verified": False,
            "fenced_code_verified": False, "max_chars": 1000, "max_parts": 3,
            "fallback_on_reject": True, "markdown_reject_codes": []}
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*\|[\s|:\-]*$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})([\w+-]*)\s*$")


def policy(config: dict | None = None) -> dict:
    result = {**DEFAULTS, **((config or {}).get("message_format") or {})}
    for key in ("markdown_verified", "tables_verified", "fenced_code_verified", "fallback_on_reject"):
        if type(result[key]) is not bool:
            raise ValueError(f"message_format.{key} must be boolean")
    for key, low, high in (("max_chars", 120, 2000), ("max_parts", 1, 5)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"message_format.{key} must be {low}..{high}")
    if not isinstance(result["markdown_reject_codes"], list):
        raise ValueError("markdown_reject_codes must be a list")
    return result


def plain_text(content: str) -> str:
    """Keep code and table values, replacing layout syntax with readable labels."""
    lines, output, inside = content.splitlines(), [], False
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = FENCE.match(line)
        if fence:
            if not inside:
                output.append("代码" + (f"（{fence[2]}）" if fence[2] else "") + "：")
            inside = not inside
            index += 1
            continue
        if inside:
            output.append("    " + line)
            index += 1
            continue
        if index + 1 < len(lines) and "|" in line and TABLE_RULE.match(lines[index + 1]):
            headers = [cell.strip() for cell in line.strip().strip("|").split("|")]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                output.append("；".join(f"{headers[n] if n < len(headers) else '补充'}：{cell}" for n, cell in enumerate(cells)))
                index += 1
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"图片：\1（\2）", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", line)
        line = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        output.append(line)
        index += 1
    return "\n".join(output).strip()


def split_content(content: str, limit: int, *, markdown: bool = False) -> list[str]:
    if len(content) <= limit:
        return [content]
    # Reserve room for part labels and fence reopen/close markers.
    budget, chunks, current, active = limit - 32, [], [], ""
    for line in content.splitlines(keepends=True):
        fragments = [line[n:n + budget - 16] for n in range(0, len(line), budget - 16)] or [""]
        for fragment in fragments:
            if current and sum(map(len, current)) + len(fragment) > budget:
                chunks.append("".join(current).rstrip() + ("\n" + FENCE.match(active)[1] if active else ""))
                current = [active + "\n"] if active else []
            current.append(fragment)
        if markdown and FENCE.match(line.strip()):
            active = "" if active else line.strip()
    if current:
        chunks.append("".join(current).rstrip() + ("\n" + FENCE.match(active)[1] if active else ""))
    return [f"[{n + 1}/{len(chunks)}]\n{text}" for n, text in enumerate(chunks)]


def prepare(content: str, requested: str, config=None, *, mode=None, max_chars=None, max_parts=None):
    settings = policy(config)
    if max_chars is not None:
        settings["max_chars"] = max_chars
    if max_parts is not None:
        settings["max_parts"] = max_parts
    settings = policy({"message_format": settings})
    warnings = []
    chosen = requested if mode is None else mode
    table = any(TABLE_RULE.match(line) for line in content.splitlines())
    code = any(FENCE.match(line) for line in content.splitlines())
    if chosen == "auto":
        supported = (settings["markdown_verified"] and (not table or settings["tables_verified"])
                     and (not code or settings["fenced_code_verified"]))
        chosen = "markdown" if supported else "plain"
        if not supported:
            warnings.append("Markdown capability unverified; using readable plain text")
    if table and len(content) > settings["max_chars"] and chosen == "markdown":
        chosen = "plain"
        warnings.append("Long table converted to labeled rows before splitting")
    if chosen == "plain" and (requested == "markdown" or mode == "auto"):
        content = plain_text(content)
    chunks = split_content(content, settings["max_chars"], markdown=chosen == "markdown")
    if len(chunks) > settings["max_parts"]:
        raise ValueError("Reply exceeds configured parts; summarize or explicitly increase --max-parts (up to 5)")
    return [{"msg_type": 2, "markdown": {"content": chunk}} if chosen == "markdown"
            else {"msg_type": 0, "content": chunk} for chunk in chunks], warnings


def may_fallback(result: dict, payload: dict, config=None) -> bool:
    settings = policy(config)
    content = payload.get("markdown", {}).get("content", "")
    if (not settings["fallback_on_reject"] or payload.get("msg_type") != 2
            or payload.get("keyboard") or "![" in content or "<qqbot-cmd" in content
            or len(plain_text(content)) > settings["max_chars"]
            or result.get("http_status") not in (400, 422)):
        return False
    body = result.get("body")
    if not isinstance(body, dict):
        return False
    if body.get("code") in settings["markdown_reject_codes"]:
        return True
    message = str(body.get("message", body.get("msg", ""))).lower()
    return "markdown" in message and any(reason in message for reason in
        ("not supported", "unsupported", "not enabled", "未开通", "不支持"))
