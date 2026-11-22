"""初回のモデルダウンロード（続きから再開・壊れたファイル・中身の確認）のテスト。"""
from __future__ import annotations

import http.server
import io
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.model_setup import MODEL_NAME, REQUIRED_FILES, download_and_extract, model_ready  # noqa: E402


def _make_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name in REQUIRED_FILES + ("README.md",):
            data = (name * 2000).encode()
            info = tarfile.TarInfo(f"{MODEL_NAME}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    ranges_seen: list = []

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        self.ranges_seen.append(rng)
        data = self.payload
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            data = data[start:]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class TestModelDownload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Handler.payload = _make_archive()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/m.tar.bz2"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _run(self, root: Path) -> Path:
        return download_and_extract(root, lambda d, t: None, lambda p: None, threading.Event(), url=self.url)

    def test_fresh_download(self):
        with tempfile.TemporaryDirectory() as d:
            out = self._run(Path(d))
            self.assertTrue(model_ready(out))
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), [MODEL_NAME])  # 作業ファイルが残らない

    def test_resume_from_partial(self):
        with tempfile.TemporaryDirectory() as d:
            part = Path(d) / f"{MODEL_NAME}.tar.bz2.part"
            half = len(_Handler.payload) // 2
            part.write_bytes(_Handler.payload[:half])
            _Handler.ranges_seen.clear()
            out = self._run(Path(d))
            self.assertTrue(model_ready(out))
            self.assertEqual(_Handler.ranges_seen, [f"bytes={half}-"])

    def test_already_complete_partial(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{MODEL_NAME}.tar.bz2.part").write_bytes(_Handler.payload)
            self.assertTrue(model_ready(self._run(Path(d))))

    def test_broken_archive_is_removed_for_retry(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{MODEL_NAME}.tar.bz2").write_bytes(b"not a tar")
            with self.assertRaises(IOError):
                self._run(Path(d))
            self.assertFalse((Path(d) / f"{MODEL_NAME}.tar.bz2").exists())
            self.assertTrue(model_ready(self._run(Path(d))))  # 次は最初からダウンロードして成功

    def test_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            ev = threading.Event()
            ev.set()
            with self.assertRaises(InterruptedError):
                download_and_extract(Path(d), lambda a, b: None, lambda p: None, ev, url=self.url)
            self.assertFalse((Path(d) / MODEL_NAME).exists())


if __name__ == "__main__":
    unittest.main()
