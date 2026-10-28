import io
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
from core import media


class MediaTests(unittest.TestCase):
    def test_rejects_private_and_credential_urls(self):
        resolver = lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        for url in ("https://example.com/file", "http://example.com/file", "https://" + "a:b@" + "example.com/file"):
            with self.assertRaises(ValueError):
                media.validate_url(url, resolver=resolver)

    def test_actual_bytes_are_bounded(self):
        class Response(io.BytesIO):
            headers = {}
        with tempfile.TemporaryDirectory() as directory, patch.object(media, "MAX_MEDIA_BYTES", 8), patch.object(media, "validate_url", return_value="https://example.com/a"):
            with self.assertRaises(ValueError):
                media.cache_image("https://example.com/a", Path(directory), opener=lambda *_args, **_kwargs: Response(b"x" * 9))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_local_size_is_checked_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large"
            path.write_bytes(b"x" * 9)
            with self.assertRaises(ValueError):
                media.read_local(path, 8)
