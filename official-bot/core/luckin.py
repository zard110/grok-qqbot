"""Luckin Coffee (瑞幸) pickup ordering for the ``luckin_coffee`` plugin.

Official MCP (https://open.lkcoffee.com/docs, skill ``@luckin/my-coffee``): Streamable HTTP JSON-RPC
at ``MCP_URL`` with ``Authorization: Bearer <token>``. Tools and fields used here (from the docs):

* queryShopList(longitude, latitude[, deptName]) -> [{deptId, deptName, address, longitude, latitude,
  workTimeStart, workTimeEnd, distance(km), number}]
* searchProductForMcp(deptId, query) -> [{productId, productName, skuCode, productAttrs, tags,
  initialPrice, estimatePrice}]
* queryProductDetailInfo(deptId, productId) / switchProduct(deptId, productId, skuCode,
  attrOperationParam{attributeId, subAttr{attributeId, operation=3}}, amount) -> product
* previewOrder(deptId, productList[{amount, productId, skuCode}]) -> {discountPrice, totalInitialPrice,
  privilegeMoney, couponCodeList, productInfoList[{name, additionDesc, skuCode, ...}], aboutTime, shopInfo}
* createOrder(deptId, productList, longitude, latitude[, couponCodeList]) -> {orderIdStr, orderId,
  payOrderQrCodeUrl, payOrderUrl, discountPrice, needPay}  (payOrderUrl is never shown, per the skill)
* queryOrderDetailInfo(orderId) -> {orderStatus 10 待付款/20 下单成功/30 制作中/60 等待取餐/80 已完成/
  100 已取消, orderStatusName, takeMealCodeInfo{code}, shopInfo, orderPayAmount, aboutTime}
* cancelOrder(orderId) -> true

Pickup only (到店自取). The token is read from ``LUCKIN_MCP_TOKEN`` or ``official-bot/luckin-token``
and is never printed, logged or sent. Every QQ message goes through send_group.py with
``--plugin-id luckin_coffee`` so a disabled plugin can never speak.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

import groups as G
from .files import atomic_write_json, file_lock
from .settings import DATA_DIR, CODE_DIR

PLUGIN_ID = "luckin_coffee"
MCP_URL = os.environ.get("LUCKIN_MCP_URL", "https://gwmcp.lkcoffee.com/order/user/mcp")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
AMAP_POI = "https://restapi.amap.com/v3/place/text"
SEND_CLI = CODE_DIR / "send_group.py"
WORKER_CLI = CODE_DIR / "luckin_coffee.py"
DEFAULTS = {
    "session_minutes": 10, "max_shops": 3, "products_per_search": 2, "max_options": 8,
    "default_queries": ["生椰拿铁", "拿铁", "美式"], "max_orders_per_member_per_hour": 2,
    "max_orders_per_group_per_day": 20, "pay_timeout_minutes": 15, "poll_seconds": 15,
    "allow_members": [], "order_members": [], "quick_buttons": True,
}
PAID = {20, 30, 60, 80}
CANCELLED = 100
CANCEL_WORDS = {"取消", "不要了", "算了", "不点了", "退出", "取消订单", "不要"}
CONFIRM_WORDS = {"确认", "确定", "确认下单", "下单", "好", "好的", "ok", "OK", "是", "嗯", "可以", "y", "yes"}
DRINK = r"(咖啡|瑞幸|luckin|拿铁|美式|生椰|橙C|摩卡|澳白|卡布奇诺|dirty|厚乳|丝绒|轻乳茶|果茶|柠檬茶|冰萃|浓缩|椰|抹茶)"
ORDER_VERB = r"(想喝|要喝|来一?杯|点一?杯|买一?杯|喝一?杯|下单|点单|请.{0,6}喝|附近|便宜|推荐|哪家|门店|比价|到手价)"
DELIVERY = re.compile(r"外送|外卖|送到|送达|配送|送过来|送上来")
LOCATION = re.compile(r"(?:我在|人在|在)?([\w\u4e00-\u9fff·()（）\-]{2,30}?)(?:附近|周边|旁边|这边|一带)")
COORD = re.compile(r"(-?\d{1,3}\.\d{2,})\s*[,，\s]\s*(-?\d{1,3}\.\d{2,})")
LOCATED = re.compile(r"(?:我|人)在([^，,。！!？?\s]{2,20}?)(?=[，,。！!？?\s]|想|要|来|喝|点|$)")
DRINKS = re.compile(r"(生椰拿铁|橙C美式|丝绒拿铁|厚乳拿铁|澳白|摩卡|卡布奇诺|拿铁|美式|dirty|轻乳茶|果茶|柠檬茶|冰萃|抹茶\w{0,3})", re.I)
PICKUP_STATUS = re.compile(r"取餐码|订单状态|做好了吗|查订单|好了没")
BLESSINGS = ["愿这杯咖啡给你满格电量，今天也元气满满 ☀️", "趁热（或趁冰）喝，祝你今天骑得顺、练得爽 🚴",
             "好咖啡配好心情，忙里偷闲也要好好犒劳自己 ☕", "喝完这杯，FTP 再涨一点点 💪",
             "愿你今天所有的努力都有回甘 🌿", "一杯下肚，烦恼清零，加油鸭 🦆"]


class LuckinError(Exception):
    def __init__(self, msg: str, kind: str = "api"):
        super().__init__(msg)
        self.kind = kind


# ------------------------------------------------------------------ token / MCP

def token() -> str:
    value = os.environ.get("LUCKIN_MCP_TOKEN", "").strip()
    if value:
        return value
    try:
        return (DATA_DIR / "luckin-token").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _parse_rpc(raw: str, content_type: str) -> dict:
    """Plain JSON or an SSE stream; returns the JSON-RPC message carrying a result/error."""
    text = raw.strip()
    if "text/event-stream" in content_type or text.startswith(("event:", "data:", "id:")):
        found = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and ("result" in message or "error" in message):
                    found = message
        if found is None:
            raise LuckinError("empty MCP stream")
        return found
    try:
        return json.loads(text)
    except ValueError:
        raise LuckinError("non-JSON MCP response") from None


def _unwrap(result: dict):
    """tools/call result -> business ``data`` (structuredContent or content[0].text JSON)."""
    if not isinstance(result, dict):
        raise LuckinError("bad MCP result")
    body = result.get("structuredContent")
    if body is None:
        texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        joined = "".join(texts).strip()
        try:
            body = json.loads(joined) if joined else None
        except ValueError:
            body = None
        if body is None:
            low = joined.lower()
            if "oauth" in low or "token" in low and ("expire" in low or "invalid" in low or "过期" in low):
                raise LuckinError("token rejected", "auth")
            raise LuckinError("MCP tool returned non-JSON text" + (" (error)" if result.get("isError") else ""))
    if result.get("isError") and not isinstance(body, dict):
        raise LuckinError("MCP tool error")
    if isinstance(body, dict) and ("code" in body or "success" in body):
        if body.get("success") is False or body.get("code") not in (0, "0", 200, None):
            msg = str(body.get("msg") or body.get("message") or "")
            raise LuckinError("Luckin error: " + msg[:120], "auth" if re.search(r"oauth|token|登录|授权", msg, re.I) else "api")
        return body.get("data")
    return body


def _body(r) -> str:
    """UTF-8 regardless of headers: SSE replies carry no charset and requests would fall back to Latin-1."""
    raw = getattr(r, "content", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode("utf-8", "replace")
    return getattr(r, "text", "") or ""


class MCPClient:
    def __init__(self, tok: str | None = None, url: str | None = None, *, http=None, timeout: float = 20):
        self.token = token() if tok is None else tok
        if not self.token:
            raise LuckinError("token missing", "auth")
        self.url, self.http, self.timeout = url or MCP_URL, http or requests, timeout
        self.session_id = ""
        self._id = 0

    def _post(self, body: dict) -> dict | None:
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            r = self.http.post(self.url, json=body, headers=headers, timeout=(10, self.timeout))
        except Exception:  # noqa: BLE001 - never surface URLs/headers
            raise LuckinError("Luckin MCP unreachable", "network") from None
        sid = (r.headers or {}).get("Mcp-Session-Id") or (r.headers or {}).get("mcp-session-id")
        if sid:
            self.session_id = sid
        if r.status_code in (401, 403):
            raise LuckinError("token rejected", "auth")
        if r.status_code == 202 or "id" not in body:
            return None
        if r.status_code >= 400:
            low = _body(r)[:300].lower()
            if "oauth" in low:
                raise LuckinError("token rejected", "auth")
            raise LuckinError(f"Luckin MCP HTTP {r.status_code}", "session" if "session" in low or "initializ" in low else "api")
        return _parse_rpc(_body(r), (r.headers or {}).get("Content-Type", ""))

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if msg.get("error"):
            err = msg["error"]
            text = str(err.get("message", ""))
            raise LuckinError("MCP error: " + text[:120], "auth" if "oauth" in text.lower() else "api")
        return msg.get("result") or {}

    def initialize(self) -> None:
        self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                 "clientInfo": {"name": "maxshenbi-qqbot", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, name: str, arguments: dict):
        try:
            return _unwrap(self._rpc("tools/call", {"name": name, "arguments": arguments}))
        except LuckinError as exc:
            if exc.kind != "session" or self.session_id:
                raise
        self.initialize()  # stateful gateway variant: handshake once, then retry
        return _unwrap(self._rpc("tools/call", {"name": name, "arguments": arguments}))

    def tools(self) -> list[str]:
        return [t.get("name") for t in self._rpc("tools/list", {}).get("tools", [])]


# ------------------------------------------------------------------ geocoding

def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """OSM/GPS (WGS-84) -> GCJ-02 used by mainland map/ordering apps; outside China unchanged."""
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return lng, lat
    a, ee = 6378245.0, 0.00669342162296594323
    x, y = lng - 105.0, lat - 35.0
    dlat = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
            + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            + (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
            + (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0)
    dlng = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
            + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
            + (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
            + (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sq) * math.pi)
    dlng = (dlng * 180.0) / (a / sq * math.cos(rad) * math.pi)
    return round(lng + dlng, 6), round(lat + dlat, 6)


def _amap_key() -> str:
    value = os.environ.get("AMAP_KEY", "").strip()
    if value:
        return value
    try:
        return (DATA_DIR / "amap-key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def geocode(text: str, *, http=None) -> dict | None:
    """Place text -> {lat, lng, label, source}. "lat,lng" is used as given (assumed GCJ-02).

    With an optional AMap Web-service key (``AMAP_KEY`` / official-bot/amap-key) AMap POI search
    is used (GCJ-02, best for Chinese landmarks); otherwise OpenStreetMap Nominatim (free, no key,
    1 req/s, WGS-84 converted to GCJ-02). The resolved label is always echoed to the user.
    """
    http = http or requests
    text = (text or "").strip()[:60]
    m = COORD.search(text)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        lat, lng = (a, b) if abs(a) <= 90 and abs(b) > 90 or abs(a) < abs(b) else (b, a)
        return {"lat": lat, "lng": lng, "label": f"{lat:.4f},{lng:.4f}", "source": "coords"}
    if len(text) < 2:
        return None
    key = _amap_key()
    try:
        if key:
            r = http.get(AMAP_POI, params={"keywords": text, "key": key, "offset": 1, "page": 1}, timeout=10)
            pois = (r.json() or {}).get("pois") or []
            if pois and pois[0].get("location"):
                lng, lat = (float(v) for v in pois[0]["location"].split(","))
                return {"lat": lat, "lng": lng, "label": pois[0].get("name") or text, "source": "amap"}
            return None
        r = http.get(NOMINATIM, params={"q": text, "format": "jsonv2", "limit": 1, "countrycodes": "cn",
                                         "accept-language": "zh-CN"},
                     headers={"User-Agent": "MaxShenBi-QQBot/1.0 (luckin_coffee plugin)"}, timeout=10)
        rows = r.json() if r.status_code == 200 else []
        if rows:
            lng, lat = wgs84_to_gcj02(float(rows[0]["lon"]), float(rows[0]["lat"]))
            label = rows[0].get("name") or str(rows[0].get("display_name", "")).split(",")[0] or text
            return {"lat": lat, "lng": lng, "label": label, "source": "osm"}
    except Exception:  # noqa: BLE001 - geocoding is best effort
        return None
    return None


# ------------------------------------------------------------------ state

def plugin_config(cfg: dict | None) -> dict:
    custom = ((cfg or {}).get("plugin_bindings") or {}).get(PLUGIN_ID, {}).get("config", {}) or {}
    return {**DEFAULTS, **{k: v for k, v in custom.items() if k in DEFAULTS}}


def _state_path(goid: str) -> Path:
    return G.group_dir(goid) / "luckin-state.json"


def load_state(goid: str) -> dict:
    try:
        state = json.loads(_state_path(goid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    for key, empty in (("sessions", {}), ("locations", {}), ("orders", []), ("events", [])):
        state.setdefault(key, empty)
    return state


def save_state(goid: str, state: dict) -> None:
    state["orders"] = state["orders"][-300:]
    state["events"] = state["events"][-500:]
    atomic_write_json(_state_path(goid), state)


def state_lock(goid: str):
    return file_lock(G.group_dir(goid) / ".luckin.lock", timeout=60)


def member_lock(goid: str, member: str):
    digest = hashlib.sha256(member.encode()).hexdigest()[:16]
    return file_lock(G.group_dir(goid) / f".luckin-{digest}.lock", timeout=180)


def active_session(state: dict, member: str, now: float | None = None) -> dict | None:
    session = state["sessions"].get(member)
    now = time.time() if now is None else now
    if not session:
        return None
    if session.get("stage") == "paying":
        return session  # the poller owns it until paid / cancelled / timed out
    return session if session.get("expires", 0) > now else None


# ------------------------------------------------------------------ intent

def _clean(text: str) -> str:
    text = re.sub(r"<qqbot-at-user[^>]*>|<@!?\w+>", " ", text or "")
    return text.replace("\u3000", " ").strip()


def is_order_intent(text: str) -> bool:
    t = _clean(text)
    if re.match(r"^/(咖啡|瑞幸|coffee|luckin)\b", t, re.I) or t in {"/咖啡", "/瑞幸"}:
        return True
    return bool(re.search(DRINK, t, re.I) and re.search(ORDER_VERB, t))


def classify(text: str, at_me: bool, session: dict | None) -> str | None:
    """Which step a message is for (None = not ours). Pure; the worker re-checks under a lock."""
    t = _clean(text)
    stage = (session or {}).get("stage")
    if session:
        if t in CANCEL_WORDS:
            return "cancel"
        if stage == "paying":
            if PICKUP_STATUS.search(t) and at_me:
                return "status"
            return "start" if at_me and is_order_intent(t) else None
        if re.match(r"^(位置|换个?地方|换位置|地址)\s*[:：]?", t):
            return "location"
        if stage in {"choose", "confirm"} and re.match(r"^\s*(\d{1,2})(\s|$)", t):
            return "choose"
        if stage == "confirm" and t.rstrip("!！。.~") in CONFIRM_WORDS:
            return "confirm"
        if at_me and is_order_intent(t):
            return "start"
        if stage == "await_location" and (at_me or len(t) <= 40) and t and not t.startswith("/"):
            return "location"
        return None
    if at_me and is_order_intent(t):
        return "start"
    return None


def split_request(text: str) -> tuple[str, str]:
    """-> (location text or "", drink query or "")."""
    raw = _clean(text)
    command = re.match(r"^/(咖啡|瑞幸|coffee|luckin)", raw, re.I)
    t = raw[command.end():].strip() if command else raw
    drinks = DRINKS.findall(t)
    loc = ""
    m = COORD.search(t)
    if m:
        loc = m.group(0)
    else:
        m = LOCATION.search(t) or LOCATED.search(t)
        if m:
            loc = re.sub(r"^(我|人)?在", "", m.group(1))
        elif command:
            rest = DRINKS.sub(" ", t)
            rest = re.sub(r"想喝|要喝|来一?杯|点一?杯|买一?杯|喝一?杯|咖啡|瑞幸|一杯|帮我|请|下单|点单", " ", rest)
            loc = re.sub(r"\s+", " ", rest).strip(" ，,。")
    return loc.strip(), (drinks[0] if drinks else "")


# ------------------------------------------------------------------ rendering

def money(v) -> str:
    try:
        return f"¥{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def at(member: str) -> str:
    return f'<qqbot-at-user id="{member}" />'


def _spec(product: dict) -> str:
    chosen = []
    for group in product.get("productAttrs") or []:
        for sub in group.get("productSubAttrs") or []:
            if sub.get("selected"):
                chosen.append(str(sub.get("attributeName")))
    return "/".join(chosen)


def render_options(member: str, loc: dict, options: list[dict], cfg: dict, minutes: int) -> str:
    lines = [f"{at(member)} ☕ 附近瑞幸 · 到手价从低到高（到店自取）",
             f"📍 按「{loc['label']}」附近查的" + ("" if loc.get("source") == "coords" else "（位置不准就回「位置 具体地标」）"), "",
             "| # | 饮品 | 到手价 | 原价 | 门店 | 距离 |", "|---|---|---|---|---|---|"]
    for i, o in enumerate(options, 1):
        name = o["productName"] + (f"（{o['spec']}）" if o.get("spec") else "")
        dist = f"{o['distance']:.1f}km" if isinstance(o.get("distance"), (int, float)) else "—"
        lines.append(f"| {i} | {name} | **{money(o['estimatePrice'])}** | {money(o['initialPrice'])} | {o['deptName']} | {dist} |")
    lines += ["", "👉 回复编号就能下单，比如「1」；想换口味写「1 热 少甜」",
              f"不要了回「取消」 · {minutes} 分钟内有效 · 只认你本人的回复"]
    if cfg.get("quick_buttons"):
        lines.append(" ".join(f'<qqbot-cmd-input text="{i}" show="{i}" />' for i in range(1, min(len(options), 6) + 1)))
    return "\n".join(lines)


def render_confirm(member: str, choice: dict, preview: dict, minutes: int) -> str:
    items = preview.get("productInfoList") or []
    item = items[0] if items else {}
    spec = item.get("additionDesc") or choice.get("spec") or "默认规格"
    eta = ""
    if preview.get("aboutTime"):
        eta = time.strftime("%H:%M", time.localtime(preview["aboutTime"] / 1000))
    coupons = preview.get("couponCodeList") or []
    lines = [f"{at(member)} 🧾 确认一下订单（到店自取）", "",
             f"- 饮品：**{item.get('name') or choice['productName']}**（{spec}）×1",
             f"- 门店：{choice['deptName']} · {choice.get('address') or ''}",
             f"- 原价 {money(preview.get('totalInitialPrice'))}，优惠 -{money(preview.get('privilegeMoney') or 0)}"
             + ("（已自动用券）" if coupons else ""),
             f"- **实付 {money(preview.get('discountPrice'))}**" + (f" · 预计 {eta} 可取" if eta else ""), "",
             f"回复「确认」下单，「取消」放弃；换一杯直接回编号 · {minutes} 分钟内有效"]
    return "\n".join(lines)


def render_pay(member: str, choice: dict, created: dict, preview: dict, minutes: int, qr_inline: bool) -> str:
    url = created.get("payOrderQrCodeUrl") or ""
    price = created.get("discountPrice", preview.get("discountPrice"))
    lines = [f"{at(member)} ✅ 订单已创建：{choice['productName']} @ {choice['deptName']}",
             f"请在 {minutes} 分钟内扫码支付 **{money(price)}**（微信扫一扫）"]
    if url:
        lines.append(f"[打开支付二维码]({url})")
    lines += ["⚠️ 这是你自己的单，只有你本人扫码支付哦；付好后我会自动 @你 发取餐码。",
              "不想要了回「取消」。"]
    if qr_inline:
        lines.insert(2, "⬇️ 支付二维码")
    return "\n".join(lines)


def render_pickup(member: str, detail: dict, choice: dict) -> str:
    shop = detail.get("shopInfo") or {}
    code = ((detail.get("takeMealCodeInfo") or {}).get("code") or "").strip()
    goods = "、".join(c.get("commodityName", "") for c in detail.get("orderCommodityList") or [] if c.get("commodityName")) or choice.get("productName", "")
    lines = [f"{at(member)} 🎉 支付成功！取餐码 **{code or '生成中，稍后在瑞幸 App 订单里看'}**", "",
             f"- 门店：{shop.get('deptName') or choice.get('deptName')}（{shop.get('address') or choice.get('address') or ''}）",
             f"- 饮品：{goods}",
             f"- 状态：{detail.get('orderStatusName') or '制作中'}"]
    if detail.get("aboutTime"):
        lines.append("- 预计 " + time.strftime("%H:%M", time.localtime(detail["aboutTime"] / 1000)) + " 可取")
    lines += ["", random.choice(BLESSINGS)]
    return "\n".join(lines)


# ------------------------------------------------------------------ sending

def send(goid: str, markdown: str, *, msg_id: str = "", image: str = "", action_id: str = "") -> bool:
    """One message via send_group.py (sent-log, delivery dedupe, plugin message.send gate)."""
    argv = [sys.executable, str(SEND_CLI), "--group", goid, "--text", markdown, "--format", "auto",
            "--trigger", "command", "--plugin-id", PLUGIN_ID]
    if image:
        argv += ["--image", image]
    if msg_id:
        argv += ["--msg-id", msg_id]
    if action_id:
        argv += ["--action-id", action_id]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=90, cwd=str(CODE_DIR))
    except Exception:  # noqa: BLE001
        return False
    if proc.returncode != 0 and msg_id:
        # Passive reply window (5 min) may have closed: send once as a normal group message.
        argv = argv[:argv.index("--msg-id")] + argv[argv.index("--msg-id") + 2:]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=90, cwd=str(CODE_DIR))
    return proc.returncode == 0


def fetch_qr(url: str, *, http=None) -> str:
    """Download the payment QR when it is an image; returns a temp file path or ""."""
    if not url.startswith("https://"):
        return ""
    try:
        r = (http or requests).get(url, timeout=15)
        kind = (r.headers or {}).get("Content-Type", "")
        if r.status_code == 200 and kind.startswith("image/") and len(r.content) < 2 * 1024 * 1024:
            fd, path = tempfile.mkstemp(suffix="." + (kind.split("/")[1].split(";")[0] or "png"), prefix="luckin-qr-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(r.content)
            return path
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ------------------------------------------------------------------ steps

class Runner:
    """Executes one step for one member. Injectable client / sender / clock for tests."""

    def __init__(self, goid: str, *, client=None, sender=None, geocoder=None, clock=None, sleep=None,
                 spawn_poll=None, qr=None):
        self.goid = goid
        self.cfg = plugin_config(G.load_config(goid))
        self._client = client
        self.sender = sender or send
        self.geocoder = geocoder or geocode
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.spawn_poll = spawn_poll or spawn_poller
        self.qr = qr or fetch_qr

    @property
    def client(self):
        if self._client is None:
            self._client = MCPClient()
        return self._client

    def say(self, text: str, msg_id: str = "", key: str = "", image: str = "") -> bool:
        return self.sender(self.goid, text, msg_id=msg_id, image=image,
                           action_id=("luckin:" + key) if key else "")

    def minutes(self) -> int:
        return int(self.cfg["session_minutes"])

    # -- entry -------------------------------------------------------------
    def handle(self, member: str, text: str, msg_id: str, at_me: bool) -> str:
        if not member:
            return "ignored"
        if self.cfg["allow_members"] and member not in self.cfg["allow_members"]:
            return "ignored"
        with member_lock(self.goid, member):
            with state_lock(self.goid):
                state = load_state(self.goid)
                if msg_id and msg_id in state["events"]:
                    return "duplicate"
                session = active_session(state, member, self.clock())
                step = classify(text, at_me, session)
                if msg_id:
                    state["events"].append(msg_id)
                if session is None and member in state["sessions"]:
                    state["sessions"].pop(member)
                save_state(self.goid, state)
            if step is None:
                return "ignored"
            if not token():
                self.say(f"{at(member)} ☕ 瑞幸点单还没开通（还在等官方授权），先手动点吧～开通了第一时间告诉大家！",
                         msg_id, f"{msg_id}:disabled")
                return "disabled"
            try:
                return getattr(self, "step_" + step)(member, text, msg_id, session)
            except LuckinError as exc:
                if exc.kind == "auth":
                    self.say(f"{at(member)} ☕ 瑞幸点单暂时不可用（授权过期了，等管理员重新登录），这次先手动点哈～",
                             msg_id, f"{msg_id}:auth")
                else:
                    self.say(f"{at(member)} 瑞幸那边暂时没响应，稍后再试试～", msg_id, f"{msg_id}:err")
                if not (session and session.get("stage") == "paying"):
                    self._drop(member)
                return "error:" + exc.kind

    # -- helpers -----------------------------------------------------------
    def _update(self, member: str, **fields) -> dict:
        with state_lock(self.goid):
            state = load_state(self.goid)
            session = state["sessions"].get(member) or {"created": self.clock()}
            session.update(fields)
            session["expires"] = self.clock() + self.minutes() * 60
            state["sessions"][member] = session
            save_state(self.goid, state)
            return session

    def _drop(self, member: str) -> None:
        with state_lock(self.goid):
            state = load_state(self.goid)
            state["sessions"].pop(member, None)
            save_state(self.goid, state)

    def _remember(self, member: str, loc: dict) -> None:
        with state_lock(self.goid):
            state = load_state(self.goid)
            state["locations"][member] = {**loc, "ts": self.clock()}
            save_state(self.goid, state)

    def order_block(self, member: str) -> str:
        if self.cfg["order_members"] and member not in self.cfg["order_members"]:
            return "这个群里下单功能只对指定成员开放，比价随便看～"
        state = load_state(self.goid)
        now = self.clock()
        mine = [o for o in state["orders"] if o.get("member") == member]
        if any(o.get("status") == "pending" for o in mine):
            return "你还有一单在等支付，先付掉或回「取消」再点新的～"
        if sum(1 for o in mine if now - o.get("ts", 0) < 3600) >= int(self.cfg["max_orders_per_member_per_hour"]):
            return "一小时内最多点 %d 单，喝慢点哈～" % int(self.cfg["max_orders_per_member_per_hour"])
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if sum(1 for o in state["orders"] if time.strftime("%Y-%m-%d", time.localtime(o.get("ts", 0))) == today) \
                >= int(self.cfg["max_orders_per_group_per_day"]):
            return "今天群里点单次数到上限了，明天再来～"
        return ""

    # -- steps -------------------------------------------------------------
    def step_start(self, member: str, text: str, msg_id: str, session) -> str:
        if session and session.get("stage") == "paying":
            self.say(f"{at(member)} 你还有一单在等支付，先付掉或回「取消」再点新的～", msg_id, f"{msg_id}:busy")
            return "blocked"
        loc_text, query = split_request(text)
        note = "（目前只能到店自取，不能外送哦）" if DELIVERY.search(_clean(text)) else ""
        loc = self.geocoder(loc_text) if loc_text else None
        if loc is None and not loc_text:
            state = load_state(self.goid)
            loc = state["locations"].get(member)
        if loc is None:
            self._update(member, stage="await_location", query=query, msg_id=msg_id)
            ask = "没认出这个位置，" if loc_text else ""
            self.say(f"{at(member)} ☕ 想喝咖啡呀{note}！{ask}你现在在哪儿？回个地址或地标就行，"
                     "比如「南京新街口德基广场」，也可以直接发经纬度「32.04,118.78」。", msg_id, f"{msg_id}:ask")
            return "await_location"
        return self._list(member, loc, query, msg_id, note)

    def step_location(self, member: str, text: str, msg_id: str, session) -> str:
        place = re.sub(r"^(位置|换个?地方|换位置|地址)\s*[:：]?\s*", "", _clean(text))
        place = re.sub(r"^(我|人)?在", "", place).strip()
        loc = self.geocoder(place)
        if loc is None:
            self._update(member, stage="await_location", msg_id=msg_id)
            self.say(f"{at(member)} 没找到「{place[:20]}」😅 换个更具体的地标试试，比如「XX市XX路XX广场」。",
                     msg_id, f"{msg_id}:noloc")
            return "await_location"
        return self._list(member, loc, (session or {}).get("query", ""), msg_id, "")

    def _list(self, member: str, loc: dict, query: str, msg_id: str, note: str) -> str:
        self._remember(member, loc)
        shops = self.client.call("queryShopList", {"longitude": loc["lng"], "latitude": loc["lat"]}) or []
        shops = [s for s in shops if isinstance(s, dict) and s.get("deptId")][: int(self.cfg["max_shops"])]
        if not shops:
            self._update(member, stage="await_location", query=query, msg_id=msg_id)
            self.say(f"{at(member)} 「{loc['label']}」附近没查到营业的瑞幸 😢 换个位置试试？", msg_id, f"{msg_id}:noshop")
            return "no_shop"
        queries = [query] if query else list(self.cfg["default_queries"])[:2]
        seen, options = set(), []
        for shop in shops:
            for q in queries:
                try:
                    products = self.client.call("searchProductForMcp", {"deptId": shop["deptId"], "query": q}) or []
                except LuckinError as exc:
                    if exc.kind == "auth":
                        raise
                    continue
                if isinstance(products, dict):
                    products = [products]
                for p in [p for p in products if isinstance(p, dict)][: int(self.cfg["products_per_search"])]:
                    key = (shop["deptId"], p.get("skuCode"))
                    if not p.get("productId") or not p.get("skuCode") or key in seen or p.get("estimatePrice") is None:
                        continue
                    seen.add(key)
                    options.append({"deptId": shop["deptId"], "deptName": shop.get("deptName", ""),
                                    "address": shop.get("address", ""), "longitude": shop.get("longitude"),
                                    "latitude": shop.get("latitude"),
                                    "distance": shop.get("distance") if loc.get("source") != "ip" else None,
                                    "productId": p["productId"], "productName": p.get("productName", ""),
                                    "skuCode": p["skuCode"], "spec": _spec(p),
                                    "estimatePrice": p.get("estimatePrice"), "initialPrice": p.get("initialPrice"),
                                    "productAttrs": p.get("productAttrs") or []})
        options.sort(key=lambda o: (float(o["estimatePrice"]), o.get("distance") or 99))
        options = options[: int(self.cfg["max_options"])]
        if not options:
            self._update(member, stage="await_location", query=query, msg_id=msg_id)
            self.say(f"{at(member)} 附近门店没搜到「{query or '咖啡'}」，换个饮品名试试？", msg_id, f"{msg_id}:noprod")
            return "no_product"
        self._update(member, stage="choose", options=options, loc=loc, query=query, msg_id=msg_id)
        body = render_options(member, loc, options, self.cfg, self.minutes())
        if note:
            body = body.replace("（到店自取）", "（到店自取）" + note, 1)
        self.say(body, msg_id, f"{msg_id}:list")
        return "listed"

    def _customize(self, choice: dict, words: list[str]) -> dict:
        if not words:
            return choice
        detail = self.client.call("queryProductDetailInfo", {"deptId": choice["deptId"], "productId": choice["productId"]}) or {}
        attrs = detail.get("productAttrs") or choice.get("productAttrs") or []
        sku = detail.get("skuCode") or choice["skuCode"]
        product = None
        for word in words:
            for group in attrs:
                for sub in group.get("productSubAttrs") or []:
                    if sub.get("attributeName") == word and not sub.get("selected") and sub.get("canSelected") != 0:
                        product = self.client.call("switchProduct", {
                            "deptId": choice["deptId"], "productId": choice["productId"], "skuCode": sku,
                            "attrOperationParam": {"attributeId": group.get("attributeId"),
                                                   "subAttr": {"attributeId": sub.get("attributeId"), "operation": 3}},
                            "amount": 1}) or {}
                        sku = product.get("skuCode") or sku
                        attrs = product.get("productAttrs") or attrs
        if product:
            choice = {**choice, "skuCode": sku, "spec": _spec(product) or choice.get("spec", ""),
                      "estimatePrice": product.get("estimatePrice", choice["estimatePrice"])}
        return choice

    def step_choose(self, member: str, text: str, msg_id: str, session) -> str:
        m = re.match(r"^\s*(\d{1,2})\s*(.*)$", _clean(text))
        index, extra = int(m.group(1)), m.group(2)
        options = session.get("options") or []
        if not 1 <= index <= len(options):
            self.say(f"{at(member)} 没有第 {index} 个选项哦，回 1~{len(options)} 之间的编号～", msg_id, f"{msg_id}:range")
            return "bad_choice"
        blocked = self.order_block(member)
        if blocked:
            self.say(f"{at(member)} {blocked}", msg_id, f"{msg_id}:blocked")
            return "blocked"
        choice = self._customize(options[index - 1], [w for w in re.split(r"[\s,，/、]+", extra) if w])
        product_list = [{"amount": 1, "productId": choice["productId"], "skuCode": choice["skuCode"]}]
        preview = self.client.call("previewOrder", {"deptId": choice["deptId"], "productList": product_list}) or {}
        items = preview.get("productInfoList") or []
        if preview.get("discountPrice") is None or (items and items[0].get("skuCode") not in (None, choice["skuCode"])):
            self.say(f"{at(member)} 这杯暂时下不了单（可能售罄），换个编号试试？", msg_id, f"{msg_id}:nopreview")
            return "preview_failed"
        self._update(member, stage="confirm", choice=choice, preview={
            k: preview.get(k) for k in ("discountPrice", "totalInitialPrice", "privilegeMoney", "couponCodeList",
                                        "productInfoList", "aboutTime")}, msg_id=msg_id)
        self.say(render_confirm(member, choice, preview, self.minutes()), msg_id, f"{msg_id}:confirm")
        return "confirm"

    def step_confirm(self, member: str, text: str, msg_id: str, session) -> str:
        blocked = self.order_block(member)
        if blocked:
            self.say(f"{at(member)} {blocked}", msg_id, f"{msg_id}:blocked")
            return "blocked"
        choice, preview = session["choice"], session["preview"]
        args = {"deptId": choice["deptId"],
                "productList": [{"amount": 1, "productId": choice["productId"], "skuCode": choice["skuCode"]}],
                "longitude": choice.get("longitude") or session["loc"]["lng"],
                "latitude": choice.get("latitude") or session["loc"]["lat"]}
        if preview.get("couponCodeList"):
            args["couponCodeList"] = list(preview["couponCodeList"])
        created = self.client.call("createOrder", args) or {}
        order_id = str(created.get("orderIdStr") or created.get("orderId") or "")
        if not order_id:
            self._drop(member)
            self.say(f"{at(member)} 下单没成功，瑞幸那边没返回订单，稍后再试～", msg_id, f"{msg_id}:nocreate")
            return "create_failed"
        with state_lock(self.goid):
            state = load_state(self.goid)
            state["orders"].append({"member": member, "order_id": order_id, "ts": self.clock(), "status": "pending",
                                    "deptName": choice["deptName"], "product": choice["productName"]})
            state["sessions"][member] = {**session, "stage": "paying", "order_id": order_id,
                                         "paid_msg_id": msg_id, "paying_since": self.clock(),
                                         "expires": self.clock() + int(self.cfg["pay_timeout_minutes"]) * 60}
            save_state(self.goid, state)
        qr = self.qr(created.get("payOrderQrCodeUrl") or "")
        try:
            body = render_pay(member, choice, created, preview, int(self.cfg["pay_timeout_minutes"]), bool(qr))
            if not self.say(body, msg_id, f"{order_id}:pay", image=qr) and qr:
                self.say(render_pay(member, choice, created, preview, int(self.cfg["pay_timeout_minutes"]), False),
                         msg_id, f"{order_id}:pay-link")
        finally:
            if qr:
                Path(qr).unlink(missing_ok=True)
        self.spawn_poll(self.goid, member, order_id)
        return "created"

    def step_cancel(self, member: str, text: str, msg_id: str, session) -> str:
        order_id = (session or {}).get("order_id")
        if order_id:
            ok = bool(self.client.call("cancelOrder", {"orderId": order_id}))
            self._finish(member, order_id, "cancelled" if ok else "pending")
            self.say(f"{at(member)} " + ("已帮你取消这单，没扣钱 👌" if ok else "取消没成功，可能已经付过了，去瑞幸 App 看看订单～"),
                     msg_id, f"{msg_id}:cancel")
            return "cancelled" if ok else "cancel_failed"
        self._drop(member)
        self.say(f"{at(member)} 好的，这次不点了～想喝的时候再 @我", msg_id, f"{msg_id}:cancel")
        return "cancelled"

    def step_status(self, member: str, text: str, msg_id: str, session) -> str:
        detail = self.client.call("queryOrderDetailInfo", {"orderId": session["order_id"]}) or {}
        if int(detail.get("orderStatus") or 0) in PAID:
            self.say(render_pickup(member, detail, session.get("choice") or {}), msg_id, f"{msg_id}:status")
        else:
            self.say(f"{at(member)} 这单还没支付成功（{detail.get('orderStatusName') or '待付款'}），付好后我会自动发取餐码～",
                     msg_id, f"{msg_id}:status")
        return "status"

    def _finish(self, member: str, order_id: str, status: str) -> None:
        with state_lock(self.goid):
            state = load_state(self.goid)
            for order in state["orders"]:
                if order.get("order_id") == order_id:
                    order["status"] = status
            if (state["sessions"].get(member) or {}).get("order_id") == order_id and status != "pending":
                state["sessions"].pop(member, None)
            save_state(self.goid, state)

    # -- payment poller ----------------------------------------------------
    def poll(self, member: str, order_id: str) -> str:
        deadline = self.clock() + int(self.cfg["pay_timeout_minutes"]) * 60
        interval = max(5, int(self.cfg["poll_seconds"]))
        errors, paid_at = 0, None
        while True:
            state = load_state(self.goid)
            session = state["sessions"].get(member) or {}
            record = next((o for o in state["orders"] if o.get("order_id") == order_id), {})
            if record.get("status") in {"cancelled", "paid", "timeout"}:
                return record["status"]
            try:
                detail = self.client.call("queryOrderDetailInfo", {"orderId": order_id}) or {}
                errors = 0
            except LuckinError as exc:
                errors += 1
                if exc.kind == "auth" or errors >= 8:
                    self.say(f"{at(member)} 暂时查不到订单状态了，付过的话直接看瑞幸 App 里的取餐码～", "", f"{order_id}:lost")
                    self._finish(member, order_id, "unknown")
                    return "unknown"
                detail = {}
            status = int(detail.get("orderStatus") or 0) if detail else 0
            if status in PAID:
                code = ((detail.get("takeMealCodeInfo") or {}).get("code") or "").strip()
                paid_at = paid_at or self.clock()
                if (code and code != "生成中") or self.clock() - paid_at > 120:
                    self.say(render_pickup(member, detail, session.get("choice") or {}),
                             self._fresh_msg(session), f"{order_id}:paid")
                    self._finish(member, order_id, "paid")
                    return "paid"
            elif status == CANCELLED:
                self.say(f"{at(member)} 这单已经取消了（{detail.get('orderStatusName') or '已取消'}），想喝再 @我～", "",
                         f"{order_id}:cancelled")
                self._finish(member, order_id, "cancelled")
                return "cancelled"
            if self.clock() >= deadline and not paid_at:
                ok = False
                try:
                    ok = bool(self.client.call("cancelOrder", {"orderId": order_id}))
                except LuckinError:
                    pass
                self.say(f"{at(member)} ⏰ 超过 {int(self.cfg['pay_timeout_minutes'])} 分钟没支付，"
                         + ("已帮你自动取消这单。" if ok else "自动取消没成功，记得去瑞幸 App 里处理一下。")
                         + "还想喝的话再 @我～", "", f"{order_id}:timeout")
                self._finish(member, order_id, "timeout" if ok else "unknown")
                return "timeout"
            self.sleep(interval)

    def _fresh_msg(self, session: dict) -> str:
        since = session.get("paying_since") or 0
        return session.get("paid_msg_id", "") if self.clock() - since < 270 else ""


# ------------------------------------------------------------------ process plumbing

def _spawn(argv: list[str]) -> int:
    log_dir = DATA_DIR / "media" / "luckin"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "worker.out", "ab") as log:
        proc = subprocess.Popen([sys.executable, str(WORKER_CLI), *argv], stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(CODE_DIR), start_new_session=True)
    return proc.pid


def start_step(goid: str, event: dict) -> dict:
    """Plugin action entry: run the step detached so the gateway never blocks on Luckin/HTTP."""
    payload = {"member": str(event.get("member_openid") or ""), "text": str(event.get("text_only") or event.get("text") or event.get("raw_message") or "")[:500],
               "msg_id": str(event.get("msg_id") or event.get("event_id") or ""), "at_me": bool(event.get("at_me"))}
    pid = _spawn(["step", "--group", goid, "--event", json.dumps(payload, ensure_ascii=False)])
    return {"status": "started", "pid": pid}


def spawn_poller(goid: str, member: str, order_id: str) -> int:
    return _spawn(["poll", "--group", goid, "--member", member, "--order", order_id])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="luckin_coffee plugin worker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("step")
    s.add_argument("--group", required=True)
    s.add_argument("--event", required=True, help="JSON {member, text, msg_id, at_me}")
    p = sub.add_parser("poll")
    p.add_argument("--group", required=True)
    p.add_argument("--member", required=True)
    p.add_argument("--order", required=True)
    sub.add_parser("check", help="verify the token against the MCP (lists tool names; never prints the token)")
    for name in ("enable", "disable"):
        e = sub.add_parser(name, help=f"{name} the plugin for one group")
        e.add_argument("--group", required=True)
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if a.cmd == "check":
            if not token():
                print(json.dumps({"ok": False, "error": "no token (LUCKIN_MCP_TOKEN or official-bot/luckin-token)"}))
                return 4
            print(json.dumps({"ok": True, "tools": MCPClient().tools()}, ensure_ascii=False))
            return 0
        if a.cmd in ("enable", "disable"):
            goid = G.resolve(a.group)
            G.set_plugin_enabled(goid, PLUGIN_ID, a.cmd == "enable")
            cfg = G.load_config(goid)
            print(json.dumps({"ok": True, "group": cfg.get("name"), "luckin_coffee": G.plugin_enabled(goid, PLUGIN_ID)},
                             ensure_ascii=False))
            return 0
        goid = G.resolve(a.group)
        if not G.plugin_enabled(goid, PLUGIN_ID):
            print(f"[{stamp}] plugin disabled; nothing done")
            return 0
        if a.cmd == "step":
            ev = json.loads(a.event)
            out = Runner(goid).handle(ev.get("member", ""), ev.get("text", ""), ev.get("msg_id", ""), bool(ev.get("at_me")))
        else:
            out = Runner(goid).poll(a.member, a.order)
        print(f"[{stamp}] {a.cmd} -> {out}")
        return 0
    except LuckinError as exc:
        print(f"[{stamp}] {a.cmd} failed: {exc.kind}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[{stamp}] {a.cmd} crashed: {type(exc).__name__}")
        return 2
