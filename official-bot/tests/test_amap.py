"""Offline tests for the amap plugin (core/amap.py); every AMap HTTP call is a local fake."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import groups as G
from core import action_runner
from core import amap as M
from core.plugins import PluginRegistry
from test_groups import A, GroupFixture

KEY = "fake-amap-key-for-tests"  # deliberately not 32 hex
ALICE, BOB = "ABCDEF01234ABCDE89AB", "ABCDEF01234ABCDE89CD"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class Resp:
    def __init__(self, data=None, *, status=200, content=b"", ctype="application/json"):
        self.data, self.status_code, self.content, self.headers = data, status, content, {"Content-Type": ctype}

    def json(self):
        if self.data is None:
            raise ValueError("not json")
        return self.data


def poi(i, name, dist, loc, rating="", cost=""):
    return {"id": f"B0TEST{i:04d}", "name": name, "location": loc, "distance": str(dist), "address": f"中山路{i}号",
            "citycode": "025", "business": {"rating": rating, "cost": cost}}


class FakeAmap:
    """Routes by path; records (path, params) without the key for assertions."""

    def __init__(self):
        self.calls, self.overrides = [], {}

    def get(self, url, params=None, timeout=None):
        assert params.get("key") == KEY
        path = url.replace(M.API, "")
        self.calls.append((path, {k: v for k, v in params.items() if k != "key"}))
        if path in self.overrides:
            value = self.overrides[path]
            if callable(value):
                value = value(params)
            if isinstance(value, list):
                value = value.pop(0) if len(value) > 1 else value[0]
            if isinstance(value, Exception):
                raise value
            return value
        return getattr(self, "r_" + path.strip("/").replace("/", "_"))(params)

    def r_v3_geocode_geo(self, p):
        table = {"新街口": ("118.784,32.041", "兴趣点", "025"), "仙林": ("118.930,32.110", "兴趣点", "025"),
                 "江宁": ("118.820,31.950", "兴趣点", "025"), "南京": ("118.796,32.059", "市", "025"),
                 "玄武湖": ("118.800,32.075", "兴趣点", "025"), "中山陵": ("118.848,32.060", "兴趣点", "025")}
        hit = next((v for k, v in table.items() if k in p["address"]), None)
        if not hit:
            return Resp({"status": "1", "count": "0", "geocodes": []})
        loc, level, code = hit
        return Resp({"status": "1", "geocodes": [{"location": loc, "level": level, "citycode": code, "adcode": "320100",
                                                  "city": "南京市", "province": "江苏省", "formatted_address": "江苏省南京市" + p["address"]}]})

    def r_v5_place_text(self, p):
        return Resp({"status": "1", "pois": [{"name": p["keywords"] + "景区", "location": "118.900,32.100",
                                               "cityname": "南京市", "adname": "玄武区", "citycode": "025", "adcode": "320102"}]})

    def r_v5_place_around(self, p):
        lng, lat = (float(x) for x in p["location"].split(","))
        return Resp({"status": "1", "pois": [
            poi(3, "luckin coffee 瑞幸咖啡(远店)", 900, f"{lng + .008:.6f},{lat:.6f}", "3.9", "18.00"),
            poi(1, "瑞幸咖啡(近店)", 100, f"{lng + .001:.6f},{lat:.6f}", "4.5", "13.00"),
            poi(2, "瑞幸咖啡(中店)", 400, f"{lng:.6f},{lat + .004:.6f}", "", "")]})

    def r_v5_direction_bicycling(self, p):
        return Resp({"status": "1", "route": {"paths": [{"distance": "8200", "cost": {"duration": "1980"}, "steps": [
            {"road_name": "北京东路", "step_distance": "5000", "polyline": "118.80,32.075;118.82,32.070;118.83,32.066"},
            {"road_name": "陵园路", "step_distance": "3200", "polyline": "118.83,32.066;118.848,32.060"}]}]}})

    def r_v3_distance(self, p):
        dest = [float(x) for x in p["destination"].split(",")]
        out = []
        for i, o in enumerate(p["origins"].split("|"), 1):
            lng, lat = (float(x) for x in o.split(","))
            d = M.haversine((lng, lat), tuple(dest)) * 1.3
            out.append({"origin_id": str(i), "distance": str(int(d)), "duration": str(int(d / 8))})
        return Resp({"status": "1", "results": out})

    def r_v5_direction_transit_integrated(self, p):
        return Resp({"status": "1", "route": {"transits": [{"distance": "9000", "cost": {"duration": "2400"}}]}})

    def r_v3_weather_weatherInfo(self, p):
        if p["extensions"] == "base":
            return Resp({"status": "1", "lives": [{"city": "南京市", "weather": "晴", "temperature": "18",
                                                   "winddirection": "东", "windpower": "≤3", "humidity": "40",
                                                   "reporttime": "2026-11-04 10:00:00"}]})
        return Resp({"status": "1", "forecasts": [{"city": "南京市", "casts": [
            {"date": "2026-11-04", "week": "3", "dayweather": "晴", "nightweather": "多云", "daytemp": "20",
             "nighttemp": "11", "daypower": "1-3"},
            {"date": "2026-11-05", "week": "4", "dayweather": "中雨", "nightweather": "小雨", "daytemp": "15",
             "nighttemp": "10", "daypower": "4-5"}]}]})

    def r_v3_staticmap(self, p):
        return Resp(content=PNG, ctype="image/png")


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class AmapFixture(GroupFixture):
    def setUp(self):
        super().setUp()
        p = patch.object(M, "DATA_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        self.http, self.clock, self.sent = FakeAmap(), Clock(), []
        self.client = M.Client(http=self.http, key=KEY, sleep=self.clock.sleep, clock=self.clock)
        self.service = M.Amap(self.client)

    def sender(self, goid, text, **kw):
        self.sent.append((text, kw))
        return True

    def runner(self, cfg=None):
        return M.Runner(A, amap=self.service, sender=self.sender, cfg={**M.DEFAULTS, **(cfg or {})})

    def assert_no_key(self, *texts):
        for text in texts:
            self.assertNotIn(KEY, str(text))
            self.assertNotIn("key=", str(text))


class ParseTests(unittest.TestCase):
    def test_natural_language_needs_at_and_slash_does_not(self):
        self.assertEqual(M.parse("南京新街口附近的瑞幸", True), {"kind": "nearby", "place": "南京新街口", "what": "瑞幸"})
        self.assertIsNone(M.parse("南京新街口附近的瑞幸", False))
        self.assertEqual(M.parse("/附近 新街口 瑞幸", False), {"kind": "nearby", "place": "新街口", "what": "瑞幸"})
        self.assertEqual(M.parse("从玄武湖骑到紫金山", True)["kind"], "route")
        self.assertEqual(M.parse("南京明天适合骑车吗", True), {"kind": "weather", "city": "南京"})
        mid = M.parse("中点 新街口 仙林 江宁 火锅", True)
        self.assertEqual((mid["kind"], mid["places"], mid["what"]), ("midpoint", ["新街口", "仙林", "江宁"], "火锅"))
        self.assertEqual(M.parse("/高德", False)["kind"], "help")

    def test_ordinary_chat_is_not_claimed(self):
        for text in ("咖啡因有什么影响", "想喝咖啡", "今天怎么这么累", "附近有瑞幸吗", "哈哈哈"):
            self.assertIsNone(M.parse(text, True), text)

    def test_category_mapping(self):
        self.assertEqual(M.category("瑞幸")["keywords"], "瑞幸咖啡")
        self.assertIn("050500", M.category("咖啡")["types"])
        self.assertEqual(M.category("火锅")["keywords"], "火锅")
        unknown = M.category("自行车维修")
        self.assertTrue(unknown["keywords"])

    def test_simplify_keeps_endpoints_and_caps_points(self):
        pts = [(118.0 + i * 0.001, 32.0 + (i % 7) * 0.0003) for i in range(800)]
        out = M.simplify(pts, 60)
        self.assertLessEqual(len(out), 60)
        self.assertEqual((out[0], out[-1]), (pts[0], pts[-1]))

    def test_fit_view_zooms_out_for_wider_spans(self):
        _, near = M.fit_view([(118.78, 32.04), (118.79, 32.045)], 512, 400)
        _, far = M.fit_view([(118.70, 31.90), (119.00, 32.20)], 512, 400)
        self.assertGreater(near, far)

    def test_ride_advice_is_labelled_and_reacts_to_rain_and_wind(self):
        self.assertIn("⚠️", M.ride_advice("小雨", 15, "≤3")[0])
        good = M.ride_advice("晴", 20, "≤3")[0]
        self.assertNotIn("⚠️", good)
        self.assertNotEqual(M.ride_advice("晴", 20, "7")[0], good)

    def test_links_never_contain_a_key(self):
        a = {"name": "新街口", "lng": 118.78, "lat": 32.04}
        link = M.marker_link({"id": "B0X", "name": "店", "location": "118.78,32.04"})
        self.assertTrue(link.startswith("https://uri.amap.com/marker?"))
        self.assert_ = None
        for value in (link, M.nav_link(a, {**a, "name": "德基"})):
            self.assertNotIn("key=", value)


class ClientTests(AmapFixture):
    def test_cache_hit_avoids_second_request_and_key_not_in_cache(self):
        self.service.geocode("南京新街口")
        self.service.geocode("南京新街口")
        self.assertEqual(self.client.calls, 1)
        for f in (self.root / "media" / "amap").rglob("*"):
            if f.is_file():
                self.assertNotIn(KEY, f.read_text(encoding="utf-8", errors="ignore"))
        self.assertEqual(M.load_usage()["cache_hits"], 1)

    def test_monthly_budget_refuses_before_request(self):
        usage = M.load_usage()
        usage["buckets"]["search"] = int(M.MONTHLY_LIMITS["search"] * M.QUOTA_RESERVE)
        M.atomic_write_json(M.usage_path(), usage)
        with self.assertRaises(M.AmapError) as ctx:
            self.service.around({"lng": 118.78, "lat": 32.04}, M.category("瑞幸"))
        self.assertEqual(ctx.exception.kind, "quota")
        self.assertEqual(self.http.calls, [])
        self.service.geocode("南京新街口")  # lbs bucket still fine
        self.assertEqual(M.load_usage()["buckets"]["lbs"], 1)

    def test_qps_throttle_waits_with_fake_clock(self):
        slept = []
        clock = Clock()
        for _ in range(4):
            M.throttle(sleep=lambda s: (slept.append(s), clock.sleep(s)), clock=clock)
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.9)

    def test_infocodes_map_to_error_kinds_without_leaking(self):
        cases = {"10001": "auth", "10003": "quota", "10044": "quota", "20000": "api"}
        for code, kind in cases.items():
            self.http.overrides["/v3/geocode/geo"] = Resp({"status": "0", "infocode": code, "info": "X"})
            with self.assertRaises(M.AmapError) as ctx:
                self.client.get("/v3/geocode/geo", {"address": "南京" + code})
            self.assertEqual(ctx.exception.kind, kind, code)
            self.assert_no_key(ctx.exception)

    def test_retry_once_on_qps_code(self):
        self.http.overrides["/v3/geocode/geo"] = [Resp({"status": "0", "infocode": "10020"}),
                                                  Resp({"status": "1", "geocodes": []})]
        self.assertEqual(self.client.get("/v3/geocode/geo", {"address": "x"})["status"], "1")
        self.assertEqual(self.client.calls, 2)

    def test_network_error_hides_url(self):
        self.http.overrides["/v3/geocode/geo"] = RuntimeError(f"failed https://restapi.amap.com/?key={KEY}")
        with self.assertRaises(M.AmapError) as ctx:
            self.client.get("/v3/geocode/geo", {"address": "x"})
        self.assertEqual(ctx.exception.kind, "network")
        self.assert_no_key(ctx.exception, repr(ctx.exception))

    def test_staticmap_downloads_png_and_rejects_non_image(self):
        path = self.service.staticmap(markers=[("mid", "0xE53935", "1", [(118.78, 32.04)])])
        self.assertTrue(Path(path).read_bytes().startswith(b"\x89PNG"))
        params = self.http.calls[-1][1]
        self.assertIn("markers", params)
        self.assertIn("zoom", params)
        self.http.overrides["/v3/staticmap"] = Resp({"status": "0", "infocode": "10001"})
        with self.assertRaises(M.AmapError):
            self.service.staticmap(markers=[("mid", "0xE53935", "2", [(118.70, 32.00)])])

    def test_missing_key_is_auth_error(self):
        with patch.dict("os.environ", {}, clear=True):
            client = M.Client(http=self.http)
            with self.assertRaises(M.AmapError) as ctx:
                client.get("/v3/geocode/geo", {"address": "x"})
        self.assertEqual(ctx.exception.kind, "auth")


class ServiceTests(AmapFixture):
    def test_coarse_geocode_falls_back_to_poi_search(self):
        place = self.service.geocode("南京")
        self.assertEqual([c[0] for c in self.http.calls], ["/v3/geocode/geo", "/v5/place/text"])
        self.assertEqual(place["name"], "南京")
        city = self.service.geocode("南京", coarse_ok=True)
        self.assertEqual(city["adcode"], "320100")

    def test_geocode_engine_error_still_finds_landmark(self):
        self.http.overrides["/v3/geocode/geo"] = Resp({"status": "0", "infocode": "30001", "info": "ENGINE_RESPONSE_DATA_ERROR"})
        place = self.service.geocode("南京玄武湖")
        self.assertEqual((place["lng"], place["lat"]), (118.9, 32.1))

    def test_coordinates_need_no_request(self):
        place = self.service.geocode("32.04,118.78")
        self.assertEqual((place["lng"], place["lat"]), (118.78, 32.04))
        self.assertEqual(self.http.calls, [])

    def test_around_sorts_by_distance(self):
        pois = self.service.around({"lng": 118.78, "lat": 32.04}, M.category("瑞幸"), limit=3)
        self.assertEqual([p["distance"] for p in pois], [100, 400, 900])
        self.assertEqual(self.http.calls[0][1]["sortrule"], "distance")


class RunnerTests(AmapFixture):
    def test_nearby_one_markdown_table_with_map(self):
        out = self.runner().handle(ALICE, "南京新街口附近的瑞幸", "m1", True)
        self.assertEqual(out, "sent:nearby")
        self.assertEqual(len(self.sent), 1)
        text, kw = self.sent[0]
        self.assertTrue(text.startswith(f'<qqbot-at-user id="{ALICE}" />'))
        self.assertIn("| # | 店名 | 距离 |", text)
        self.assertLess(text.index("近店"), text.index("中店"))
        self.assertIn("uri.amap.com/marker?poiid=B0TEST0001", text)
        self.assertTrue(kw["image"].endswith(".png"))
        self.assert_no_key(text, kw)

    def test_route_has_distance_time_nav_link_and_polyline(self):
        self.runner().handle(ALICE, "从南京玄武湖骑到中山陵", "m1", True)
        text, kw = self.sent[0]
        self.assertIn("8.2km", text)
        self.assertIn("uri.amap.com/navigation", text)
        self.assertIn("mode=ride", text)
        static = [c for c in self.http.calls if c[0] == "/v3/staticmap"][0][1]
        self.assertIn("paths", static)
        self.assertTrue(kw["image"])

    def test_weather_labels_advice(self):
        self.runner().handle(ALICE, "/天气 南京", "m1", False)
        text = self.sent[0][0]
        self.assertIn("骑行建议（仅供参考）", text)
        self.assertIn("11-05", text)
        self.assertNotIn("image", self.sent[0][1])

    def test_weather_without_city_asks(self):
        self.runner().handle(ALICE, "/天气", "m1", False)
        self.assertIn("哪个城市", self.sent[0][0])
        self.assertEqual(self.http.calls, [])

    def test_midpoint_picks_min_of_max(self):
        sent = self.runner().handle(ALICE, "中点 新街口 仙林 江宁 火锅", "m1", True)
        self.assertEqual(sent, "sent:midpoint")
        text = self.sent[0][0]
        self.assertIn("中点聚餐", text)
        self.assertIn("公交/地铁", text)
        dist_calls = [c for c in self.http.calls if c[0] == "/v3/distance"]
        self.assertEqual(len(dist_calls), 3)  # one matrix call per candidate covers every participant
        # The fake POIs are offset east/north of the centroid; check the winner really has the smallest worst leg.
        r = self.runner()
        people = [self.service.geocode(t) for t in ("新街口", "仙林", "江宁")]
        lng, lat, _ = M.centroid(people)
        cands = self.service.around({"lng": lng, "lat": lat}, M.category("火锅"), radius=2000, limit=8)
        worst = {p["name"]: max(l["duration"] for l in self.service.driving_matrix(people, dict(zip(("lng", "lat"), M.parse_loc(p["location"])))))
                 for p in cands}
        best = min(worst, key=worst.get)
        self.assertIn(f"推荐 **{best}**", text)
        del r

    def test_midpoint_too_spread_is_polite(self):
        def geo(params):  # the re-geocode in the majority city still lands in Beijing for 天安门
            loc, code = ("116.40,39.90", "010") if "天安门" in params["address"] else ("118.80,32.05", "025")
            return Resp({"status": "1", "geocodes": [{"location": loc, "level": "兴趣点", "citycode": code, "adcode": "1"}]})
        self.http.overrides["/v3/geocode/geo"] = geo
        out = self.runner().handle(ALICE, "中点 北京天安门 南京新街口", "m1", True)
        self.assertEqual(out, "error:limit")
        self.assertIn("离得太远", self.sent[0][0])

    def test_midpoint_regeocodes_homonym_in_majority_city(self):
        def geo(params):  # bare "新街口" first lands in Beijing; with city=025 it is the Nanjing one
            if "新街口" in params["address"] and params.get("city") != "025":
                return Resp({"status": "1", "geocodes": [{"location": "116.37,39.94", "level": "兴趣点", "citycode": "010", "adcode": "1"}]})
            return FakeAmap.r_v3_geocode_geo(self.http, params)
        self.http.overrides["/v3/geocode/geo"] = geo
        self.assertEqual(self.runner().handle(ALICE, "中点 新街口 仙林 江宁 火锅", "m1", True), "sent:midpoint")

    def test_api_error_reply_is_friendly(self):
        self.http.overrides["/v5/place/around"] = Resp({"status": "0", "infocode": "10003", "info": "DAILY_QUERY_OVER_LIMIT"})
        out = self.runner().handle(ALICE, "/附近 南京新街口 瑞幸", "m1", False)
        self.assertEqual(out, "error:quota")
        self.assertIn("额度", self.sent[0][0])
        self.assert_no_key(self.sent)

    def test_map_failure_still_sends_text(self):
        self.http.overrides["/v3/staticmap"] = Resp(status=500, content=b"err", ctype="text/html")
        self.runner().handle(ALICE, "/附近 南京新街口 瑞幸", "m1", False)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.sent[0][1].get("image"))

    def test_member_and_group_daily_limits_and_dedupe(self):
        r = self.runner({"max_requests_per_member_per_day": 2, "max_requests_per_group_per_day": 3})
        self.assertEqual(r.handle(ALICE, "/天气 南京", "m1", False), "sent:weather")
        self.assertEqual(r.handle(ALICE, "/天气 南京", "m1", False), "duplicate")
        r.handle(ALICE, "/天气 南京", "m2", False)
        self.assertEqual(r.handle(ALICE, "/天气 南京", "m3", False), "limited")
        r.handle(BOB, "/天气 南京", "m4", False)
        self.assertEqual(r.handle(BOB, "/天气 南京", "m5", False), "limited")
        self.assertIn("全群今天", self.sent[-1][0])

    def test_embed_failure_falls_back_to_text_then_image(self):
        calls = []

        def sender(goid, text, **kw):
            calls.append(kw)
            return not (kw.get("image") and len(calls) == 1)
        r = M.Runner(A, amap=self.service, sender=sender, cfg=dict(M.DEFAULTS))
        r.handle(ALICE, "/附近 南京新街口 瑞幸", "m1", False)
        self.assertEqual([bool(c.get("image")) for c in calls], [True, False, True])


class DispatchTests(AmapFixture):
    def dispatch(self, text, at_me=True, member=ALICE, msg="m1"):
        registry = PluginRegistry()
        self.addCleanup(registry.stop)
        return registry.dispatch({"group_openid": A, "post_type": "message", "at_me": at_me, "text": text,
                                  "member_openid": member, "msg_id": msg})

    def test_disabled_by_default(self):
        self.assertFalse(G.load_config(A)["features"]["amap"])
        self.assertFalse(G.default_config()["features"]["amap"])
        self.assertEqual(self.dispatch("南京新街口附近的瑞幸")["actions"][0]["type"], "routine.reply")
        self.assertNotEqual((self.dispatch("/天气 南京", False)["actions"] or [{}])[0].get("type"), "amap.query")

    def test_claims_when_enabled(self):
        G.set_plugin_enabled(A, "amap", True)
        out = self.dispatch("南京新街口附近的瑞幸")
        self.assertEqual((out["actions"][0]["type"], out["actions"][0]["plugin_id"]), ("amap.query", "amap"))
        self.assertEqual(self.dispatch("/骑行 玄武湖 中山陵", False)["actions"][0]["type"], "amap.query")
        self.assertEqual(self.dispatch("咖啡因有什么影响")["actions"][0]["type"], "routine.reply")
        self.assertEqual(self.dispatch("今天适合骑车吗")["actions"][0]["type"], "routine.reply")  # no city known

    def test_allowlist(self):
        G.set_plugin_enabled(A, "amap", True)
        G.update_plugin_binding(A, "amap", {"enabled": True, "config": {"allow_members": [BOB]}})
        self.assertEqual(self.dispatch("/天气 南京")["actions"][0]["type"], "routine.reply")
        self.assertEqual(self.dispatch("/天气 南京", member=BOB)["actions"][0]["type"], "amap.query")

    def test_defers_coffee_orders_to_luckin(self):
        G.set_plugin_enabled(A, "amap", True)
        G.set_plugin_enabled(A, "luckin_coffee", True)
        out = self.dispatch("新街口附近有便宜的瑞幸吗")
        self.assertNotEqual(out["actions"][0]["type"], "amap.query")
        self.assertEqual(self.dispatch("/附近 新街口 瑞幸", False)["actions"][0]["type"], "amap.query")

    def test_action_runner_starts_detached_worker(self):
        G.set_plugin_enabled(A, "amap", True)
        cfg = G.load_config(A)
        event = {"group_openid": A, "member_openid": ALICE, "msg_id": "m1", "text_only": "/天气 南京", "at_me": False}
        with patch.object(M.subprocess, "Popen") as popen:
            popen.return_value.pid = 4321
            out = action_runner.run_actions(event, [{"type": "amap.query", "group_openid": A, "plugin_id": "amap",
                                                     "config_revision": cfg["revision"]}])
        argv = popen.call_args.args[0]
        self.assertEqual(out[0]["result"], {"status": "started", "pid": 4321})
        self.assertEqual(argv[2:5], ["query", "--group", A])
        self.assertEqual(json.loads(argv[6])["text"], "/天气 南京")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assert_no_key(argv)


if __name__ == "__main__":
    unittest.main()
