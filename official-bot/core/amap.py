"""AMap (高德) Web-service features for the ``amap`` plugin: 附近找店 / 骑行路线 / 骑行天气 / 中点聚餐.

Endpoints (checked against https://lbs.amap.com/api/webservice/guide, 2026-11):

* v3/geocode/geo(address, city) -> geocodes[{formatted_address, location "lng,lat", level, citycode, adcode, city}]
* v5/place/text(keywords, region, city_limit, page_size) / v5/place/around(location, keywords, types, radius,
  sortrule=distance, page_size<=25, show_fields=business) -> pois[{id, name, location, distance(m), address,
  business{rating, cost, opentime_today}}]
* v5/direction/bicycling(origin, destination, show_fields=cost,polyline) -> route.paths[{distance(m),
  cost{duration(s)}, steps[{road_name, step_distance, polyline "lng,lat;..."}]}]
* v5/direction/transit/integrated(origin, destination, city1, city2 = citycode, show_fields=cost)
  -> route.transits[{distance, cost{duration}}]
* v3/distance(origins "a|b|c" <=100, destination, type=1 driving) -> results[{origin_id, distance, duration}]
* v3/weather/weatherInfo(city=adcode, extensions=base|all) -> lives[...] / forecasts[{casts[...]}]
* v3/staticmap(size<=1024*1024, scale, markers<=10 "size,color,label:lng,lat|...", paths<=4
  "weight,color,transparency,,:lng,lat;...") -> PNG
* uri.amap.com/marker?poiid= / uri.amap.com/navigation?from=&to=&mode=ride (keyless links for users)

The key comes from ``AMAP_WEB_KEY`` / ``AMAP_KEY`` or ``official-bot/amap-key`` (chmod 600, gitignored).
It is only ever placed in request params sent to restapi.amap.com: never printed, logged, cached, put in
an exception message or a link. Static maps are downloaded here and sent to QQ as image files.

Budget guards: a file cache (default 10 min, weather 30 min), a cross-process 3 QPS limiter, a local
monthly counter per quota bucket (search 5000 / weather 5000 / lbs 150000, stop at 95%), and per-member /
per-group daily request limits. Every QQ message goes through send_group.py with ``--plugin-id amap``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

import groups as G
from .files import atomic_write_json, file_lock
from .settings import DATA_DIR, CODE_DIR

PLUGIN_ID = "amap"
API = "https://restapi.amap.com"
URI = "https://uri.amap.com"
SRC = "maxshenbi_qqbot"
SEND_CLI = CODE_DIR / "send_group.py"
WORKER_CLI = CODE_DIR / "amap_tool.py"
DEFAULTS = {
    "default_city": "", "max_results": 6, "radius": 3000, "cache_seconds": 600, "weather_cache_seconds": 1800,
    "max_requests_per_member_per_day": 10, "max_requests_per_group_per_day": 200, "midpoint_candidates": 4,
    "midpoint_transit": True, "max_participants": 6, "embed_image": True, "road_bike_kmh": 25,
}
MONTHLY_LIMITS = {"search": 5000, "weather": 5000, "lbs": 150000}
QUOTA_RESERVE = 0.95
QPS = 3
# infocodes: https://lbs.amap.com/api/webservice/guide/tools/info
QUOTA_CODES = {"10003", "10004", "10014", "10015", "10019", "10020", "10021", "10029", "10044", "10045"}
RETRY_CODES = {"10004", "10014", "10015", "10019", "10020", "10021"}
AUTH_CODES = {"10001", "10002", "10005", "10006", "10007", "10008", "10009", "10010", "10011", "10012", "10013"}
PRECISE_LEVELS = {"兴趣点", "门牌号", "单元号", "楼层", "房间", "道路", "道路交叉路口", "村庄", "热点商圈",
                  "公交站台、地铁站", "公交站台", "地铁站"}

# --------------------------------------------------------------------------- categories

CATEGORIES = [  # (pattern, label, keywords, types, emoji)
    (r"瑞幸|luckin", "瑞幸", "瑞幸咖啡", "050500", "☕"),
    (r"星巴克|starbucks", "星巴克", "星巴克", "050500", "☕"),
    (r"库迪|cotti", "库迪", "库迪咖啡", "050500", "☕"),
    (r"咖啡|coffee|拿铁|美式", "咖啡店", "", "050500", "☕"),
    (r"奶茶|茶饮|果茶|喜茶|蜜雪|霸王茶姬|古茗|茶百道", "奶茶", "奶茶", "050000", "🧋"),
    (r"火锅", "火锅", "火锅", "050000", "🍲"),
    (r"烧烤|烤肉|撸串", "烧烤", "烧烤", "050000", "🍢"),
    (r"面馆|拉面|面条|吃面", "面馆", "面", "050000", "🍜"),
    (r"甜品|蛋糕|面包|烘焙", "甜品", "", "050800|050900", "🍰"),
    (r"快餐|麦当劳|肯德基|汉堡", "快餐", "", "050300", "🍔"),
    (r"自行车|单车|车店|修车|骑行店", "自行车店", "自行车", "", "🚲"),
    (r"便利店|超市", "便利店", "", "060200|060400", "🏪"),
    (r"停车", "停车场", "", "150900", "🅿️"),
    (r"吃的|吃饭|美食|餐厅|饭店|饭馆|好吃|聚餐|吃啥|吃什么|^$", "美食", "", "050000", "🍽️"),
]


def category(text: str) -> dict:
    t = (text or "").strip().lower()
    for pattern, label, keywords, types, emoji in CATEGORIES:
        if re.search(pattern, t, re.I):
            return {"label": label, "keywords": keywords, "types": types, "emoji": emoji}
    word = re.sub(r"[的店吗呢啊？?！!。,，\s]+$", "", t)[:20] or "美食"
    return {"label": word, "keywords": word, "types": "", "emoji": "📍"}


# --------------------------------------------------------------------------- errors / key

class AmapError(Exception):
    """kind: auth | quota | rate | notfound | api | network | limit. Messages never contain the key."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def amap_key() -> str:
    for name in ("AMAP_WEB_KEY", "AMAP_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        return (DATA_DIR / "amap-key").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _work_dir() -> Path:
    path = DATA_DIR / "media" / "amap"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bucket_of(path: str) -> str:
    if "/place/" in path:
        return "search"
    if "/weather/" in path:
        return "weather"
    return "lbs"


# --------------------------------------------------------------------------- usage / qps

def usage_path() -> Path:
    return _work_dir() / "api-usage.json"


def load_usage(now: float | None = None) -> dict:
    month = time.strftime("%Y-%m", time.localtime(time.time() if now is None else now))
    try:
        usage = json.loads(usage_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    if usage.get("month") != month:
        usage = {"month": month, "buckets": {}, "endpoints": {}, "cache_hits": 0}
    return usage


def reserve_api_call(path: str, now: float | None = None) -> None:
    """Count one real request against its monthly bucket; refuse near the free quota."""
    bucket = bucket_of(path)
    with file_lock(_work_dir() / ".usage.lock", timeout=30):
        usage = load_usage(now)
        used = usage["buckets"].get(bucket, 0)
        if used >= int(MONTHLY_LIMITS[bucket] * QUOTA_RESERVE):
            raise AmapError("quota", f"local monthly {bucket} budget reached")
        usage["buckets"][bucket] = used + 1
        endpoint = path.strip("/")
        usage["endpoints"][endpoint] = usage["endpoints"].get(endpoint, 0) + 1
        atomic_write_json(usage_path(), usage)


def note_cache_hit() -> None:
    try:
        with file_lock(_work_dir() / ".usage.lock", timeout=10):
            usage = load_usage()
            usage["cache_hits"] = usage.get("cache_hits", 0) + 1
            atomic_write_json(usage_path(), usage)
    except Exception:  # noqa: BLE001 - statistics only
        pass


def throttle(sleep=time.sleep, clock=time.time) -> None:
    """Cross-process limiter: at most QPS requests in any rolling second."""
    path = _work_dir() / "qps.json"
    with file_lock(_work_dir() / ".qps.lock", timeout=30):
        try:
            stamps = [float(x) for x in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            stamps = []
        now = clock()
        stamps = [s for s in stamps if now - s < 1.0]
        if len(stamps) >= QPS:
            wait = 1.0 - (now - stamps[-QPS]) + 0.02
            if wait > 0:
                sleep(wait)
            now = clock()
            stamps = [s for s in stamps if now - s < 1.0]
        stamps.append(now)
        atomic_write_json(path, stamps[-QPS * 2:])


# --------------------------------------------------------------------------- client

class Client:
    """Thin REST client: cache -> monthly budget -> QPS -> request. Injectable ``http`` for tests."""

    def __init__(self, *, http=None, key: str | None = None, cache_seconds: int = 600, weather_cache_seconds: int = 1800,
                 sleep=time.sleep, clock=time.time):
        self.http = http or requests
        self._key = key
        self.cache_seconds, self.weather_cache_seconds = cache_seconds, weather_cache_seconds
        self.sleep, self.clock = sleep, clock
        self.calls = 0  # real network requests made by this client

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = amap_key()
        if not self._key:
            raise AmapError("auth", "no AMap key configured")
        return self._key

    def _cache_file(self, path: str, params: dict, suffix: str) -> Path:
        digest = hashlib.sha256(json.dumps([path, sorted(params.items())], ensure_ascii=False).encode()).hexdigest()[:32]
        directory = _work_dir() / "cache"
        directory.mkdir(exist_ok=True)
        return directory / f"{digest}.{suffix}"

    def _ttl(self, path: str) -> int:
        return self.weather_cache_seconds if bucket_of(path) == "weather" else self.cache_seconds

    def _request(self, path: str, params: dict):
        key = self.key  # AmapError("auth") before anything is counted
        reserve_api_call(path)
        throttle(self.sleep, self.clock)
        self.calls += 1
        try:
            return self.http.get(API + path, params={**params, "key": key}, timeout=12)
        except Exception as exc:  # noqa: BLE001 - never surface str(exc): it may contain the request URL
            raise AmapError("network", type(exc).__name__) from None

    def get(self, path: str, params: dict) -> dict:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        cache = self._cache_file(path, params, "json")
        ttl = self._ttl(path)
        if ttl > 0 and cache.exists() and time.time() - cache.stat().st_mtime < ttl:
            try:
                note_cache_hit()
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        for attempt in range(2):
            r = self._request(path, params)
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                raise AmapError("api", f"HTTP {getattr(r, 'status_code', '?')} non-JSON") from None
            if str(data.get("status")) == "1":
                if ttl > 0:
                    atomic_write_json(cache, data)
                return data
            code = str(data.get("infocode") or "")
            if code in RETRY_CODES and attempt == 0:
                self.sleep(1.2)
                continue
            if code in AUTH_CODES:
                raise AmapError("auth", f"infocode {code}")
            if code in QUOTA_CODES:
                raise AmapError("quota", f"infocode {code}")
            raise AmapError("api", f"infocode {code} {str(data.get('info') or '')[:60]}")
        raise AmapError("rate", "retry exhausted")

    def image(self, params: dict) -> str:
        """Download a static map PNG to a local file (never a URL with the key); returns its path."""
        out = self._cache_file("/v3/staticmap", params, "png")
        if out.exists() and out.stat().st_size > 0:
            note_cache_hit()
            return str(out)
        r = self._request("/v3/staticmap", params)
        kind = str((getattr(r, "headers", None) or {}).get("Content-Type", ""))
        body = getattr(r, "content", b"") or b""
        if getattr(r, "status_code", 0) != 200 or not kind.startswith("image/") or not body:
            raise AmapError("api", "static map not an image")
        out.write_bytes(body)
        return str(out)


# --------------------------------------------------------------------------- geometry helpers

def parse_loc(text: str) -> tuple[float, float]:
    lng, lat = (float(v) for v in str(text).split(";")[0].split(","))
    return lng, lat


def fmt_loc(lng: float, lat: float) -> str:
    return f"{lng:.6f},{lat:.6f}"


def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    lng1, lat1, lng2, lat2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def simplify(points: list[tuple[float, float]], max_points: int = 100) -> list[tuple[float, float]]:
    """Douglas-Peucker with a growing tolerance until the polyline fits ``max_points``."""
    if len(points) <= max_points:
        return points

    def dp(pts, eps):
        keep = [False] * len(pts)
        keep[0] = keep[-1] = True
        stack = [(0, len(pts) - 1)]
        while stack:
            s, e = stack.pop()
            (x1, y1), (x2, y2) = pts[s], pts[e]
            dx, dy = x2 - x1, y2 - y1
            norm = math.hypot(dx, dy) or 1e-12
            best, index = 0.0, -1
            for i in range(s + 1, e):
                x0, y0 = pts[i]
                d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / norm
                if d > best:
                    best, index = d, i
            if best > eps and index > 0:
                keep[index] = True
                stack += [(s, index), (index, e)]
        return [p for p, k in zip(pts, keep) if k]

    eps = 0.00005
    out = dp(points, eps)
    while len(out) > max_points:
        eps *= 1.6
        out = dp(points, eps)
    return out


def centroid(people: list[dict]) -> tuple[float, float, float]:
    """(lng, lat, farthest distance in metres from the centroid)."""
    lng = sum(p["lng"] for p in people) / len(people)
    lat = sum(p["lat"] for p in people) / len(people)
    return lng, lat, max(haversine((lng, lat), (p["lng"], p["lat"])) for p in people)


def fit_view(points: list[tuple[float, float]], width: int, height: int, pad: float = 0.85) -> tuple[tuple[float, float], int]:
    """Center + the largest zoom (<=17) that keeps every point inside ``pad`` of the image (Web Mercator)."""
    def merc_y(lat):
        lat = max(min(lat, 85.0), -85.0)
        return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    lngs, lats = [p[0] for p in points], [p[1] for p in points]
    lng = (min(lngs) + max(lngs)) / 2
    y = (merc_y(min(lats)) + merc_y(max(lats))) / 2
    lat = math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)
    span_x = max(max(lngs) - min(lngs), 1e-6) / 360.0
    span_y = max(merc_y(max(lats)) - merc_y(min(lats)), 1e-8) / (2 * math.pi)
    zoom = 17
    while zoom > 3 and (span_x * 256 * 2 ** zoom > width * pad or span_y * 256 * 2 ** zoom > height * pad):
        zoom -= 1
    return (lng, lat), max(3, zoom - 1)  # AMap static zoom renders one level tighter than 256px tiles


def km(meters) -> str:
    try:
        m = float(meters)
    except (TypeError, ValueError):
        return "—"
    return f"{m:.0f}m" if m < 1000 else f"{m / 1000:.1f}km"


def minutes(seconds) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "—"
    m = max(1, round(s / 60))
    return f"{m}分钟" if m < 60 else f"{m // 60}小时{m % 60:02d}分"


def shop_name(name: str) -> str:
    """'luckin coffee 瑞幸咖啡(新街口店)' -> '瑞幸(新街口店)'; keeps the branch visible in a narrow column."""
    n = re.sub(r"^luckin\s*coffee\s*", "", str(name or ""), flags=re.I).strip()
    n = re.sub(r"^(瑞幸咖啡|星巴克咖啡|库迪咖啡)", lambda m: m.group(1)[:2], n)
    return n.replace("（", "(").replace("）", ")")


def cell(text, limit: int = 18) -> str:
    t = re.sub(r"[|\n\r]+", " ", str(text or "")).strip()
    if isinstance(text, list) or t in ("[]", ""):
        return "—"
    return t if len(t) <= limit else t[:limit - 1] + "…"


def at(member: str) -> str:
    return f'<qqbot-at-user id="{member}" />' if member else ""


def marker_link(poi: dict) -> str:
    if poi.get("id"):
        return f"{URI}/marker?poiid={quote(str(poi['id']))}&src={SRC}&callnative=1"
    lng, lat = parse_loc(poi["location"])
    return f"{URI}/marker?position={lng:.6f},{lat:.6f}&name={quote(str(poi.get('name') or '')[:20])}&src={SRC}&callnative=1"


def nav_link(a: dict, b: dict, mode: str = "ride") -> str:
    def point(p):
        return f"{p['lng']:.6f},{p['lat']:.6f},{quote(str(p.get('name') or '')[:16])}"
    return f"{URI}/navigation?from={point(a)}&to={point(b)}&mode={mode}&src={SRC}&callnative=1"


# --------------------------------------------------------------------------- services

class Amap:
    def __init__(self, client: Client | None = None, *, default_city: str = ""):
        self.client = client or Client()
        self.default_city = default_city

    def geocode(self, text: str, city: str = "", *, coarse_ok: bool = False) -> dict:
        """Address / landmark -> {name, lng, lat, citycode, adcode, city, label}."""
        text = re.sub(r"\s+", "", (text or ""))[:60]
        m = re.fullmatch(r"(-?\d{1,3}\.\d+)[,，](-?\d{1,3}\.\d+)", text)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            lng, lat = (a, b) if abs(a) > abs(b) else (b, a)
            return {"name": text, "label": text, "lng": lng, "lat": lat, "citycode": "", "adcode": "", "city": ""}
        if len(text) < 2:
            raise AmapError("notfound", "address too short")
        city = city or self.default_city
        try:
            data = self.client.get("/v3/geocode/geo", {"address": text, "city": city})
        except AmapError as exc:
            # The geocoder answers 30001 (ENGINE_RESPONSE_DATA_ERROR) for some landmark names such as
            # "南京玄武湖"; the POI keyword search below still finds them.
            if exc.kind != "api":
                raise
            data = {}
        rows = [g for g in data.get("geocodes") or [] if g.get("location")]
        best = rows[0] if rows else None
        if best and (coarse_ok or best.get("level") in PRECISE_LEVELS):
            return self._geo(best, text)
        # Coarse geocode (区县/市) for a landmark name: one POI keyword search is more precise.
        try:
            pois = self.client.get("/v5/place/text", {"keywords": text, "region": city or (best or {}).get("citycode", ""),
                                                      "page_size": 1}).get("pois") or []
        except AmapError as exc:
            if exc.kind in ("auth",) or not best:
                raise
            pois = []
        if pois and pois[0].get("location"):
            p = pois[0]
            lng, lat = parse_loc(p["location"])
            return {"name": text, "label": f"{p.get('cityname') or ''}{p.get('adname') or ''} {p.get('name') or text}".strip(),
                    "lng": lng, "lat": lat, "citycode": p.get("citycode") or "", "adcode": p.get("adcode") or "",
                    "city": p.get("cityname") or ""}
        if best:
            return self._geo(best, text)
        raise AmapError("notfound", "address not found")

    @staticmethod
    def _geo(g: dict, text: str) -> dict:
        lng, lat = parse_loc(g["location"])
        city = g.get("city") if isinstance(g.get("city"), str) else ""
        return {"name": text, "label": g.get("formatted_address") or text, "lng": lng, "lat": lat,
                "citycode": g.get("citycode") if isinstance(g.get("citycode"), str) else "",
                "adcode": g.get("adcode") or "", "city": city or (g.get("province") or ""), "level": g.get("level")}

    def around(self, center: dict, cat: dict, *, radius: int = 3000, limit: int = 6) -> list[dict]:
        data = self.client.get("/v5/place/around", {
            "location": fmt_loc(center["lng"], center["lat"]), "keywords": cat.get("keywords"), "types": cat.get("types"),
            "radius": radius, "sortrule": "distance", "page_size": min(25, max(limit * 2, 10)), "show_fields": "business"})
        pois = []
        for p in data.get("pois") or []:
            if not p.get("location"):
                continue
            try:
                dist = float(p.get("distance") or 0)
            except ValueError:
                dist = haversine((center["lng"], center["lat"]), parse_loc(p["location"]))
            if not p.get("distance"):
                dist = haversine((center["lng"], center["lat"]), parse_loc(p["location"]))
            biz = p.get("business") if isinstance(p.get("business"), dict) else {}
            pois.append({"id": p.get("id") or "", "name": p.get("name") or "", "location": p["location"], "distance": dist,
                         "address": p.get("address") if isinstance(p.get("address"), str) else "",
                         "rating": biz.get("rating") or "", "cost": biz.get("cost") or "",
                         "citycode": p.get("citycode") if isinstance(p.get("citycode"), str) else "",
                         "opentime": biz.get("opentime_today") or "", "area": biz.get("business_area") or ""})
        pois.sort(key=lambda x: x["distance"])
        return pois[:limit]

    def bicycling(self, a: dict, b: dict) -> dict:
        data = self.client.get("/v5/direction/bicycling", {"origin": fmt_loc(a["lng"], a["lat"]),
                                                           "destination": fmt_loc(b["lng"], b["lat"]),
                                                           "show_fields": "cost,polyline"})
        paths = ((data.get("route") or {}).get("paths")) or []
        if not paths:
            raise AmapError("notfound", "no bicycling route")
        p = paths[0]
        points, roads = [], {}
        for step in p.get("steps") or []:
            for pair in str(step.get("polyline") or "").split(";"):
                if "," in pair:
                    points.append(parse_loc(pair))
            name = step.get("road_name") or ""
            if name and not isinstance(name, list):
                roads[name] = roads.get(name, 0) + float(step.get("step_distance") or 0)
        duration = (p.get("cost") or {}).get("duration") or p.get("duration")
        return {"distance": float(p.get("distance") or 0), "duration": float(duration or 0), "points": points,
                "roads": [r for r, _ in sorted(roads.items(), key=lambda kv: -kv[1])][:4]}

    def driving_matrix(self, origins: list[dict], dest: dict) -> list[dict]:
        data = self.client.get("/v3/distance", {"origins": "|".join(fmt_loc(o["lng"], o["lat"]) for o in origins),
                                                "destination": fmt_loc(dest["lng"], dest["lat"]), "type": 1})
        out = [None] * len(origins)
        for r in data.get("results") or []:
            i = int(r.get("origin_id") or 0) - 1
            if 0 <= i < len(origins):
                out[i] = {"distance": float(r.get("distance") or 0), "duration": float(r.get("duration") or 0)}
        if any(x is None for x in out):
            raise AmapError("api", "incomplete distance matrix")
        return out

    def transit(self, a: dict, b: dict) -> dict | None:
        city1 = a.get("citycode") or b.get("citycode")
        city2 = b.get("citycode") or city1
        if not city1:
            return None
        data = self.client.get("/v5/direction/transit/integrated", {
            "origin": fmt_loc(a["lng"], a["lat"]), "destination": fmt_loc(b["lng"], b["lat"]),
            "city1": city1, "city2": city2, "AlternativeRoute": 1, "show_fields": "cost"})
        transits = (data.get("route") or {}).get("transits") or []
        if not transits:
            return None
        t = transits[0]
        duration = (t.get("cost") or {}).get("duration") or t.get("duration")
        return {"distance": float(t.get("distance") or 0), "duration": float(duration or 0)}

    def weather(self, adcode: str) -> dict:
        live = (self.client.get("/v3/weather/weatherInfo", {"city": adcode, "extensions": "base"}).get("lives") or [{}])[0]
        fc = (self.client.get("/v3/weather/weatherInfo", {"city": adcode, "extensions": "all"}).get("forecasts") or [{}])[0]
        return {"live": live, "casts": fc.get("casts") or [], "city": live.get("city") or fc.get("city") or ""}

    def staticmap(self, *, markers: list[tuple[str, str, str, list[tuple[float, float]]]] = (),
                  paths: list[tuple[str, list[tuple[float, float]]]] = (), size: str = "512*400") -> str:
        """markers: (size, color, label, points); paths: (style, points). Returns a local PNG path."""
        params = {"size": size, "scale": 2}
        everything = [pt for *_, pts in markers for pt in pts] + [pt for _, pts in paths for pt in pts]
        if everything:
            width, height = (int(v) for v in size.split("*"))
            (lng, lat), zoom = fit_view(everything, width, height)
            params.update({"location": fmt_loc(lng, lat), "zoom": zoom})
        groups, count = [], 0
        for msize, color, label, points in markers:
            points = points[:max(0, 10 - count)]
            if not points:
                continue
            count += len(points)
            groups.append(f"{msize},{color},{label}:" + ";".join(f"{x:.6f},{y:.6f}" for x, y in points))
        if groups:
            params["markers"] = "|".join(groups)
        if paths:
            params["paths"] = "|".join(f"{style}:" + ";".join(f"{x:.5f},{y:.5f}" for x, y in pts) for style, pts in paths[:4])
        return self.client.image(params)


# --------------------------------------------------------------------------- intent parsing

PREFIX = re.compile(r"^/(附近|找店|周边|骑行|路线|骑车|天气|中点|聚餐|地图|高德)(?=\s|$)")
WEATHER_STOP = {"今天", "明天", "后天", "现在", "这边", "这里", "最近", "周末", "今晚", "明早", "外面", "我们", "你们", "那边", "本周"}
FOOD_TAIL = re.compile(r"^(吃什么|吃啥|吃饭|吃的|美食|火锅|烧烤|咖啡|奶茶|面馆|甜品|快餐|聚餐|餐厅|日料|西餐|川菜|湘菜|粤菜|小吃|自助)$")


def clean(text: str) -> str:
    text = re.sub(r"<qqbot-at-user[^>]*>|<@!?\w+>", " ", text or "")
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()


def _split_places(rest: str) -> list[str]:
    return [p for p in re.split(r"[\s、，,；;/]+|和|跟|与|还有", rest) if p]


def parse(text: str, at_me: bool) -> dict | None:
    """-> {kind: nearby|route|weather|midpoint|help, ...} or None (not ours). Pure."""
    t = clean(text)
    m = PREFIX.match(t)
    slash = bool(m)
    if not slash and not at_me:
        return None
    command = m.group(1) if m else ""
    rest = t[m.end():].strip() if m else t
    if command in ("地图", "高德"):
        return {"kind": "help"}
    # --- midpoint
    mm = re.match(r"^(?:中点聚餐|中点|中间点|找个?中间点?|折中)\s*[:：]?\s*(.*)$", rest) if not command else None
    if command in ("中点", "聚餐") or mm:
        body = mm.group(1) if mm else rest
        places = _split_places(body)
        what = ""
        if places and (FOOD_TAIL.match(places[-1]) or (len(places) > 2 and len(places[-1]) <= 3 and category(places[-1])["label"] != places[-1])):
            what = places.pop()
        if len(places) < 2:
            return {"kind": "help", "topic": "midpoint"} if slash else None
        return {"kind": "midpoint", "places": places[:8], "what": what}
    # --- route
    if command in ("骑行", "路线", "骑车"):
        parts = re.split(r"\s*(?:到|去|至|->|→|—|-)\s*|\s+", re.sub(r"^从", "", rest), maxsplit=1)
        parts = [p.strip(" ，,。？?") for p in parts if p.strip(" ，,。？?")]
        if len(parts) != 2:
            return {"kind": "help", "topic": "route"}
        return {"kind": "route", "from": parts[0], "to": re.sub(r"(怎么走|怎么骑|要?多久|多远|路线)$", "", parts[1])}
    rm = re.search(r"(?:^|从)(?P<a>[^，,。？?]{2,30}?)\s*(?:骑车|骑行|骑)\s*(?:到|去|至)\s*(?P<b>[^，,。？?]{2,30}?)\s*"
                   r"(?:要?多久|多远|怎么走|怎么骑|的?路线|几公里)?[?？。!！]*$", rest)
    if rm and not command:
        a = re.sub(r"^(我想|我要|想|要|帮我查?一?下|查一?下|从)", "", rm.group("a")).strip()
        return {"kind": "route", "from": a, "to": rm.group("b").strip()}
    # --- weather
    if command == "天气":
        city = re.sub(r"(的)?天气.*$|适合骑车吗?|能骑车吗?", "", rest).strip(" ，,。？?")
        return {"kind": "weather", "city": city}
    wm = re.search(r"(?P<city>[\u4e00-\u9fff]{2,8}?)(?:今天|明天|后天|这几天|周末|今晚|明早)?的?(?:天气|气温|下雨吗|会下雨)", rest) \
        or re.search(r"(?P<city>[\u4e00-\u9fff]{2,8}?)(?:今天|明天|周末)?(?:适合|能|可以)骑(?:车|行)", rest)
    if wm and not command:
        city = re.sub(r"^(查一?下|看看|帮我查?|请问|那|在)", "", wm.group("city"))
        city = "" if city in WEATHER_STOP else city
        return {"kind": "weather", "city": city}
    if not command and re.search(r"(今天|明天|周末|现在)?(适合|能|可以)骑(车|行)吗|骑行天气|天气怎么样", rest):
        return {"kind": "weather", "city": ""}
    # --- nearby
    if command in ("附近", "找店", "周边"):
        bits = rest.split()
        if not bits:
            return {"kind": "help", "topic": "nearby"}
        nm = re.match(r"^(?P<place>.+?)(?:附近|周边|周围|旁边)(?:的|有什么|有啥|有没有)?(?P<what>.*)$", "".join(bits))
        if nm:
            return {"kind": "nearby", "place": nm.group("place"), "what": nm.group("what")}
        return {"kind": "nearby", "place": bits[0], "what": " ".join(bits[1:])}
    nm = re.search(r"(?:我在)?(?P<place>[^，,。？?\s]{2,30}?)(?:附近|周边|周围|旁边|一带)(?:的|有什么|有啥|有没有|哪里有|哪家|找个?|推荐个?|最近的)*"
                   r"(?P<what>[^，,。？?]{0,12}?)(?:店)?(?:吗|呢|啊|吧)?[?？。!！]*$", rest)
    if nm:
        what = re.sub(r"^(好吃的|便宜的|好喝的)$", "吃的", nm.group("what").strip())
        place = re.sub(r"^(帮我找|帮我查|找一?下|查一?下|看看|请问)", "", nm.group("place"))
        return {"kind": "nearby", "place": place, "what": what}
    return None


# --------------------------------------------------------------------------- rendering

HELP = {
    "nearby": "/附近 <地址> [瑞幸/咖啡/吃的/火锅…]　例：/附近 南京新街口 瑞幸",
    "route": "/骑行 <起点> 到 <终点>　例：/骑行 玄武湖 到 紫金山",
    "weather": "/天气 <城市>　例：/天气 南京（会附骑行建议）",
    "midpoint": "/中点 <地址1> <地址2> [...] [吃什么]　例：/中点 新街口 仙林 江宁 火锅",
}


def render_help(member: str, topic: str = "") -> str:
    lines = [f"{at(member)} 🗺️ 高德小工具用法（也可以直接 @我 说人话，比如「新街口附近的瑞幸」「从玄武湖骑到紫金山」）", ""]
    for key in ([topic] if topic in HELP else HELP):
        lines.append("- " + HELP[key])
    return "\n".join(lines)


def render_nearby(member: str, place: dict, cat: dict, pois: list[dict], radius: int) -> str:
    head = f"{at(member)} {cat['emoji']} **{place['name']}** 附近的{cat['label']}（按距离，{km(radius)}内）"
    if not pois:
        return head + f"\n\n😶 {km(radius)}内没找到，换个地标或者关键词试试？"
    has_rating = any(p.get("rating") for p in pois)
    has_cost = any(p.get("cost") for p in pois)
    cols = ["#", "店名", "距离", "地址"] + (["评分"] if has_rating else []) + (["人均"] if has_cost else []) + ["导航"]
    lines = [head, f"📍 定位：{cell(place['label'], 40)}", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for i, p in enumerate(pois, 1):
        row = [str(i), cell(shop_name(p["name"]), 18), km(p["distance"]), cell(re.sub(r"^\S{2,6}街道", "", p["address"]), 14)]
        if has_rating:
            row.append(cell(p.get("rating"), 4))
        if has_cost:
            row.append(f"¥{float(p['cost']):.0f}" if re.fullmatch(r"\d+(\.\d+)?", str(p.get("cost") or "")) else "—")
        row.append(f"[去这]({marker_link(p)})")
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", "🗺️ 地图上的数字 = 表格编号，点「去这」用高德导航"]
    return "\n".join(lines)


def render_route(member: str, a: dict, b: dict, route: dict, road_kmh: int) -> str:
    dist_km = route["distance"] / 1000
    fast = dist_km / max(road_kmh, 1) * 3600
    lines = [f"{at(member)} 🚴 **{a['name']} → {b['name']}** 骑行路线", "",
             "| 项目 | 数值 |", "|---|---|",
             f"| 距离 | **{km(route['distance'])}** |",
             f"| 高德骑行用时 | {minutes(route['duration'])}（通勤车速） |",
             f"| 公路车估算 | {minutes(fast)}（按 {road_kmh}km/h） |"]
    if route.get("roads"):
        lines.append(f"| 主要道路 | {cell('、'.join(route['roads']), 40)} |")
    lines += ["", f"🧭 [打开高德骑行导航]({nav_link(a, b, 'ride')})",
              "⚠️ 高德不提供爬升数据；起终点：" + cell(a["label"], 24) + " → " + cell(b["label"], 24)]
    return "\n".join(lines)


def _power(text) -> int:
    nums = [int(n) for n in re.findall(r"\d+", str(text or ""))]
    return max(nums) if nums else 0


def ride_advice(weather: str, temp, power) -> tuple[str, str]:
    """-> (emoji verdict, reason). Rule of thumb, always labelled as advice."""
    w = str(weather or "")
    try:
        t = float(temp)
    except (TypeError, ValueError):
        t = None
    p = _power(power)
    if re.search(r"雷|暴|冰雹|雪|大雨|中雨|冻雨|沙尘|台风", w):
        return "❌", f"{w}，不建议户外骑，练功房 Zwift 见"
    if "雨" in w:
        return "⚠️", f"{w}，路面湿滑，户外骑要谨慎（挡泥板+尾灯）"
    if p >= 6:
        return "⚠️", f"风力 {power} 级，侧风大，注意安全"
    if t is not None and t >= 35:
        return "⚠️", f"{t:.0f}℃ 高温，避开中午，多带水和电解质"
    if t is not None and t <= 0:
        return "⚠️", f"{t:.0f}℃ 低温，注意防冻和路面结冰"
    if re.search(r"霾|雾", w):
        return "⚠️", f"{w}，能见度和空气一般，开灯、少高强度"
    if p >= 4:
        return "🙂", f"风力 {power} 级，可以骑，逆风段省着点"
    return "✅", "天气不错，适合出门骑一圈"


def render_weather(member: str, city: str, data: dict) -> str:
    live, casts = data.get("live") or {}, data.get("casts") or []
    lines = [f"{at(member)} 🌤️ **{data.get('city') or city}** 天气"]
    if live:
        verdict, reason = ride_advice(live.get("weather"), live.get("temperature"), live.get("windpower"))
        lines += [f"现在：{live.get('weather', '—')} {live.get('temperature', '—')}℃ · {live.get('winddirection', '')}风 "
                  f"{live.get('windpower', '—')}级 · 湿度 {live.get('humidity', '—')}%"
                  + (f"（{str(live.get('reporttime'))[11:16]} 发布）" if live.get("reporttime") else ""),
                  "", f"🚴 骑行建议（仅供参考）：{verdict} {reason}"]
    if casts:
        week = "一二三四五六日"
        lines += ["", "| 日期 | 白天 | 夜间 | 温度 | 风力 | 骑行 |", "|---|---|---|---|---|---|"]
        for c in casts[:4]:
            w = c.get("week")
            day = f"{str(c.get('date', ''))[5:]} 周{week[int(w) - 1] if str(w).isdigit() and 1 <= int(w) <= 7 else '?'}"
            verdict, _ = ride_advice(c.get("dayweather"), c.get("daytemp"), c.get("daypower"))
            lines.append(f"| {day} | {c.get('dayweather', '—')} | {c.get('nightweather', '—')} | "
                         f"{c.get('nighttemp', '—')}~{c.get('daytemp', '—')}℃ | {c.get('daypower', '—')}级 | {verdict} |")
    return "\n".join(lines)


def render_midpoint(member: str, people: list[dict], cat: dict, ranked: list[dict], transit: list | None) -> str:
    best = ranked[0]
    lines = [f"{at(member)} 🤝 **中点聚餐**：{len(people)} 个人，推荐 **{best['name']}**",
             f"📍 {cell(best.get('address') or '', 30)}" + (f" · ⭐{best['rating']}" if best.get("rating") else "")
             + (f" · 人均¥{float(best['cost']):.0f}" if re.fullmatch(r"\d+(\.\d+)?", str(best.get("cost") or "")) else "") + f" · [去这]({marker_link(best)})",
             "", "| 出发地 | 开车 | " + ("公交/地铁 | " if transit else "") + "距离 |", "|---|---|" + ("---|" if transit else "") + "---|"]
    for i, person in enumerate(people):
        leg = best["legs"][i]
        row = [f"{i + 1}. {cell(person['name'], 10)}", minutes(leg["duration"])]
        if transit:
            row.append(minutes(transit[i]["duration"]) if transit[i] else "—")
        row.append(km(leg["distance"]))
        lines.append("| " + " | ".join(row) + " |")
    lines += ["", f"⚖️ 选法：先取大家位置的中心点，搜附近{cat['label']}，再比每家「最远那个人开车要多久」，挑最小的（最公平）。"]
    if len(ranked) > 1:
        alts = "；".join(f"{chr(66 + n)}. {cell(r['name'], 14)}（最远 {minutes(r['worst'])}）" for n, r in enumerate(ranked[1:4]))
        lines.append("备选：" + alts)
    lines.append("🗺️ 地图：蓝色数字=出发地，红 A=推荐，橙 B/C/D=备选")
    return "\n".join(lines)


# --------------------------------------------------------------------------- limits / state

def plugin_config(cfg: dict | None) -> dict:
    custom = ((cfg or {}).get("plugin_bindings") or {}).get(PLUGIN_ID, {}).get("config", {}) or {}
    return {**DEFAULTS, **{k: v for k, v in custom.items() if k in DEFAULTS}}


def admit_member(goid: str, member: str, cfg: dict, now: float | None = None, *, event: str = "") -> str:
    """'' if admitted; else 'member' / 'group' / 'duplicate'. Per local day."""
    day = time.strftime("%Y-%m-%d", time.localtime(time.time() if now is None else now))
    path = G.group_dir(goid) / "amap-usage.json"
    with file_lock(G.group_dir(goid) / ".amap.lock", timeout=30):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("date") != day:
            state = {"date": day, "total": 0, "members": {}, "events": []}
        if event and event in state["events"]:
            return "duplicate"
        if state["total"] >= int(cfg["max_requests_per_group_per_day"]):
            return "group"
        if state["members"].get(member, 0) >= int(cfg["max_requests_per_member_per_day"]):
            return "member"
        state["total"] += 1
        state["members"][member] = state["members"].get(member, 0) + 1
        if event:
            state["events"] = (state["events"] + [event])[-500:]
        atomic_write_json(path, state)
        return ""


# --------------------------------------------------------------------------- sending

def send(goid: str, markdown: str, *, msg_id: str = "", image: str = "", action_id: str = "") -> bool:
    argv = [sys.executable, str(SEND_CLI), "--group", goid, "--text", markdown, "--format", "auto",
            "--trigger", "command", "--plugin-id", PLUGIN_ID]
    if image:
        argv += ["--image", image]
    if msg_id:
        argv += ["--msg-id", msg_id]
    if action_id:
        argv += ["--action-id", action_id]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=str(CODE_DIR))
        if proc.returncode != 0 and msg_id:
            # The passive reply window may have closed: retry once as a normal group message.
            argv = argv[:argv.index("--msg-id")] + argv[argv.index("--msg-id") + 2:]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, cwd=str(CODE_DIR))
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


# --------------------------------------------------------------------------- runner

ERROR_TEXT = {
    "auth": "高德服务暂时不可用（Key 配置有问题，已记录，等管理员处理）",
    "quota": "本月高德免费额度快用完了，这个功能先歇一歇～",
    "rate": "高德那边有点忙，过一分钟再试试～",
    "notfound": "没找到这个地方，换个更具体的地标（比如「南京新街口」）试试？",
    "network": "高德那边暂时没响应，稍后再试试～",
    "api": "高德那边返回出错了，换个说法或者稍后再试～",
}


class Runner:
    """One request for one member. Injectable ``amap`` / ``sender`` for tests and dry runs."""

    def __init__(self, goid: str, *, amap: Amap | None = None, sender=None, cfg: dict | None = None):
        self.goid = goid
        self.cfg = cfg or plugin_config(G.load_config(goid) if goid else None)
        self.amap = amap or Amap(Client(cache_seconds=int(self.cfg["cache_seconds"]),
                                        weather_cache_seconds=int(self.cfg["weather_cache_seconds"])),
                                 default_city=str(self.cfg.get("default_city") or ""))
        self.sender = sender or send

    def say(self, text: str, msg_id: str, key: str, image: str = "") -> bool:
        action = f"amap:{msg_id}:{key}" if msg_id else ""
        if image and self.cfg.get("embed_image", True) and len(text) <= 1700:
            if self.sender(self.goid, text, msg_id=msg_id, image=image, action_id=action):
                return True
        ok = self.sender(self.goid, text, msg_id=msg_id, action_id=action + (":t" if action else ""))
        if image:
            self.sender(self.goid, "🗺️", msg_id=msg_id, image=image, action_id=action + (":i" if action else ""))
        return ok

    def build(self, member: str, req: dict) -> tuple[str, str]:
        """-> (markdown, image path or ''). Raises AmapError."""
        kind = req.get("kind")
        if kind == "help":
            return render_help(member, req.get("topic", "")), ""
        if kind == "nearby":
            place = self.amap.geocode(req["place"])
            cat = category(req.get("what", ""))
            radius = int(self.cfg["radius"])
            pois = self.amap.around(place, cat, radius=radius, limit=int(self.cfg["max_results"]))
            image = ""
            if pois:
                image = self._safe_map(markers=[("mid", "0x1E88E5", "我", [(place["lng"], place["lat"])])] + [
                    ("large", "0xE53935", str(i), [parse_loc(p["location"])]) for i, p in enumerate(pois[:9], 1)])
            return render_nearby(member, place, cat, pois, radius), image
        if kind == "route":
            a = self.amap.geocode(req["from"])
            b = self.amap.geocode(req["to"], a.get("citycode") or "")
            if haversine((a["lng"], a["lat"]), (b["lng"], b["lat"])) > 300_000:
                raise AmapError("limit", "too far")
            route = self.amap.bicycling(a, b)
            pts = simplify(route["points"], 90) if route["points"] else [(a["lng"], a["lat"]), (b["lng"], b["lat"])]
            image = self._safe_map(markers=[("mid", "0x22AA22", "起", [(a["lng"], a["lat"])]),
                                            ("mid", "0xE53935", "终", [(b["lng"], b["lat"])])],
                                   paths=[("6,0x1565C0,0.9,,", pts)])
            return render_route(member, a, b, route, int(self.cfg["road_bike_kmh"])), image
        if kind == "weather":
            city = req.get("city") or str(self.cfg.get("default_city") or "")
            if not city:
                return f"{at(member)} 想查哪个城市？比如「/天气 南京」或者「南京明天适合骑车吗」", ""
            place = self.amap.geocode(city, coarse_ok=True)
            if not place.get("adcode"):
                raise AmapError("notfound", "no adcode")
            return render_weather(member, city, self.amap.weather(place["adcode"])), ""
        if kind == "midpoint":
            return self.midpoint(member, req)
        raise AmapError("api", "unknown request")

    def midpoint(self, member: str, req: dict) -> tuple[str, str]:
        places = req["places"][: int(self.cfg["max_participants"])]
        people = []
        for i, text in enumerate(places):
            people.append(self.amap.geocode(text, people[0].get("citycode", "") if people else ""))
        lng, lat, spread = centroid(people)
        if spread > 150_000:
            # Bare names like "新街口" exist in several cities: re-geocode the odd ones out in the
            # city most participants landed in, then give up if it is still unreasonable.
            cities = [p.get("citycode") or p.get("city") for p in people if p.get("citycode") or p.get("city")]
            common = max(cities, key=cities.count) if cities else ""  # ties: first person wins
            if common:
                people = [p if (p.get("citycode") or p.get("city")) == common else self.amap.geocode(text, common)
                          for p, text in zip(people, places)]
                lng, lat, spread = centroid(people)
            if spread > 150_000:
                raise AmapError("limit", "too spread")
        cat = category(req.get("what") or "吃的")
        center = {"lng": lng, "lat": lat, "name": "中心点"}
        k = int(self.cfg["midpoint_candidates"])
        pois = self.amap.around(center, cat, radius=2000, limit=k * 2)
        if not pois:
            pois = self.amap.around(center, cat, radius=5000, limit=k * 2)
        if not pois:
            return f"{at(member)} 中心点附近没找到{cat['label']}，换个吃的或者换几个地址试试？", ""
        # Prefer rated places among the nearest few, then the rest by distance.
        pois = sorted(pois, key=lambda p: (not p.get("rating"), p["distance"]))[:k]
        ranked = []
        for p in pois:
            plng, plat = parse_loc(p["location"])
            legs = self.amap.driving_matrix(people, {"lng": plng, "lat": plat})
            ranked.append({**p, "lng": plng, "lat": plat, "legs": legs, "worst": max(l["duration"] for l in legs),
                           "total": sum(l["duration"] for l in legs)})
        ranked.sort(key=lambda r: (r["worst"], r["total"]))
        best = ranked[0]
        transit = None
        if self.cfg.get("midpoint_transit"):
            transit = []
            for person in people:
                try:
                    transit.append(self.amap.transit(person, best))
                except AmapError as exc:
                    if exc.kind in ("quota", "auth"):
                        raise
                    transit.append(None)
            if not any(transit):
                transit = None
        markers = [("mid", "0x1E88E5", str(i + 1), [(p["lng"], p["lat"])]) for i, p in enumerate(people[:6])]
        markers.append(("large", "0xE53935", "A", [(best["lng"], best["lat"])]))
        markers += [("mid", "0xFB8C00", chr(66 + n), [(r["lng"], r["lat"])]) for n, r in enumerate(ranked[1:4])]
        image = self._safe_map(markers=markers)
        return render_midpoint(member, people, cat, ranked, transit), image

    def _safe_map(self, **kw) -> str:
        try:
            return self.amap.staticmap(**kw)
        except AmapError as exc:
            if exc.kind in ("auth",):
                raise
            return ""  # the text answer still goes out

    def handle(self, member: str, text: str, msg_id: str, at_me: bool) -> str:
        req = parse(text, at_me)
        if not req or not member:
            return "ignored"
        blocked = admit_member(self.goid, member, self.cfg, event=msg_id) if req["kind"] != "help" else ""
        if blocked == "duplicate":
            return "duplicate"
        if blocked:
            limit = self.cfg["max_requests_per_member_per_day"] if blocked == "member" else self.cfg["max_requests_per_group_per_day"]
            who = "你今天" if blocked == "member" else "全群今天"
            self.say(f"{at(member)} {who}已经查了 {limit} 次地图啦，明天再来～", msg_id, "limit")
            return "limited"
        try:
            markdown, image = self.build(member, req)
        except AmapError as exc:
            if exc.kind == "limit":
                msg = "这几个地方离得太远了，超出了骑行/聚餐的合理范围～地名重名的话加上城市试试（比如「南京新街口」）"
            else:
                msg = ERROR_TEXT.get(exc.kind, ERROR_TEXT["api"])
            self.say(f"{at(member)} {msg}", msg_id, "err")
            return "error:" + exc.kind
        self.say(markdown, msg_id, "ok", image)
        return "sent:" + req["kind"]


# --------------------------------------------------------------------------- process plumbing

def start_query(goid: str, event: dict) -> dict:
    """Plugin action entry: run detached so the gateway never blocks on AMap / QQ uploads."""
    payload = {"member": str(event.get("member_openid") or ""),
               "text": str(event.get("text_only") or event.get("text") or event.get("raw_message") or "")[:500],
               "msg_id": str(event.get("msg_id") or event.get("event_id") or ""), "at_me": bool(event.get("at_me"))}
    log_dir = _work_dir()
    with open(log_dir / "worker.out", "ab") as log:
        proc = subprocess.Popen([sys.executable, str(WORKER_CLI), "query", "--group", goid, "--event",
                                 json.dumps(payload, ensure_ascii=False)], stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=str(CODE_DIR), start_new_session=True)
    return {"status": "started", "pid": proc.pid}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="amap plugin worker / admin CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("query", help="(spawned by the gateway) answer one group message")
    q.add_argument("--group", required=True)
    q.add_argument("--event", required=True, help="JSON {member, text, msg_id, at_me}")
    d = sub.add_parser("dry-run", help="build the reply for TEXT without sending (prints markdown + image path)")
    d.add_argument("text")
    d.add_argument("--group", default="")
    d.add_argument("--member", default="MEMBER_OPENID")
    d.add_argument("--out", default="", help="copy markdown/png to this path prefix")
    sub.add_parser("usage", help="print this month's local API counters")
    sub.add_parser("check", help="one geocode call to verify the key (never prints it)")
    for name in ("enable", "disable"):
        e = sub.add_parser(name, help=f"{name} the plugin for one group")
        e.add_argument("--group", required=True)
    a = ap.parse_args(argv)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        if a.cmd == "usage":
            usage = load_usage()
            usage["limits"] = {k: int(v * QUOTA_RESERVE) for k, v in MONTHLY_LIMITS.items()}
            print(json.dumps(usage, ensure_ascii=False, indent=1))
            return 0
        if a.cmd == "check":
            if not amap_key():
                print(json.dumps({"ok": False, "error": "no key (AMAP_WEB_KEY or official-bot/amap-key)"}))
                return 4
            geo = Amap().geocode("南京新街口", "南京")
            print(json.dumps({"ok": True, "sample": geo["label"]}, ensure_ascii=False))
            return 0
        if a.cmd in ("enable", "disable"):
            goid = G.resolve(a.group)
            G.set_plugin_enabled(goid, PLUGIN_ID, a.cmd == "enable")
            print(json.dumps({"ok": True, "group": G.load_config(goid).get("name"),
                              "amap": G.plugin_enabled(goid, PLUGIN_ID)}, ensure_ascii=False))
            return 0
        if a.cmd == "dry-run":
            goid = G.resolve(a.group) if a.group else ""
            runner = Runner(goid, cfg=plugin_config(G.load_config(goid)) if goid else dict(DEFAULTS))
            req = parse(a.text, True)
            if not req:
                print(json.dumps({"ok": False, "error": "not an amap request"}, ensure_ascii=False))
                return 1
            markdown, image = runner.build(a.member, req)
            if a.out:
                Path(a.out + ".md").write_text(markdown + "\n", encoding="utf-8")
                if image:
                    Path(a.out + ".png").write_bytes(Path(image).read_bytes())
            print(json.dumps({"ok": True, "request": req, "chars": len(markdown), "image": image,
                              "api_calls": runner.amap.client.calls}, ensure_ascii=False))
            print(markdown)
            return 0
        goid = G.resolve(a.group)
        if not G.plugin_enabled(goid, PLUGIN_ID):
            print(f"[{stamp}] plugin disabled; nothing done")
            return 0
        ev = json.loads(a.event)
        out = Runner(goid).handle(ev.get("member", ""), ev.get("text", ""), ev.get("msg_id", ""), bool(ev.get("at_me")))
        print(f"[{stamp}] query -> {out}")
        return 0
    except AmapError as exc:
        print(f"[{stamp}] {a.cmd} failed: {exc.kind} ({str(exc)[:80]})")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[{stamp}] {a.cmd} crashed: {type(exc).__name__}")
        return 2
