"""Bounded media IO; external filenames never select a local destination."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .files import atomic_write_bytes, confined_path

MAX_MEDIA_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000

EGRESS_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _allowed_peer_ip(addr: str, hostname: str = "") -> bool:
    ip = ipaddress.ip_address(addr)
    if ip.is_global:
        return True
    try:
        ipaddress.ip_address(hostname.strip("[]"))
        return False
    except ValueError:
        pass
    if hostname.lower() in {"localhost", "localhost.localdomain"} or hostname.lower().endswith((".local", ".internal")):
        return False
    return ip in EGRESS_FAKE_IP_NET


class PublicResponse:
    def __init__(self, response, connection):
        self.response, self.connection = response, connection
        self.status, self.headers = response.status, response.headers

    def read(self, amount=-1):
        return self.response.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.response.close()
        self.connection.close()


def safe_open(url, *, data=None, method="GET", headers=None, timeout=4):
    """Check the connected peer before sending HTTP; no environment proxy or DNS-rebinding bypass."""
    for _ in range(6):
        validate_url(url)
        parsed = urllib.parse.urlsplit(url)
        connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=timeout)
        try:
            connection.connect()
            peer = connection.sock.getpeername()[0]
            if not _allowed_peer_ip(peer, parsed.hostname or ""):
                raise ValueError("Media connection resolved to a non-public peer")
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.request(method, path, body=data, headers=headers or {})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                if method != "GET":
                    raise ValueError("Upload redirects are not followed")
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Invalid media redirect")
                url = urllib.parse.urljoin(url, location)
                response.close()
                connection.close()
                continue
            if response.status >= 300:
                raise ValueError("Media server rejected request")
            return PublicResponse(response, connection)
        except Exception:
            connection.close()
            raise
    raise ValueError("Too many media redirects")


def validate_url(url: str, *, resolver=socket.getaddrinfo) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Media URL must use HTTPS without credentials")
    if parsed.port not in (None, 443):
        raise ValueError("Media URL port is not allowed")
    addresses = resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not _allowed_peer_ip(row[4][0], parsed.hostname or "") for row in addresses):
        raise ValueError("Media URL must resolve only to public addresses")
    return url


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_local(path: str | Path, limit: int = MAX_MEDIA_BYTES) -> bytes:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > limit:
        raise ValueError("Media file is missing or exceeds the byte limit")
    with source.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Media file exceeds the byte limit")
    return data


def image_info(data: bytes) -> tuple[int, int, str]:
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Image exceeds pixel limit")
        fmt = (image.format or "").lower()
        if fmt not in {"jpeg", "png", "gif", "webp"}:
            raise ValueError("Unsupported image format")
        image.verify()
    return width, height, fmt


def cache_image(url: str, directory: Path, *, opener=None) -> Path:
    validate_url(url)
    open_url = opener or safe_open
    deadline = time.monotonic() + 15
    chunks, count = [], 0
    with open_url(url, timeout=4) as response:
        if response.headers.get("Content-Length") and int(response.headers["Content-Length"]) > MAX_MEDIA_BYTES:
            raise ValueError("Media response exceeds byte limit")
        while True:
            chunk = response.read(min(65536, MAX_MEDIA_BYTES + 1 - count))
            count += len(chunk)
            if count > MAX_MEDIA_BYTES or time.monotonic() > deadline:
                raise ValueError("Media download exceeded its budget")
            if not chunk:
                break
            chunks.append(chunk)
    data = b"".join(chunks)
    _, _, fmt = image_info(data)
    directory.mkdir(parents=True, exist_ok=True)
    destination = confined_path(directory, hashlib.sha256(data).hexdigest() + "." + fmt)
    if not destination.exists():
        atomic_write_bytes(destination, data)
    return destination
