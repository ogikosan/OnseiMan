"""失敗した録音を履歴に残し、「再試行」で書き換えるテスト。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.config import ClientConfig  # noqa: E402
from client.logger import AppLogger  # noqa: E402


class TestHistoryFailureAndRetry(unittest.TestCase):
    def test_failure_is_recorded_and_entry_can_be_updated(self):
        with tempfile.TemporaryDirectory() as d:
            lg = AppLogger(Path(d))
            try:
                lg.record_transcription(backend="LAN", elapsed_ms=10, duration_sec=1, raw_text="a", text="a", audio="1.wav")
                e = lg.record_failure(reason="音声が無音です", duration_sec=2.0, audio="2.wav")
                self.assertEqual(lg.get_history()[0].failed, "音声が無音です")
                self.assertEqual(lg.last_text, "a")  # 失敗は「直前の結果」にしない
                lg.update_entry(e, text="こんにちは", raw_text="こんにちは", failed="")
                lines = [json.loads(x) for x in (Path(d) / "history.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines[1]["text"], "こんにちは")
                self.assertEqual(lines[1]["failed"], "")
                # 読み直しても同じ
                lg2 = AppLogger(Path(d))
                self.assertEqual(lg2.get_history()[0].text, "こんにちは")
            finally:
                lg.close()

    def test_old_history_lines_still_load(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "history.jsonl").write_text(
                json.dumps({"timestamp": "t", "backend": "b", "elapsed_ms": 1, "duration_sec": 1, "raw_text": "x", "text": "x", "未知": 1}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            lg = AppLogger(Path(d))
            try:
                self.assertEqual(lg.get_history()[0].audio, "")
            finally:
                lg.close()


class TestAppRetryFlow(unittest.TestCase):
    def _app(self, d: Path):
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = ClientConfig(beep_enabled=False)
        app.ui = mock.MagicMock()
        app.hook = mock.MagicMock()
        app._busy_lock = threading.Lock()
        app._processing = False
        app._tray = None
        app._status_text = ""
        app._last_paste = None
        app.recorder = mock.MagicMock(is_recording=False)
        app.transcriber = mock.MagicMock(last_backend_status="LAN (cpu)")
        app.dictionary = mock.MagicMock(hotwords_csv="")
        app.dictionary.remove_fillers.side_effect = lambda t: t
        app.dictionary.apply_replacements.side_effect = lambda t: t
        app.dictionary.normalize.side_effect = lambda t: t
        app.app_logger = AppLogger(d)
        return app

    def test_empty_result_is_logged_then_retry_fixes_it(self):
        from client import main as m

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = self._app(d)
            try:
                with mock.patch.object(m, "LOG_DIR", d):
                    samples = np.full(16000, 0.05, dtype=np.float32)
                    name = app._save_recording_audio(samples, 16000)
                    self.assertTrue((d / "audio" / name).exists())
                    app.transcriber.transcribe.return_value = mock.MagicMock(text="", backend="LAN", elapsed_ms=5.0)
                    app._process_audio(samples, 1.0, audio_name=name)
                    h = app.app_logger.get_history()[0]
                    self.assertEqual(h.failed, "音声が認識されませんでした")
                    self.assertEqual(h.audio, name)

                    app.transcriber.transcribe.return_value = mock.MagicMock(text="こんにちは", backend="LAN", elapsed_ms=5.0)
                    with mock.patch.object(m, "set_clipboard_text") as clip:
                        app._retry_entry(h.timestamp, h.audio)
                    clip.assert_called_once_with("こんにちは")
                    self.assertEqual(len(app.app_logger.get_history()), 1)  # 新しい行は増えない
                    self.assertEqual(app.app_logger.get_history()[0].text, "こんにちは")
                    self.assertEqual(app.app_logger.get_history()[0].failed, "")
            finally:
                app.app_logger.close()

    def test_retry_rejects_names_outside_audio_folder(self):
        from client import main as m

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = self._app(d)
            try:
                (d / "secret.wav").write_bytes(b"x")
                app.app_logger.record_failure(reason="x", duration_sec=1, audio="../secret.wav")
                with mock.patch.object(m, "LOG_DIR", d), mock.patch.object(app, "_process_audio") as pa:
                    app._retry_entry(app.app_logger.get_history()[0].timestamp, "../secret.wav")
                pa.assert_not_called()
            finally:
                app.app_logger.close()

    def test_audio_files_are_pruned(self):
        from client import main as m

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = self._app(d)
            try:
                with mock.patch.object(m, "LOG_DIR", d), mock.patch.object(type(app), "AUDIO_KEEP", 3):
                    for _ in range(5):
                        app._save_recording_audio(np.zeros(160, dtype=np.float32), 16000)
                    self.assertEqual(len(list((d / "audio").glob("*.wav"))), 3)
            finally:
                app.app_logger.close()


if __name__ == "__main__":
    unittest.main()
