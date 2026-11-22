"""認識サーバーが、おかしな送信を断ることのテスト（サーバー用の部品が入っている環境でだけ動く）。"""
from __future__ import annotations

import io
import sys
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


@unittest.skipUnless(HAVE, "fastapi が入っていません")
class TestServerGuard(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = mock.MagicMock(active_provider="cpu")
        self.engine.transcribe.return_value = ("こんにちは", 12.0)
        self.engine.model_dir.name = "m"
        p = mock.patch.object(srv, "engine", self.engine)
        p.start()
        self.addCleanup(p.stop)
        self.c = TestClient(srv.app)

    def _post(self, data: bytes, **form):
        return self.c.post("/transcribe", files={"audio": ("a.wav", data, "audio/wav")}, data=form)

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

    def test_error_does_not_leak_details(self) -> None:
        r = self._post(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 60)
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Traceback", r.text)


if __name__ == "__main__":
    unittest.main()
