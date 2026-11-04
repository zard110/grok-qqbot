"""Offline tests for the luckin_coffee plugin (core/luckin.py) against a local mock Luckin MCP server."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import groups as G
from core import action_runner
from core import luckin as L
from core.plugins import PluginRegistry
from test_groups import A, GroupFixture

TOKEN = "test-tok"
ALICE, BOB = "ABCDEF01234ABCDE89AB", "ABCDEF01234ABCDE89CD"
SHOPS = [
    {"deptId": 101, "deptName": "新街口店", "address": "中山路1号", "deptTags": [], "longitude": 118.781,
     "latitude": 32.041, "workTimeStart": "07:00", "workTimeEnd": "22:00", "distance": 0.3, "number": "(No.1)"},
    {"deptId": 102, "deptName": "德基店", "address": "中山路18号", "deptTags": [], "longitude": 118.783,
     "latitude": 32.043, "workTimeStart": "07:00", "workTimeEnd": "22:00", "distance": 0.6, "number": "(No.2)"},
]
ATTRS = [{"attributeId": 122, "attributeName": "温度", "productSubAttrs": [
    {"attributeId": 429, "attributeName": "热", "selected": False, "price": 0, "canSelected": 1},
    {"attributeId": 428, "attributeName": "冰", "selected": True, "price": 0, "canSelected": 1}]}]


def product(pid, name, sku, est, init):
    return {"productId": pid, "productName": name, "skuCode": sku, "pictureUrl": "", "productAttrs": ATTRS,
            "tags": [], "initialPrice": init, "estimatePrice": est}


class MockLuckin:
    """Minimal Streamable-HTTP MCP: JSON or SSE replies, Bearer check, optional session handshake."""

    def __init__(self):
        self.calls, self.statuses, self.require_session, self.reject = [], [], False, False
        self.products = {101: [product(11, "生椰拿铁", "SP1-1", 9.9, 29), product(12, "美式", "SP2-1", 12.9, 23)],
                         102: [product(11, "生椰拿铁", "SP1-1", 13.9, 29), product(13, "拿铁", "SP3-1", 8.8, 26)]}
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if mock.reject or self.headers.get("Authorization") != "Bearer " + TOKEN:
                    return self.reply(401, {"message": "oauth token is not found"})
                if body.get("method") == "initialize":
                    return self.reply(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-03-26"}},
                                      {"Mcp-Session-Id": "s1"})
                if "id" not in body:
                    return self.reply(202, {})
                if mock.require_session and self.headers.get("Mcp-Session-Id") != "s1":
                    return self.reply(400, {"error": "Bad Request: No valid session ID provided"})
                name, args = body["params"]["name"], body["params"]["arguments"]
                mock.calls.append((name, args))
                data = mock.tool(name, args)
                result = {"content": [{"type": "text", "text": json.dumps({"code": 0, "msg": "success", "data": data,
                                                                           "success": True}, ensure_ascii=False)}]}
                message = {"jsonrpc": "2.0", "id": body["id"], "result": result}
                if name == "queryShopList":  # exercise the SSE framing
                    raw = ("event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return None
                return self.reply(200, message)

            def reply(self, status, payload, headers=None):
                raw = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1" + ":%d/order/user/mcp" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tool(self, name, args):
        if name == "queryShopList":
            return SHOPS
        if name == "searchProductForMcp":
            return [p for p in self.products[args["deptId"]] if args["query"] in p["productName"]]
        if name == "queryProductDetailInfo":
            return next(p for p in self.products[args["deptId"]] if p["productId"] == args["productId"])
        if name == "switchProduct":
            hot = [{**ATTRS[0], "productSubAttrs": [{**ATTRS[0]["productSubAttrs"][0], "selected": True},
                                                    {**ATTRS[0]["productSubAttrs"][1], "selected": False}]}]
            return {**product(args["productId"], "拿铁", "SP3-2", 8.8, 26), "productAttrs": hot}
        if name == "previewOrder":
            item = args["productList"][0]
            return {"aboutTime": 0, "discountPrice": 8.8, "totalInitialPrice": 26, "privilegeMoney": 17.2,
                    "couponCodeList": ["CPN-1"], "shopInfo": SHOPS[1],
                    "productInfoList": [{"productId": item["productId"], "skuCode": item["skuCode"], "name": "拿铁",
                                         "amount": 1, "additionDesc": "热/大杯", "initPrice": 26, "estimatePrice": 8.8}]}
        if name == "createOrder":
            return {"orderId": 7639308439653908490, "orderIdStr": "7639308439653908490",
                    "payOrderUrl": "weixin://wxpay/bizpayurl?pr=secret", "needPay": True, "discountPrice": 8.8,
                    "payOrderQrCodeUrl": "https://example.com/transfer/qrcode?token=abc"}
        if name == "queryOrderDetailInfo":
            status, code = self.statuses.pop(0) if self.statuses else (10, "生成中")
            return {"orderId": args["orderId"], "orderStatus": status,
                    "orderStatusName": {10: "待付款", 30: "制作中", 100: "已取消"}.get(status, "下单成功"),
                    "aboutTime": 0, "takeMealCodeInfo": {"code": code, "takeOrderId": "x"}, "shopInfo": SHOPS[1],
                    "orderPayAmount": 8.8, "orderCommodityList": [{"commodityName": "拿铁", "payableMoney": 8.8}]}
        if name == "cancelOrder":
            return True
        raise AssertionError(name)

    def names(self):
        return [name for name, _ in self.calls]


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class LuckinFixture(GroupFixture):
    def setUp(self):
        super().setUp()
        self.mock = MockLuckin()
        self.addCleanup(self.mock.server.server_close)
        self.addCleanup(self.mock.server.shutdown)
        for target, value in ((L, {"MCP_URL": self.mock.url}),):
            for key, val in value.items():
                p = patch.object(target, key, val)
                p.start()
                self.addCleanup(p.stop)
        env = patch.dict(os.environ, {"LUCKIN_MCP_TOKEN": TOKEN})
        env.start()
        self.addCleanup(env.stop)
        G.set_plugin_enabled(A, "luckin_coffee", True)
        self.sent, self.polls, self.clock = [], [], Clock()
        self.geocoded = []

    def geocoder(self, text):
        self.geocoded.append(text)
        return None if "火星" in text else {"lat": 32.04, "lng": 118.78, "label": text, "source": "osm"}

    def runner(self, **kw):
        return L.Runner(A, sender=lambda goid, text, **k: self.sent.append((text, k)) or True,
                        geocoder=self.geocoder, clock=self.clock, sleep=self.clock.sleep,
                        spawn_poll=lambda goid, member, order: self.polls.append((member, order)),
                        qr=lambda url: "", **kw)

    def say(self, member, text, msg, at_me=False):
        return self.runner().handle(member, text, msg, at_me)


class IntentTests(unittest.TestCase):
    def test_order_intent_is_narrow(self):
        for text in ("想喝咖啡", "/咖啡", "来杯生椰拿铁", "新街口附近有便宜的瑞幸吗", "帮我点一杯美式"):
            self.assertTrue(L.is_order_intent(text), text)
        for text in ("咖啡因对骑行有啥影响", "我想喝奶茶", "今天练什么", "拿铁和美式哪个热量高"):
            self.assertFalse(L.is_order_intent(text), text)

    def test_split_location_and_drink(self):
        self.assertEqual(L.split_request("我在新街口附近想喝拿铁"), ("新街口", "拿铁"))
        self.assertEqual(L.split_request("想喝生椰拿铁，我在德基广场"), ("德基广场", "生椰拿铁"))
        self.assertEqual(L.split_request("/咖啡 新街口"), ("新街口", ""))
        self.assertEqual(L.split_request("32.04,118.78 来杯美式"), ("32.04,118.78", "美式"))
        self.assertEqual(L.split_request("想喝咖啡"), ("", ""))

    def test_classify_follows_only_live_session(self):
        self.assertIsNone(L.classify("1", False, None))
        self.assertEqual(L.classify("想喝咖啡", True, None), "start")
        self.assertIsNone(L.classify("想喝咖啡", False, None))
        self.assertEqual(L.classify("2 热", False, {"stage": "choose"}), "choose")
        self.assertEqual(L.classify("确认", False, {"stage": "confirm"}), "confirm")
        self.assertIsNone(L.classify("确认", False, {"stage": "choose"}))
        self.assertEqual(L.classify("新街口", False, {"stage": "await_location"}), "location")
        self.assertEqual(L.classify("位置 德基广场", False, {"stage": "choose"}), "location")
        self.assertEqual(L.classify("取消", False, {"stage": "paying"}), "cancel")
        self.assertIsNone(L.classify("哈哈哈", False, {"stage": "paying"}))

    def test_gcj02_shift_inside_china_only(self):
        lng, lat = L.wgs84_to_gcj02(118.774088, 32.056596)
        self.assertTrue(0.002 < lng - 118.774088 < 0.008 and -0.004 < lat - 32.056596 < 0.0)
        self.assertEqual(L.wgs84_to_gcj02(-0.1276, 51.5072), (-0.1276, 51.5072))

    def test_geocode_coordinates_without_network(self):
        loc = L.geocode("118.78, 32.04", http=object())
        self.assertEqual((loc["lat"], loc["lng"], loc["source"]), (32.04, 118.78, "coords"))


class DispatchTests(LuckinFixture):
    def test_disabled_by_default_and_falls_through(self):
        G.set_plugin_enabled(A, "luckin_coffee", False)
        self.assertFalse(G.load_config(A)["features"]["luckin_coffee"])
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        result = registry.dispatch({"group_openid": A, "post_type": "message", "at_me": True, "text": "想喝咖啡",
                                    "member_openid": ALICE, "msg_id": "m1"})
        self.assertEqual([a["type"] for a in result["actions"]], ["routine.reply"])
        fresh = G.default_config()
        self.assertFalse(fresh["features"]["luckin_coffee"])

    def test_claims_intent_and_own_session_followups_only(self):
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        base = {"group_openid": A, "post_type": "message", "member_openid": ALICE}
        claimed = registry.dispatch({**base, "at_me": True, "text": "想喝咖啡", "msg_id": "m1"})
        self.assertEqual(claimed["actions"][0]["type"], "coffee.step")
        self.assertEqual(claimed["actions"][0]["plugin_id"], "luckin_coffee")
        chat = registry.dispatch({**base, "at_me": True, "text": "咖啡因有什么影响", "msg_id": "m2"})
        self.assertEqual(chat["actions"][0]["type"], "routine.reply")
        self.say(ALICE, "我在新街口附近想喝咖啡", "m3", True)
        own = registry.dispatch({**base, "at_me": False, "text": "1", "msg_id": "m4"})
        self.assertEqual(own["actions"][0]["type"], "coffee.step")
        other = registry.dispatch({**base, "member_openid": BOB, "at_me": False, "text": "1", "msg_id": "m5"})
        self.assertNotEqual((other["actions"] or [{}])[0].get("type"), "coffee.step")

    def test_allowlist_limits_who_can_start(self):
        G.update_plugin_binding(A, "luckin_coffee", {"enabled": True, "config": {"allow_members": [BOB]}})
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        result = registry.dispatch({"group_openid": A, "post_type": "message", "at_me": True, "text": "想喝咖啡",
                                    "member_openid": ALICE, "msg_id": "m1"})
        self.assertEqual(result["actions"][0]["type"], "routine.reply")

    def test_action_runner_starts_detached_worker(self):
        cfg = G.load_config(A)
        event = {"group_openid": A, "member_openid": ALICE, "msg_id": "m1", "text_only": "想喝咖啡", "at_me": True}
        with patch.object(L.subprocess, "Popen") as popen, patch.object(L, "DATA_DIR", self.root):
            popen.return_value.pid = 4321
            out = action_runner.run_actions(event, [{"type": "coffee.step", "group_openid": A,
                                                     "plugin_id": "luckin_coffee", "config_revision": cfg["revision"]}])
        argv = popen.call_args.args[0]
        self.assertEqual(out[0]["result"], {"status": "started", "pid": 4321})
        self.assertEqual(argv[2:5], ["step", "--group", A])
        self.assertEqual(json.loads(argv[6])["member"], ALICE)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class FlowTests(LuckinFixture):
    def test_missing_token_replies_politely_and_calls_nothing(self):
        with patch.dict(os.environ, {"LUCKIN_MCP_TOKEN": ""}), patch.object(L, "DATA_DIR", self.root):
            self.assertEqual(self.say(ALICE, "想喝咖啡", "m1", True), "disabled")
        self.assertEqual(self.mock.calls, [])
        self.assertIn("还没开通", self.sent[0][0])

    def test_expired_token_is_polite_and_drops_session(self):
        self.mock.reject = True
        self.assertEqual(self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True), "error:auth")
        self.assertIn("授权过期", self.sent[-1][0])
        self.assertNotIn(ALICE, L.load_state(A)["sessions"])

    def test_full_order_flow(self):
        self.assertEqual(self.say(ALICE, "想喝拿铁", "m1", True), "await_location")
        self.assertIn("在哪儿", self.sent[-1][0])
        self.assertEqual(self.say(ALICE, "新街口", "m2"), "listed")
        listing = self.sent[-1][0]
        self.assertTrue(listing.startswith(f'<qqbot-at-user id="{ALICE}" />'))
        self.assertIn("| # | 饮品 | 到手价 | 原价 | 门店 | 距离 |", listing)
        rows = [line for line in listing.splitlines() if line.startswith("| 1 ") or line.startswith("| 2 ")]
        self.assertIn("¥8.80", rows[0])  # cheapest first, across shops
        self.assertIn("德基店", rows[0])  # SSE body (no charset) decoded as UTF-8
        self.assertIn('<qqbot-cmd-input text="1" show="1" />', listing)
        self.assertEqual(self.sent[-1][1]["msg_id"], "m2")
        self.assertEqual(self.say(BOB, "1", "x1"), "ignored")  # someone else's session
        self.assertEqual(self.say(ALICE, "1 热", "m3"), "confirm")
        self.assertEqual(self.mock.names()[-3:], ["queryProductDetailInfo", "switchProduct", "previewOrder"])
        self.assertEqual(self.mock.calls[-1][1]["productList"][0]["skuCode"], "SP3-2")
        confirm = self.sent[-1][0]
        self.assertIn("实付 ¥8.80", confirm)
        self.assertIn("已自动用券", confirm)
        self.assertEqual(self.say(BOB, "确认", "x2"), "ignored")
        self.assertEqual(self.say(ALICE, "确认", "m4"), "created")
        name, args = self.mock.calls[-1]
        self.assertEqual(name, "createOrder")
        self.assertEqual(args["couponCodeList"], ["CPN-1"])
        self.assertEqual((args["longitude"], args["latitude"]), (118.783, 32.043))  # shop coordinates
        pay = self.sent[-1][0]
        self.assertIn("https://example.com/transfer/qrcode?token=abc", pay)
        self.assertNotIn("weixin://", pay)
        self.assertNotIn("取餐码", pay.replace("发取餐码", ""))
        self.assertEqual(self.polls, [(ALICE, "7639308439653908490")])
        self.assertEqual(self.say(ALICE, "想喝咖啡", "m5", True), "blocked")  # one active order
        self.mock.statuses = [(10, "生成中"), (30, "生成中"), (30, "A123")]
        self.assertEqual(self.runner().poll(ALICE, "7639308439653908490"), "paid")
        pickup = self.sent[-1][0]
        self.assertIn("取餐码 **A123**", pickup)
        self.assertIn("德基店", pickup)
        self.assertTrue(any(b in pickup for b in L.BLESSINGS))
        state = L.load_state(A)
        self.assertEqual(state["orders"][0]["status"], "paid")
        self.assertNotIn(ALICE, state["sessions"])
        self.assertEqual(state["locations"][ALICE]["label"], "新街口")
        blob = json.dumps(state, ensure_ascii=False) + json.dumps(self.sent, ensure_ascii=False)
        self.assertNotIn(TOKEN, blob)

    def test_remembered_location_skips_question_and_redelivery_is_ignored(self):
        self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True)
        self.assertEqual(self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True), "duplicate")
        self.assertEqual(self.say(ALICE, "想喝美式", "m2", True), "listed")
        self.assertEqual(self.geocoded, ["新街口"])
        self.assertEqual({a["query"] for n, a in self.mock.calls if n == "searchProductForMcp"} , {"生椰拿铁", "拿铁", "美式"})

    def test_unknown_place_asks_again(self):
        self.say(ALICE, "想喝咖啡", "m1", True)
        self.assertEqual(self.say(ALICE, "火星基地", "m2"), "await_location")
        self.assertIn("没找到", self.sent[-1][0])

    def test_session_expires(self):
        self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True)
        self.clock.now += 11 * 60
        self.assertEqual(self.say(ALICE, "1", "m2"), "ignored")

    def test_unpaid_order_is_cancelled_after_timeout(self):
        self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True)
        self.say(ALICE, "1", "m2")
        self.say(ALICE, "确认", "m3")
        self.assertEqual(self.runner().poll(ALICE, "7639308439653908490"), "timeout")
        self.assertEqual(self.mock.names()[-1], "cancelOrder")
        self.assertIn("自动取消", self.sent[-1][0])
        self.assertEqual(self.sent[-1][1]["msg_id"], "")  # active message, passive window long gone
        self.assertEqual(L.load_state(A)["orders"][0]["status"], "timeout")

    def test_member_cancel_while_paying(self):
        self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True)
        self.say(ALICE, "1", "m2")
        self.say(ALICE, "确认", "m3")
        self.assertEqual(self.say(ALICE, "取消", "m4"), "cancelled")
        self.assertEqual(self.mock.calls[-1], ("cancelOrder", {"orderId": "7639308439653908490"}))
        self.assertEqual(self.runner().poll(ALICE, "7639308439653908490"), "cancelled")

    def test_hourly_rate_limit(self):
        for n in range(2):
            self.say(ALICE, "我在新街口附近想喝咖啡", f"a{n}", True)
            self.say(ALICE, "1", f"b{n}")
            self.say(ALICE, "确认", f"c{n}")
            self.say(ALICE, "取消", f"d{n}")
        self.say(ALICE, "想喝咖啡", "e", True)
        self.assertEqual(self.say(ALICE, "1", "f"), "blocked")
        self.assertIn("一小时内最多点 2 单", self.sent[-1][0])
        self.clock.now += 3601
        self.say(ALICE, "想喝咖啡", "g", True)
        self.assertEqual(self.say(ALICE, "1", "h"), "confirm")

    def test_order_members_allowlist_blocks_ordering_not_browsing(self):
        G.update_plugin_binding(A, "luckin_coffee", {"enabled": True, "config": {"order_members": [BOB]}})
        self.assertEqual(self.say(ALICE, "我在新街口附近想喝咖啡", "m1", True), "listed")
        self.assertEqual(self.say(ALICE, "1", "m2"), "blocked")

    def test_stateful_gateway_handshake(self):
        self.mock.require_session = True
        client = L.MCPClient(TOKEN, self.mock.url)
        self.assertEqual(client.call("cancelOrder", {"orderId": "1"}), True)
        self.assertEqual(client.session_id, "s1")


if __name__ == "__main__":
    unittest.main()
