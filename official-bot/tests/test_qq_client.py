import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.qq_client import QQClient, QQTransportError


class Response(io.BytesIO):
    status = 200
    def __init__(self, value):
        super().__init__(json.dumps(value).encode())


class QQClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / "appid").write_text("test-id")
        (self.path / "appsecret").write_text("fixture-secret")

    def test_token_expiry_uses_reported_lifetime(self):
        calls, clock = [], [10.0]
        def opener(request, timeout):
            calls.append(request.full_url)
            return Response({"access_token": "fixture", "expires_in": 10})
        client = QQClient(self.path, opener=opener, clock=lambda: clock[0])
        self.assertEqual(client.token(), "fixture")
        clock[0] = 18
        client.token()
        self.assertEqual(len(calls), 1)
        clock[0] = 19
        client.token()
        self.assertEqual(len(calls), 2)

    def test_post_is_never_replayed_after_401_or_timeout(self):
        calls = []
        def opener(request, timeout):
            if "getAppAccessToken" in request.full_url:
                return Response({"access_token": "fixture", "expires_in": 100})
            calls.append(request.method)
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        client = QQClient(self.path, opener=opener)
        self.assertEqual(client.request("POST", "/messages", {})[0], 401)
        self.assertEqual(calls, ["POST"])
        client.opener = lambda *_args, **_kw: (_ for _ in ()).throw(TimeoutError("fixture-secret"))
        with self.assertRaises(QQTransportError) as caught:
            client.token()
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_get_refreshes_expired_auth_once(self):
        requests = []
        def opener(request, timeout):
            if "getAppAccessToken" in request.full_url:
                return Response({"access_token": "fixture", "expires_in": 100})
            requests.append(request.method)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 401, "", {}, io.BytesIO(b"{}"))
            return Response({"url": "wss://example.test"})
        self.assertEqual(QQClient(self.path, opener=opener).request("GET", "/gateway")[0], 200)
        self.assertEqual(requests, ["GET", "GET"])
