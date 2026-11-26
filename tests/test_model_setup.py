"""聞き取りの部品（speech.cpp と Haas-ja）を取ってくる手順のテスト（続きから再開・中身の確認・zip の中身）。"""
from __future__ import annotations

import hashlib
import http.server
import io
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared import speechcpp_setup as s  # noqa: E402


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    ranges_seen: list = []

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        self.ranges_seen.append(rng)
        data = self.payload
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestDownload(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.payload = bytes(range(256)) * 4000
        _Handler.ranges_seen = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/f"
        self.sha = hashlib.sha256(_Handler.payload).hexdigest()
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self.srv.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resumes_from_partial_file(self) -> None:
        dest = self.tmp / "m.gguf"
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(_Handler.payload[:100000])   # 前回ここで止まった
        s._download(self.url, dest, self.sha, "テスト", None, None)
        self.assertEqual(dest.read_bytes(), _Handler.payload)
        self.assertEqual(_Handler.ranges_seen, ["bytes=100000-"])
        self.assertFalse(part.exists())

    def test_wrong_file_is_rejected(self) -> None:
        dest = self.tmp / "m.gguf"
        with self.assertRaises(RuntimeError):
            s._download(self.url, dest, "0" * 64, "テスト", None, None)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_progress_and_cancel(self) -> None:
        seen = []
        s._download(self.url, self.tmp / "a", self.sha, "テスト", lambda lab, d, t: seen.append((d, t)), None)
        self.assertEqual(seen[-1], (len(_Handler.payload), len(_Handler.payload)))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(RuntimeError):
            s._download(self.url, self.tmp / "b", self.sha, "テスト", None, stop)


class TestZipEscape(unittest.TestCase):
    def test_zip_cannot_write_outside_the_folder(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../evil.txt", "x")
        data = buf.getvalue()
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            src.mkdir()
            (src / "speech.zip").write_bytes(data)
            root = Path(d) / "root"
            with mock.patch.object(s, "platform_key", return_value="windows"), \
                    mock.patch.dict(s.TOOLS, {"windows": ("speech.zip", hashlib.sha256(data).hexdigest())}), \
                    mock.patch.object(s, "_BASE", src.as_uri() + "/"):
                with self.assertRaises(RuntimeError):
                    s.ensure(root)
            self.assertFalse((Path(d) / "root" / "tools" / "evil.txt").exists())
            self.assertFalse((s.tool_dir(root).parent / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
