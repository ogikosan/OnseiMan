"""設定画面（このパソコンの中だけの管理画面）が、ほかのサイトから操作されないことのテスト。"""
from __future__ import annotations

import http.client
import json
import sys
import tarfile
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.config import ClientConfig  # noqa: E402


class TestWebUIGuard(unittest.TestCase):
    def setUp(self) -> None:
        from client.web_ui import WebUIServer

        self.tmp = tempfile.TemporaryDirectory()
        p = Path(self.tmp.name)
        self.cfg = p / "config.json"
        ClientConfig().save(self.cfg)
        (p / "dict.json").write_text("{}", encoding="utf-8")
        self.srv = WebUIServer(
            dict_path=p / "dict.json", config_path=self.cfg, get_history_fn=lambda: [],
            get_status_info=lambda: ("Local CPU", "ralt"), get_learning_candidates_fn=lambda: [],
            on_copy_text=lambda _t: None, on_quick_add_dict=lambda s, d, h: None, on_retranscribe_last=lambda: None,
            on_approve_candidate=lambda _s, _d: None, on_reject_candidate=lambda _s, _d: None,
            on_learn_from_manual_edit=lambda _b, _a: 1, get_hotkey=lambda: "ralt", preferred_port=0,
        )
        self.url = self.srv.start()
        self.port = int(self.url.rsplit(":", 1)[1])

    def tearDown(self) -> None:
        self.srv.stop()
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None, headers=None) -> int:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        st = c.getresponse().status
        c.close()
        return st

    def test_own_screen_works(self) -> None:
        self.assertEqual(self._req("GET", "/api/state"), 200)
        own = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self._req("POST", "/api/dict", {"hotwords": ["LAN"]}, own), 200)

    def test_other_site_cannot_write(self) -> None:
        evil = {"Origin": "https://evil.example"}
        self.assertEqual(self._req("POST", "/api/settings", {"server_urls": ["http://evil:8765"]}, evil), 403)
        self.assertEqual(self._req("POST", "/api/dict", {"hotwords": ["x"]}, evil), 403)

    def test_form_post_is_rejected(self) -> None:
        # フォームの送信（ほかのサイトから送れる形）は受け付けない
        st = self._req("POST", "/api/dict", {"hotwords": ["x"]}, {"Content-Type": "text/plain"})
        self.assertEqual(st, 403)

    def test_dns_rebinding_host_is_rejected(self) -> None:
        self.assertEqual(self._req("GET", "/api/state", headers={"Host": f"evil.example:{self.port}"}), 403)
        self.assertEqual(self._req("GET", "/api/audio/a.wav", headers={"Host": "evil.example"}), 403)

    def test_cross_site_read_and_embed_are_rejected(self) -> None:
        # ほかのサイト（別のポートで動く画面も含む）からの読み込み・埋め込み
        for site in ("cross-site", "same-site"):
            self.assertEqual(self._req("GET", "/api/state", headers={"Sec-Fetch-Site": site}), 403, site)
            self.assertEqual(self._req("GET", "/api/audio/a.wav", headers={"Sec-Fetch-Site": site}), 403, site)
        self.assertEqual(self._req("GET", "/api/state", headers={"Sec-Fetch-Site": "same-origin"}), 200)
        self.assertEqual(self._req("GET", "/", headers={"Sec-Fetch-Site": "none"}), 200)

    def test_security_headers(self) -> None:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        csp = r.getheader("Content-Security-Policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self';", csp)   # 画面に直接書かれたスクリプトは動かさない
        self.assertIn("connect-src 'self'", csp)
        c.close()

    def test_no_inline_script_in_page(self) -> None:
        import re

        html = (ROOT / "client" / "web" / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r"<script[^>]*>", html)
        self.assertTrue(tags)
        self.assertTrue(all("src=" in t for t in tags), tags)          # 直接書いたスクリプトが無い
        self.assertEqual(re.findall(r"<[^>]*\son(?:click|load|error|input|change|submit)\s*=", html), [])

    def test_settings_unchanged_after_attack(self) -> None:
        self._req("POST", "/api/settings", {"server_urls": ["http://evil:8765"]}, {"Origin": "https://evil.example"})
        self.assertEqual(ClientConfig.load(self.cfg).server_urls, [])


class TestSafeExtract(unittest.TestCase):
    def test_model_archive_cannot_escape_folder(self) -> None:
        from client.model_setup import MODEL_NAME, download_and_extract
        import threading

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
            info = tarfile.TarInfo(f"{MODEL_NAME}/evil-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../../../outside.txt"
            tar.addfile(info)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / f"{MODEL_NAME}.tar.bz2").write_bytes(buf.getvalue())
            with self.assertRaises(Exception):
                download_and_extract(Path(d), lambda a, b: None, lambda p: None, threading.Event(), expected_sha256=None)
            self.assertFalse((Path(d).parent / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
