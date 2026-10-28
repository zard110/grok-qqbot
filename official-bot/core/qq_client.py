"""QQ HTTP boundary. Message POSTs are never automatically replayed."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .settings import DATA_DIR

API = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"


class QQTransportError(RuntimeError):
    """A request may have reached QQ; callers must retain an unknown outcome."""


def classify_status(status: int) -> str:
    if status < 300:
        return "ok"
    if status in (401, 403):
        return "authentication"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "upstream"
    return "rejected"


class QQClient:
    def __init__(self, data_dir: Path = DATA_DIR, *, opener=None, clock=None):
        self.data_dir = Path(data_dir)
        self.opener = opener or urllib.request.urlopen
        self.clock = clock or time.monotonic
        self._token = ""
        self._expires = 0.0
        self._lock = threading.Lock()

    def invalidate_token(self):
        with self._lock:
            self._token, self._expires = "", 0.0

    def token(self) -> str:
        with self._lock:
            if self._token and self.clock() < self._expires:
                return self._token
            appid = (self.data_dir / "appid").read_text("utf-8").strip()
            secret = (self.data_dir / "appsecret").read_text("utf-8").strip()
            request = urllib.request.Request(TOKEN_URL,
                data=json.dumps({"appId": appid, "clientSecret": secret}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with self.opener(request, timeout=20) as response:
                    data = json.loads(response.read(65537))
                token = data.get("access_token")
                lifetime = float(data.get("expires_in", 0))
                if not isinstance(token, str) or not token or lifetime <= 0:
                    raise ValueError("invalid token response")
            except Exception:
                # Do not include upstream bodies/URLs or credentials in errors.
                raise QQTransportError("QQ token acquisition failed") from None
            self._token = token
            self._expires = self.clock() + max(1, lifetime - min(60, lifetime * .1))
            return token

    def request(self, method: str, path: str, payload=None, timeout: int = 60):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("QQ API path must be relative to the configured API")
        for attempt in range(2):
            request = urllib.request.Request(API + path,
                data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                headers={"Content-Type": "application/json", "Authorization": "QQBot " + self.token()},
                method=method)
            try:
                with self.opener(request, timeout=timeout) as response:
                    status, raw = response.status, response.read(2 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read(65536)
            except Exception:
                raise QQTransportError("QQ request outcome unknown; inspect delivery status before retry") from None
            if len(raw) > 2 * 1024 * 1024:
                raise QQTransportError("QQ response exceeded limit; request outcome unknown")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                body = {"error": "Non-JSON QQ response"}
            if status == 401:
                self.invalidate_token()
                if method == "GET" and attempt == 0:
                    continue
            return status, body
        raise AssertionError("unreachable")


client = QQClient()
