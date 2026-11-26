"""認識サーバーが、おかしな送信を断ることのテスト（サーバー用の部品が入っている環境でだけ動く）。"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from server import app as srv
    HAVE = True
except Exception:  # fastapi が無い（クライアントだけの環境）
    HAVE = False


def _wav(sec: float, sr: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * sec))
    return buf.getvalue()


class _ServerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = mock.MagicMock(active_provider="cpu")
        self.engine.transcribe.return_value = ("こんにちは", 12.0)
        self.engine.model_name = "Haas-ja-v1.0"
        p = mock.patch.object(srv, "engine", self.engine)
        p.start()
        self.addCleanup(p.stop)
        self.c = TestClient(srv.app)

    def _post(self, data: bytes, **form):
        return self.c.post("/transcribe", files={"audio": ("a.wav", data, "audio/wav")}, data=form)


@unittest.skipUnless(HAVE, "fastapi が入っていません")
class TestServerGuard(_ServerCase):
    def test_normal(self) -> None:
        r = self._post(_wav(1.0))
        self.assertEqual((r.status_code, r.json()["text"]), (200, "こんにちは"))

    def test_non_wav_is_rejected(self) -> None:
        self.assertEqual(self._post(b"fLaC" + b"\0" * 100).status_code, 400)
        self.assertEqual(self._post(b"OggS" + b"\0" * 100).status_code, 400)
        self.engine.transcribe.assert_not_called()

    def test_too_long_and_too_big(self) -> None:
        with mock.patch.object(srv, "MAX_AUDIO_SEC", 2.0):
            self.assertEqual(self._post(_wav(3.0)).status_code, 413)
        with mock.patch.object(srv, "MAX_AUDIO_BYTES", 1000):
            self.assertEqual(self._post(_wav(1.0)).status_code, 413)

    def test_docs_are_not_exposed(self) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.c.get(path).status_code, 404, path)

    def test_long_audio_is_split(self) -> None:
        # 長い音声（iPhone から 40 秒など）は、認識データの上限を超えないよう区切ってから認識してつなぐ
        import numpy as np
        sr = 16000
        rng = np.random.default_rng(0)
        voice = lambda sec: (rng.standard_normal(int(sr * sec)) * 0.1).astype(np.float32)
        quiet = lambda sec: np.zeros(int(sr * sec), dtype=np.float32)
        samples = np.concatenate([voice(10), quiet(1), voice(10), quiet(1), voice(10)])
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes((samples * 32767).astype("<i2").tobytes())
        self.engine.transcribe.side_effect = [("一つ目", 5.0), ("二つ目", 5.0), ("三つ目", 5.0), ("四つ目", 5.0)]
        r = self._post(buf.getvalue())
        self.assertEqual(r.status_code, 200)
        calls = self.engine.transcribe.call_args_list
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(c.kwargs["samples"].size / sr <= 18.0 for c in calls))
        self.assertTrue(r.json()["text"].startswith("一つ目二つ目"))

    def test_health_tells_engine_and_segment_length(self) -> None:
        h = self.c.get("/health").json()
        self.assertEqual(h["engine"], "haas")
        self.assertEqual(h["max_segment_sec"], 15.0)

    def test_error_does_not_leak_details(self) -> None:
        r = self._post(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 60)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Traceback", r.text)


# M4A のふりをした先頭（中身は、すり替えた変換役が決める）
FAKE_M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100

# 変換役のすり替えで動かす Python（引数は 入力, 出力）
WRITE_WAV = r"""
import sys, wave
w = wave.open(sys.argv[2], 'wb')
w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
w.writeframes(b'\x00\x00' * 16000)
w.close()
"""
ENDLESS = r"""
import sys, time
f = open(sys.argv[2], 'wb')
while True:
    f.write(b'0' * 65536); f.flush(); time.sleep(0.001)
"""


def _fake_converter(code: str):
    return lambda src, dst: [sys.executable, "-c", code, src, dst]


@unittest.skipUnless(HAVE, "fastapi が入っていません")
class TestM4A(_ServerCase):
    """iPhone のショートカットなどから送られる M4A。WAV にしてから、いつもの確かめに通す。"""

    def _post_m4a(self, data: bytes = FAKE_M4A):
        return self.c.post("/transcribe", files={"audio": ("a.m4a", data, "audio/mp4")})

    def test_m4a_is_converted(self) -> None:
        with mock.patch.object(srv, "m4a_command", _fake_converter(WRITE_WAV)):
            r = self._post_m4a()
        self.assertEqual((r.status_code, r.json()["text"]), (200, "こんにちは"))
        kw = self.engine.transcribe.call_args.kwargs
        self.assertEqual((kw["sample_rate"], kw["samples"].size), (16000, 16000))

    def test_broken_m4a_is_rejected(self) -> None:
        with mock.patch.object(srv, "m4a_command", _fake_converter("import sys; sys.exit(1)")):
            r = self._post_m4a()
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Traceback", r.text)
        self.engine.transcribe.assert_not_called()

    def test_no_converter(self) -> None:
        with mock.patch.object(srv, "m4a_command", lambda src, dst: None):
            self.assertEqual(self._post_m4a().status_code, 415)

    def test_huge_output_is_stopped(self) -> None:
        # 小さな M4A が巨大な WAV に化けても、上限で止める
        with mock.patch.object(srv, "MAX_AUDIO_BYTES", 2_000_000), \
             mock.patch.object(srv, "m4a_command", _fake_converter(ENDLESS)):
            self.assertEqual(self._post_m4a().status_code, 413)
        self.engine.transcribe.assert_not_called()

    def test_slow_converter_times_out(self) -> None:
        with mock.patch.object(srv, "CONVERT_TIMEOUT_SEC", 0.5), \
             mock.patch.object(srv, "m4a_command", _fake_converter("import time; time.sleep(30)")):
            self.assertEqual(self._post_m4a().status_code, 413)

    def test_other_formats_still_rejected(self) -> None:
        with mock.patch.object(srv, "m4a_command", _fake_converter(WRITE_WAV)):
            self.assertEqual(self._post(b"fLaC" + b"\0" * 100).status_code, 400)
            self.assertEqual(self._post(b"OggS" + b"\0" * 100).status_code, 400)

    @unittest.skipUnless(shutil.which("ffmpeg"), "本物の M4A を作る ffmpeg がありません")
    def test_real_m4a(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            m4a = Path(tmp) / "a.m4a"
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=2", "-ar", "44100", "-c:a", "aac", "-y", str(m4a)],
                           check=True)
            data = m4a.read_bytes()
        self.assertTrue(srv.is_m4a(data))
        r = self._post_m4a(data)
        self.assertEqual(r.status_code, 200, r.text)
        kw = self.engine.transcribe.call_args.kwargs
        self.assertEqual(kw["sample_rate"], 16000)
        self.assertAlmostEqual(kw["samples"].size / 16000, 2.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
