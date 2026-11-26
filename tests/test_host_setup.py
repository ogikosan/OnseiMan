"""設定画面から「このパソコンを声を文字にする役にする」仕組みのテスト（本物の準備は動かさない）。"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import host_setup as hs  # noqa: E402
from server import relay  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_addresses_skip_virtual_networks(self) -> None:
        rows = [
            {"ip": "127.0.0.1", "alias": "Loopback Pseudo-Interface 1"},
            {"ip": "172.25.80.1", "alias": "vEthernet (WSL (Hyper-V firewall))"},
            {"ip": "192.168.99.1", "alias": "VirtualBox Host-Only Network"},
            {"ip": "169.254.3.4", "alias": "イーサネット 2"},
            {"ip": "192.168.10.185", "alias": "Wi-Fi"},
            {"ip": "100.83.113.30", "alias": "Tailscale"},
            {"ip": "192.168.10.185", "alias": "Wi-Fi"},
        ]
        got = hs.pick_addresses(rows)
        self.assertEqual([a["ip"] for a in got], ["100.83.113.30", "192.168.10.185"])  # Tailscale が先
        self.assertEqual(got[0]["kind"], "tailscale")
        self.assertEqual(got[1]["url"], "http://192.168.10.185:8765")

    def test_tailscale_status(self) -> None:
        j = {"BackendState": "Running", "Self": {"HostName": "pc", "TailscaleIPs": ["100.1.2.3", "fd7a:115c:a1e0::1"]}}
        self.assertEqual(hs.parse_tailscale_status(json.dumps(j)),
                         {"installed": True, "state": "running", "ip": "100.1.2.3", "name": "pc"})
        self.assertEqual(hs.parse_tailscale_status(json.dumps({"BackendState": "NeedsLogin"}))["state"], "needs_login")
        self.assertEqual(hs.parse_tailscale_status("壊れた")["state"], "unknown")

    def test_server_kind_from_run_value(self) -> None:
        self.assertEqual(hs.server_kind_from_run_value(r'wscript.exe "D:\x\server\start_server_gpu_hidden.vbs"'), "gpu")
        self.assertEqual(hs.server_kind_from_run_value(r'wscript.exe "D:\x\server\start_server_hidden.vbs"'), "cpu")
        self.assertEqual(hs.server_kind_from_run_value(""), "")

    def test_bad_kind_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            hs.HostSetup(ROOT).start_server("tpu")


@unittest.skipUnless(os.name == "nt", "Windows だけ")
class TestJob(unittest.TestCase):
    """準備は別の手順（PowerShell）で動き、進み具合（記録）と結果を status() で見られる。"""

    def test_job_log_and_result(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "tools").mkdir()
            (tmp / "tools" / "installer_run.ps1").write_bytes(
                "\ufeffparam($Server, [switch]$NoPause)\r\nWrite-Host \"準備しています $Server\"\r\nWrite-Host \"済みました\"\r\nexit 3\r\n".encode("utf-8"))
            h = hs.HostSetup(tmp)
            h._cache["gpu"] = {"ok": True, "message": "テスト"}
            h.start_server("gpu")
            with self.assertRaises(ValueError):   # 準備中は、ほかの準備を始めない
                h.start_server("cpu")
            for _ in range(100):
                st = h.status()
                if not st["job"]["running"]:
                    break
                time.sleep(0.1)
            self.assertFalse(st["job"]["running"])
            self.assertEqual(st["job"]["result"], 3)
            self.assertEqual(st["job"]["kind"], "gpu")
            log = "\n".join(st["job"]["log"])
            self.assertIn("準備しています gpu", log)
            self.assertIn("済みました", log)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_gpu_needs_ok_check(self) -> None:
        h = hs.HostSetup(ROOT)
        h._cache["gpu"] = {"ok": False, "message": "NVIDIA の GPU が見つかりませんでした。"}
        with self.assertRaises(ValueError) as cm:
            h.start_server("gpu")
        self.assertIn("NVIDIA", str(cm.exception))


class TestWebApi(unittest.TestCase):
    def test_routes(self) -> None:
        import urllib.request

        from client import web_ui

        f = lambda *a, **k: None  # noqa: E731
        tmp = Path(tempfile.mkdtemp())
        srv = web_ui.WebUIServer(dict_path=tmp / "d.json", config_path=tmp / "c.json", get_history_fn=lambda: [],
                                 get_status_info=lambda: ("", ""), get_learning_candidates_fn=lambda: [], on_copy_text=f,
                                 on_quick_add_dict=f, on_retranscribe_last=f, on_approve_candidate=f, on_reject_candidate=f,
                                 on_learn_from_manual_edit=lambda a, b: 0, preferred_port=0)
        host = mock.MagicMock()
        host.status.return_value = {"supported": True, "running": False}
        srv.host_setup = host
        url = srv.start()
        try:
            with urllib.request.urlopen(url + "/api/host", timeout=5) as r:
                self.assertTrue(json.loads(r.read())["supported"])

            def post(path, body):
                req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read())

            post("/api/host/start", {"kind": "gpu"})
            host.start_server.assert_called_with("gpu")
            post("/api/host/stop", {"remove_gpu": True})
            host.stop_server.assert_called_with(remove_gpu=True)
            post("/api/host/firewall", {})
            host.allow_network.assert_called_once()
            post("/api/host/tailscale", {"action": "login"})
            host.tailscale_action.assert_called_with("login")
        finally:
            srv.stop()
            shutil.rmtree(tmp, ignore_errors=True)


class TestRelayFallback(unittest.TestCase):
    def test_uses_linux_address_when_localhost_fails(self) -> None:
        # 127.0.0.1 では届かず、Linux のアドレス（ここでは 127.0.0.2 にしておく）で届く
        async def run() -> bytes:
            async def echo(r, w):
                w.write(b"ok:" + await r.read(10))
                await w.drain()
                w.close()

            up = await asyncio.start_server(echo, "127.0.0.2", 0)
            port = up.sockets[0].getsockname()[1]
            with mock.patch.object(relay, "wsl_ip", return_value="127.0.0.2"):
                t = relay.Targets("127.0.0.1", port, "onseiman-gpu")
                r, w = await t.connect()
                self.assertTrue(t.prefer_ip)
                w.write(b"hi")
                await w.drain()
                got = await asyncio.wait_for(r.read(10), timeout=5)
                w.close()
            up.close()
            return got

        self.assertEqual(asyncio.run(run()), b"ok:hi")


if __name__ == "__main__":
    unittest.main()


class TestAutoChoice(unittest.TestCase):
    def test_gpu_server_comes_first(self) -> None:
        # 返事が少し遅くても、GPU で聞き取るパソコン（聞き間違いが少ない）を先にする
        from client.discovery import merge_same_machines

        found = [
            {"url": "http://100.99.44.124:8765", "via": "Tailscale", "ready": True, "ms": 20, "engine": "funasr", "id": "a"},
            {"url": "http://100.83.113.30:8765", "via": "Tailscale", "ready": True, "ms": 60, "engine": "haas", "id": "b"},
            {"url": "http://100.79.230.25:8765", "via": "Tailscale", "ready": False, "ms": 5, "engine": "haas", "id": "c"},
        ]
        got = merge_same_machines(found, [])
        self.assertEqual(got[0]["url"], "http://100.83.113.30:8765")
        self.assertFalse(got[-1]["ready"])   # 準備中は後ろ

    def test_gpu_host_auto_is_off_by_default(self) -> None:
        # speech.cpp の Haas-ja が CPU でも速いので、GPU のパソコンに 10GB を勝手に入れない
        from client.config import ClientConfig

        self.assertFalse(ClientConfig().gpu_host_auto)

    def test_settings_screen_has_no_host_card(self) -> None:
        # 「頼まれる側」になる準備は裏で自動でやる。設定の画面には出さない（話す側が迷うため）
        html = (ROOT / "client" / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="host-card"', html)
        js = (ROOT / "client" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("#host-card", js)


class TestMicTest(unittest.TestCase):
    """設定の「マイク」の「試す」。本物のマイクの代わりに、作り物の音を流し込む。"""

    def _run(self, amplitude: float, text: str = "テストです"):
        import threading
        import types

        import numpy as np

        from client import main as m

        class FakeStream:
            def __init__(self, samplerate, channels, dtype, device, callback, blocksize):
                self.cb, self.n = callback, blocksize

            def __enter__(self):
                t = np.arange(self.n) / 16000.0
                for _ in range(5):
                    self.cb((amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32).reshape(-1, 1), self.n, None, None)
                return self

            def __exit__(self, *a):
                return False

        app = types.SimpleNamespace(
            config=types.SimpleNamespace(sample_rate=16000, microphone=None),
            _stop_event=threading.Event(), MIC_TEST_SEC=0.2,
            dictionary=types.SimpleNamespace(hotwords_csv=""),
            transcriber=types.SimpleNamespace(last_backend_status="Local Haas"),
            _recognize_safely=lambda audio, sr, hw: types.SimpleNamespace(text=text),
        )
        app.mic_verdict = m.OnseiManApp.mic_verdict
        app.friendly_backend = m.OnseiManApp.friendly_backend
        app._mic_test = {"state": "recording", "level": 0.0, "peak": 0.0, "left": 0.2, "text": "", "verdict": "", "verdict_kind": "", "backend": ""}
        import sounddevice
        with mock.patch.object(sounddevice, "InputStream", FakeStream):
            m.OnseiManApp._mic_test_run(app)
        return app._mic_test

    def test_good_level_shows_text(self) -> None:
        st = self._run(0.3)
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["verdict_kind"], "ok")
        self.assertEqual(st["text"], "テストです")
        self.assertEqual(st["backend"], "このパソコンで認識")

    def test_silence_is_reported(self) -> None:
        st = self._run(0.0)
        self.assertEqual(st["verdict_kind"], "ng")
        self.assertEqual(st["text"], "")

    def test_quiet_and_clipping(self) -> None:
        self.assertEqual(self._run(0.03)["verdict_kind"], "warn")
        self.assertEqual(self._run(1.0)["verdict_kind"], "warn")
