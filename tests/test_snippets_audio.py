"""定型文（合言葉 → 登録した文）と、履歴の録音を聞くための配信のテスト。"""
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
from shared.dictionary import CustomDictionary  # noqa: E402


def _dict(tmp: Path, snippets: dict) -> CustomDictionary:
    p = tmp / "dict.json"
    p.write_text(json.dumps({"hotwords": [], "replacements": {}, "snippets": snippets}, ensure_ascii=False), encoding="utf-8")
    return CustomDictionary(p)


class TestSnippets(unittest.TestCase):
    def test_whole_utterance_expands(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dic = _dict(Path(d), {"メールアドレス": "taro@example.com", "いつもの挨拶": "お世話になっております。\n山田です。"})
            self.assertEqual(dic.apply_snippets("メールアドレス"), ("taro@example.com", True))
            self.assertEqual(dic.apply_snippets("メールアドレス。"), ("taro@example.com", True))  # 句読点は無視
            self.assertEqual(dic.apply_snippets("いつもの挨拶")[0], "お世話になっております。\n山田です。")

    def test_part_of_sentence_is_not_expanded(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dic = _dict(Path(d), {"メールアドレス": "taro@example.com"})
            for t in ["メールアドレスを教えてください", "私のメールアドレス", ""]:
                self.assertEqual(dic.apply_snippets(t), (t, False))

    def test_kana_trigger_matches_katakana(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            dic = _dict(Path(d), {"じゅうしょ": "東京都千代田区1-1"})
            self.assertEqual(dic.apply_snippets("ジュウショ"), ("東京都千代田区1-1", True))

    def test_snippet_is_pasted_verbatim(self) -> None:
        """定型文は、文末の「。」削りや言い直しの整理にかけず、登録したとおりに入る。"""
        from client.main import OnseiManApp
        from client.paste import PasteResult

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            app = OnseiManApp.__new__(OnseiManApp)
            app.config = ClientConfig(beep_enabled=False, strip_trailing_period=True)
            app.ui = mock.MagicMock()
            app.hook = mock.MagicMock()
            app.hook.typed_key_count = 0
            app._busy_lock = threading.Lock()
            app._processing = False
            app._tray = None
            app._status_text = ""
            app._last_paste = None
            app.edit_watcher = None
            app.recorder = mock.MagicMock(is_recording=False)
            app.transcriber = mock.MagicMock(last_backend_status="Local CPU")
            app.transcriber.transcribe.return_value = mock.MagicMock(text="いつもの挨拶。", backend="Local-CPU", elapsed_ms=10.0)
            app.dictionary = _dict(d, {"いつもの挨拶": "お世話になっております。"})
            app.app_logger = AppLogger(d)
            try:
                with mock.patch("client.main.paste_text", return_value=PasteResult(True, "pasted", False, "メモ帳", 1)) as pt:
                    app._process_audio(np.full(16000, 0.05, dtype=np.float32), 1.0, paste_target=1)
                self.assertEqual(pt.call_args.args[0], "お世話になっております。")  # 「。」が残る
                h = app.app_logger.get_history()[0]
                self.assertEqual(h.text, "お世話になっております。")
                self.assertIn("定型文", h.backend)
                self.assertEqual(h.raw_text, "いつもの挨拶。")
            finally:
                app.app_logger.close()


class TestAudioEndpoint(unittest.TestCase):
    def test_serves_only_wav_in_audio_dir(self) -> None:
        import requests
        from client.web_ui import WebUIServer

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "audio").mkdir()
            (p / "audio" / "a.wav").write_bytes(b"RIFF" + b"\0" * 100)
            (p / "secret.txt").write_text("x", encoding="utf-8")
            (p / "audio" / "note.txt").write_text("x", encoding="utf-8")
            ClientConfig().save(p / "config.json")
            (p / "dict.json").write_text("{}", encoding="utf-8")
            lg = AppLogger(p)
            lg.record_transcription(backend="b", elapsed_ms=1, duration_sec=1, raw_text="a", text="a", audio="a.wav")
            lg.record_transcription(backend="b", elapsed_ms=1, duration_sec=1, raw_text="b", text="b", audio="gone.wav")
            srv = WebUIServer(
                dict_path=p / "dict.json", config_path=p / "config.json", get_history_fn=lg.get_history,
                get_status_info=lambda: ("Local CPU", "ralt"), get_learning_candidates_fn=lambda: [],
                on_copy_text=lambda _t: None, on_quick_add_dict=lambda s, d, h: None, on_retranscribe_last=lambda: None,
                on_approve_candidate=lambda _s, _d: None, on_reject_candidate=lambda _s, _d: None,
                on_learn_from_manual_edit=lambda _b, _a: 1, get_hotkey=lambda: "ralt", preferred_port=0,
                audio_dir=p / "audio",
            )
            url = srv.start()
            try:
                r = requests.get(f"{url}/api/audio/a.wav", timeout=3)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.content), 104)
                r2 = requests.get(f"{url}/api/audio/a.wav", headers={"Range": "bytes=0-3"}, timeout=3)
                self.assertEqual((r2.status_code, r2.content), (206, b"RIFF"))
                for bad in ("note.txt", "..%2Fsecret.txt", "..%5Csecret.txt", "nothing.wav"):
                    self.assertIn(requests.get(f"{url}/api/audio/{bad}", timeout=3).status_code, (403, 404), bad)
                hist = {h["text"]: h for h in requests.get(f"{url}/api/state", timeout=3).json()["history"]}
                self.assertTrue(hist["a"]["has_audio"])
                self.assertFalse(hist["b"]["has_audio"])  # 録音が消えた行には ▶ を出さない
                # 定型文の保存と、古い画面（snippets を送らない）で消えないこと
                requests.post(f"{url}/api/dict", json={"snippets": {"あいさつ": "こんにちは"}}, timeout=3)
                requests.post(f"{url}/api/dict", json={"hotwords": ["LAN"]}, timeout=3)
                saved = json.loads((p / "dict.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["snippets"], {"あいさつ": "こんにちは"})
                self.assertEqual(saved["hotwords"], ["LAN"])
            finally:
                srv.stop()
                lg.close()


if __name__ == "__main__":
    unittest.main()
