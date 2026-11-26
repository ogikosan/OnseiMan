"""区切り認識・サーバー選択・押す前の音・取り消し・記号コマンドのテスト。"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.config import ClientConfig  # noqa: E402
from shared.segmenter import Segmenter, join_texts, split_on_pauses  # noqa: E402
from shared.voice_commands import apply_voice_commands, is_undo_command  # noqa: E402

SR = 16000


def speech(sec: float, amp: float = 0.08) -> np.ndarray:
    t = np.arange(int(SR * sec)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.float32)


SEG = dict(rms_threshold=0.012, pause_sec=0.6, min_segment_sec=8.0, max_segment_sec=18.0)


class TestSegmenter(unittest.TestCase):
    def test_short_utterance_is_one_segment(self) -> None:
        parts = split_on_pauses(np.concatenate([speech(3), silence(0.8), speech(2)]), SR, **SEG)
        self.assertEqual(len(parts), 1)

    def test_cuts_at_pauses_after_min_length(self) -> None:
        audio = np.concatenate([speech(9), silence(0.8), speech(9), silence(0.8), speech(5)])
        parts = split_on_pauses(audio, SR, **SEG)
        self.assertEqual(len(parts), 3)
        # 音を落とさない（区切りを全部つなぐと元の長さ）
        self.assertEqual(sum(len(p) for p in parts), len(audio))
        for p in parts:
            self.assertLessEqual(len(p) / SR, 18.0 + 0.01)

    def test_continuous_speech_is_force_cut_under_model_limit(self) -> None:
        # 切れ目なしで 60 秒話し続けても、どの区切りも 18 秒以下（モデルの上限 約30秒 より十分短い）
        audio = speech(60)
        parts = split_on_pauses(audio, SR, **SEG)
        self.assertGreaterEqual(len(parts), 4)
        self.assertTrue(all(len(p) / SR <= 18.0 + 0.01 for p in parts))
        self.assertEqual(sum(len(p) for p in parts), len(audio))

    def test_force_cut_prefers_quiet_spot(self) -> None:
        # 15秒地点に短い小休止（0.3秒、切れ目判定の 0.6 秒未満）→ 18秒で強制的に切るとき、そこで切る
        audio = np.concatenate([speech(15), speech(0.3, amp=0.001), speech(10)])
        parts = split_on_pauses(audio, SR, **SEG)
        self.assertAlmostEqual(len(parts[0]) / SR, 15.15, delta=0.25)

    def test_silence_only_is_dropped_live(self) -> None:
        seg = Segmenter(SR, **SEG)
        out = seg.feed(silence(20))
        self.assertEqual(out, [])
        self.assertIsNone(seg.flush())

    def test_quiet_voice_is_not_dropped(self) -> None:
        # 切れ目判定のしきい値より小さい声でも、完全な無音でなければ捨てない
        parts = split_on_pauses(speech(3, amp=0.01), SR, **SEG)
        self.assertEqual(len(parts), 1)
        self.assertEqual(len(parts[0]), int(SR * 3))

    def test_join_texts(self) -> None:
        self.assertEqual(join_texts(["今日は。", "", "明日も。"]), "今日は。明日も。")
        self.assertEqual(join_texts(["use GPU", "RTX 5090"]), "use GPU RTX 5090")


class TestLiveTranscription(unittest.TestCase):
    def test_results_are_joined_in_order_and_failures_retried(self) -> None:
        from client.live_session import LiveTranscription

        calls = {"n": 0}

        def fake(samples):
            calls["n"] += 1
            key = int(samples[0])
            if key == 1 and calls["n"] == 2:
                raise ConnectionError("一時的な失敗")
            time.sleep(0.01 * (3 - key))
            return mock.MagicMock(text=f"文{key}。", backend="LAN-cpu", elapsed_ms=100.0)

        progress = []
        live = LiveTranscription(fake, on_progress=progress.append)
        for k in range(3):
            live.submit(np.full(10, k, dtype=np.float32))
        res = live.finish()
        self.assertEqual(res.text, "文0。文1。文2。")
        self.assertEqual(res.segments, 3)
        self.assertTrue(progress)

    def test_permanent_failure_raises_instead_of_dropping_text(self) -> None:
        from client.live_session import LiveTranscription

        def fake(samples):
            if int(samples[0]) == 1:
                raise ConnectionError("落ちている")
            return mock.MagicMock(text="ok", backend="x", elapsed_ms=1.0)

        live = LiveTranscription(fake)
        live.submit(np.zeros(4, dtype=np.float32))
        live.submit(np.ones(4, dtype=np.float32))
        with self.assertRaises(RuntimeError):
            live.finish()

    def test_cancel_stops_worker(self) -> None:
        from client.live_session import LiveTranscription

        live = LiveTranscription(lambda s: mock.MagicMock(text="x", backend="b", elapsed_ms=0))
        live.cancel()
        live._worker.join(timeout=1)
        self.assertFalse(live._worker.is_alive())


class TestServerOrder(unittest.TestCase):
    def _make(self, urls):
        from client.transcriber import HybridTranscriber

        cfg = ClientConfig(server_urls=urls, local_fallback=False)
        return HybridTranscriber(cfg, ROOT)

    def test_failed_server_is_skipped_next_time_and_good_one_goes_first(self) -> None:
        import requests

        tr = self._make(["http://a:1", "http://b:1"])
        posted = []

        class FakeSession:
            def post(self, url, **kw):
                posted.append(url)
                if url.startswith("http://a"):
                    raise requests.exceptions.ConnectTimeout("down")
                r = mock.MagicMock()
                r.json.return_value = {"text": "こんにちは", "provider": "cpu"}
                return r

        with mock.patch("client.transcriber.requests.Session", FakeSession):
            res1 = tr.transcribe(np.zeros(1600, dtype=np.float32))
            res2 = tr.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(res1.text, "こんにちは")
        self.assertEqual(res2.text, "こんにちは")
        # 1回目は a で待たされるが、2回目は最初から b に送る
        self.assertEqual(posted, ["http://a:1/transcribe", "http://b:1/transcribe", "http://b:1/transcribe"])

    def test_all_down_still_tries_everything(self) -> None:
        tr = self._make(["http://a:1", "http://b:1"])
        tr._mark_down("http://a:1")
        tr._mark_down("http://b:1")
        self.assertEqual(tr.ordered_servers(), ["http://a:1", "http://b:1"])

    def test_stale_connection_is_retried_once(self) -> None:
        import requests

        tr = self._make(["http://a:1"])
        n = {"c": 0}

        class FakeSession:
            def post(self, url, **kw):
                n["c"] += 1
                if n["c"] == 1:
                    raise requests.exceptions.ConnectionError("connection reset")
                r = mock.MagicMock()
                r.json.return_value = {"text": "ok", "provider": "cpu"}
                return r

        with mock.patch("client.transcriber.requests.Session", FakeSession):
            res = tr.transcribe(np.zeros(1600, dtype=np.float32))
        self.assertEqual(res.text, "ok")
        self.assertEqual(tr.ordered_servers()[0], "http://a:1")

    def test_health_check_marks_servers(self) -> None:
        tr = self._make(["http://a:1", "http://b:1"])
        with mock.patch.object(tr, "_probe", side_effect=lambda u: None if "a:" in u else "cpu"):
            chosen = tr.check_server_health()
        self.assertEqual(chosen, "http://b:1")
        self.assertEqual(tr.ordered_servers(), ["http://b:1"])


class TestRecorderPrerollAndSegments(unittest.TestCase):
    def _warm_recorder(self, **kw):
        from client.recorder import AudioRecorder

        rec = AudioRecorder(sample_rate=SR, keep_open=True, preroll_sec=0.4, **kw)
        rec._stream = mock.MagicMock(active=True)
        rec._stream_device = None
        rec._last_callback_time = time.perf_counter()
        # テストでは本物のマイクを絶対に開かない（開いたままだと後のテストで落ちる）
        patcher = mock.patch.object(rec, "_open_stream_unlocked", side_effect=AssertionError("本物のマイクを開こうとした"))
        patcher.start()
        self.addCleanup(patcher.stop)
        return rec

    def _feed(self, rec, audio):
        for i in range(0, len(audio), 1024):
            rec._last_callback_time = time.perf_counter()
            rec._callback(audio[i:i + 1024].reshape(-1, 1), 1024, None, None)

    def test_audio_just_before_press_is_included(self) -> None:
        rec = self._warm_recorder()
        self._feed(rec, np.concatenate([silence(2.0), speech(0.4)]))  # 押す前: 2秒の無音のあと話し始めた
        with mock.patch("client.recorder.resolve_input_device", return_value=None):
            self.assertTrue(rec.start())
        self._feed(rec, speech(1.0))
        samples, _dur, cancelled = rec.stop()
        self.assertFalse(cancelled)
        self.assertGreaterEqual(len(samples), int(SR * 1.35))  # 押す前の約0.4秒 + 押した後の1秒
        self.assertGreater(float(np.abs(samples[:2000]).max()), 0.05)  # 言い出しの音が先頭に入っている
        self.assertIsNotNone(rec._stream)  # マイクは開いたまま

    def test_dead_stream_is_reopened(self) -> None:
        rec = self._warm_recorder()
        rec._last_callback_time = time.perf_counter() - 5  # スリープ復帰などで止まっていた
        with mock.patch("client.recorder.resolve_input_device", return_value=None), \
                mock.patch.object(rec, "_open_stream_unlocked", return_value=True) as op:
            self.assertTrue(rec.start())
        op.assert_called_once()

    def test_segments_are_emitted_while_recording_and_tail_on_stop(self) -> None:
        got = []
        rec = self._warm_recorder(on_segment=got.append, segment_kwargs=dict(SEG))
        with mock.patch("client.recorder.resolve_input_device", return_value=None):
            rec.start()
        self._feed(rec, np.concatenate([speech(9), silence(0.8)]))
        self.assertEqual(len(got), 1)  # 話している途中で 1 個目が出る
        self._feed(rec, speech(3))
        rec.stop()
        self.assertEqual(len(got), 2)  # 止めたときに残りが出る

    def test_no_signal_warning_fires_once(self) -> None:
        warned = []
        rec = self._warm_recorder(on_no_signal=lambda: warned.append(1))
        with mock.patch("client.recorder.resolve_input_device", return_value=None):
            rec.start()
        rec._start_time -= 2.0
        self._feed(rec, silence(0.5))
        self.assertEqual(warned, [1])


class TestVoiceCommandsV2(unittest.TestCase):
    def test_symbols(self) -> None:
        self.assertEqual(
            apply_voice_commands("彼は、かぎかっこ、こんにちは、かぎかっこ閉じ、と言った。"),
            "彼は、「こんにちは」と言った。",
        )
        self.assertEqual(apply_voice_commands("本当、はてなマーク。"), "本当？")
        self.assertEqual(apply_voice_commands("すごい、びっくりマーク"), "すごい！")
        self.assertEqual(apply_voice_commands("かっこ開き、注、かっこ閉じ"), "（注）")

    def test_ordinary_words_are_kept(self) -> None:
        for t in ["びっくりした", "かっこいい", "はてなマークを付ける", "疑問符がつく", "かっこ悪い"]:
            self.assertEqual(apply_voice_commands(t), t)

    def test_undo_phrases(self) -> None:
        for t in ["今のなし", "今のなし。", "いまのなし", "今のは無し！", "取り消し", "取り消して", "今の消して"]:
            self.assertTrue(is_undo_command(t), t)
        for t in ["取り消しボタン", "今の話", "今のなしで行こう", ""]:
            self.assertFalse(is_undo_command(t), t)


class TestUndoFlow(unittest.TestCase):
    def _app(self):
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = ClientConfig(beep_enabled=False)
        app.ui = mock.MagicMock()
        app.hook = mock.MagicMock()
        app.hook.typed_key_count = 5
        app._last_paste = {"hwnd": 123, "text": "こんにちは", "time": time.monotonic(), "keys": 5}
        return app

    def test_undo_sends_to_same_window(self) -> None:
        app = self._app()
        with mock.patch("client.main.undo_paste", return_value=(True, "ctrl_z")) as up, \
                mock.patch("client.main.get_window_process_name", return_value="Discord.exe"):
            app._undo_last_paste()
        up.assert_called_once_with("こんにちは", 123, "Discord.exe")
        self.assertIsNone(app._last_paste)

    def test_undo_refused_after_typing(self) -> None:
        app = self._app()
        app.hook.typed_key_count = 9  # 貼り付け後にキーを打った
        with mock.patch("client.main.undo_paste") as up:
            app._undo_last_paste()
        up.assert_not_called()
        self.assertIn("キーを打っている", app.ui.show_status.call_args.args[0])

    def test_spoken_undo_does_not_paste(self) -> None:
        app = self._app()
        app._busy_lock = threading.Lock()
        app._processing = False
        app._tray = None
        app._status_text = ""
        app.transcriber = mock.MagicMock()
        app.transcriber.last_backend_status = "LAN (cpu)"
        app.transcriber.transcribe.return_value = mock.MagicMock(text="今のなし。", backend="LAN", elapsed_ms=10.0)
        app.dictionary = mock.MagicMock()
        app.dictionary.hotwords_csv = ""
        app.dictionary.remove_fillers.side_effect = lambda t: t
        app.dictionary.apply_replacements.side_effect = lambda t: t
        app.dictionary.normalize.side_effect = lambda t: t
        app.app_logger = mock.MagicMock()
        with mock.patch("client.main.paste_text") as pt, \
                mock.patch.object(app, "_undo_last_paste") as undo:
            app._process_audio(np.full(16000, 0.05, dtype=np.float32), 1.0, paste_target=123)
        undo.assert_called_once()
        pt.assert_not_called()
        self.assertFalse(app._processing)


class TestLongAudioWithoutLiveSession(unittest.TestCase):
    def test_long_audio_is_split_before_sending(self) -> None:
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = ClientConfig()
        app.dictionary = mock.MagicMock()
        app.dictionary.hotwords_csv = ""
        app.transcriber = mock.MagicMock()
        lengths = []

        def fake(samples, sample_rate, hotwords_csv):
            lengths.append(len(samples) / SR)
            return mock.MagicMock(text="あ。", backend="LAN-cpu", elapsed_ms=10.0)

        app.transcriber.transcribe.side_effect = fake
        text, backend, _ms = app._transcribe_whole(speech(45), 45.0)
        self.assertGreaterEqual(len(lengths), 3)
        self.assertTrue(all(sec <= 18.01 for sec in lengths))
        self.assertTrue(backend.endswith(f"×{len(lengths)}"))
        self.assertEqual(text, "あ。" * len(lengths))

class TestRecognizeSafely(unittest.TestCase):
    def _app(self):
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = ClientConfig()
        app.transcriber = mock.MagicMock()
        return app

    def test_readings_are_passed(self) -> None:
        # 辞書のカナの言い方は、優先語の読みとして渡す
        app = self._app()
        app.dictionary = mock.MagicMock()
        app.dictionary.keyword_readings.return_value = {"GitHub": ["ギットハブ"]}
        app.transcriber.transcribe.return_value = mock.MagicMock(text="GitHub", elapsed_ms=10.0)
        app._recognize_safely(speech(3), SR, "GitHub")
        self.assertEqual(app.transcriber.transcribe.call_args.kwargs["readings"], {"GitHub": ["ギットハブ"]})

    def test_recognized_once(self) -> None:
        app = self._app()
        app.dictionary = mock.MagicMock()
        app.dictionary.keyword_readings.return_value = {}
        app.transcriber.transcribe.return_value = mock.MagicMock(text="あ", elapsed_ms=10.0)
        app._recognize_safely(speech(8), SR, "ComfyUI")
        self.assertEqual(app.transcriber.transcribe.call_count, 1)

class TestRecognitionPlace(unittest.TestCase):
    def test_local_place_ignores_servers(self) -> None:
        from client.transcriber import HybridTranscriber

        cfg = ClientConfig(server_urls=["http://a:1"], recognition_place="local")
        tr = HybridTranscriber(cfg, ROOT)
        self.assertFalse(tr.uses_servers())
        self.assertEqual(tr.ordered_servers(), [])
        self.assertIsNone(tr.check_server_health())
        self.assertEqual(tr.last_backend_status, "Local")

    def test_old_settings_keep_using_servers(self) -> None:
        from client.transcriber import HybridTranscriber

        self.assertTrue(HybridTranscriber(ClientConfig(server_urls=["http://a:1"]), ROOT).uses_servers())
        self.assertFalse(HybridTranscriber(ClientConfig(), ROOT).uses_servers())


if __name__ == "__main__":
    unittest.main()
