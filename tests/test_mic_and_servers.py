"""マイクテストと、認識役を選ぶ順番のテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestAutoChoice(unittest.TestCase):
    def test_gpu_server_comes_first(self) -> None:
        # 返事が少し遅くても、GPU で聞き取るパソコン（聞き間違いが少ない）を先にする
        from client.discovery import merge_same_machines

        found = [
            {"url": "http://100.99.44.124:8765", "via": "Tailscale", "ready": True, "ms": 20, "engine": "", "id": "a"},
            {"url": "http://100.83.113.30:8765", "via": "Tailscale", "ready": True, "ms": 60, "engine": "haas", "id": "b"},
            {"url": "http://100.79.230.25:8765", "via": "Tailscale", "ready": False, "ms": 5, "engine": "haas", "id": "c"},
        ]
        got = merge_same_machines(found, [])
        self.assertEqual(got[0]["url"], "http://100.83.113.30:8765")
        self.assertFalse(got[-1]["ready"])   # 準備中は後ろ

    def test_settings_screen_has_no_host_card(self) -> None:
        # 「このパソコンを認識役にする」欄はなくした（話す側が迷うため）
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
