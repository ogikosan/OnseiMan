from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from client.config import ClientConfig
from client.input_hook import MODIFIER_VKS, format_vk_name, parse_binding
from client.logger import AppLogger
from client.transcriber import clean_asr_text
from shared.audio import decode_wav_bytes, samples_to_wav_bytes
from shared.dictionary import CustomDictionary


class TestOnseiManCore(unittest.TestCase):
    def test_parse_binding_mouse_and_keys(self) -> None:
        self.assertEqual(parse_binding("mouse_x1"), ("mouse", "x1"))
        self.assertEqual(parse_binding("mouse_side2"), ("mouse", "x2"))
        self.assertEqual(parse_binding("mouse_middle"), ("mouse", "middle"))
        self.assertEqual(parse_binding("ralt"), ("key", 0xA5))
        self.assertEqual(parse_binding("muhenkan"), ("key", 0x1D))
        self.assertEqual(parse_binding("f2"), ("key", 0x71))
        self.assertEqual(parse_binding("vk:0x2D"), ("key", 0x2D))
        self.assertEqual(format_vk_name(0xA5), "ralt")
        with self.assertRaises(ValueError):
            parse_binding("unknown_super_key")

    def test_dictionary_hot_reload_and_replacement_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dict.json"
            p.write_text(
                json.dumps(
                    {
                        "hotwords": ["ComfyUI", "Fun-ASR"],
                        "replacements": {
                            "コンフィー": "Comfy",
                            "コンフィーUI": "ComfyUI",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            d = CustomDictionary(p)
            # 置き換え後の「正しい言葉」も、音声認識に渡す（重複はまとめる）
            self.assertEqual(d.hotwords_csv.split(",")[:2], ["ComfyUI", "Fun-ASR"])
            self.assertEqual(len(d.hotwords_csv.split(",")), len(set(d.hotwords_csv.split(","))))
            # Longer key must win over shorter prefix
            self.assertEqual(d.apply_replacements("コンフィーUIを使う"), "ComfyUIを使う")
            self.assertEqual(d.apply_replacements("コンフィーだけ"), "Comfyだけ")

    def test_wav_roundtrip(self) -> None:
        sr = 16000
        t = np.linspace(0, 0.25, int(sr * 0.25), endpoint=False, dtype=np.float32)
        orig = 0.5 * np.sin(2 * np.pi * 440.0 * t).astype(np.float32)
        wav_bytes = samples_to_wav_bytes(orig, sr)
        decoded, decoded_sr = decode_wav_bytes(wav_bytes)
        self.assertEqual(decoded_sr, sr)
        self.assertEqual(len(decoded), len(orig))
        self.assertLess(float(np.max(np.abs(decoded - orig))), 1e-3)

    def test_config_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.json"
            cfg = ClientConfig(hotkey="mouse_x2", server_urls=["http://192.168.1.50:8765"])
            cfg.save(p)
            loaded = ClientConfig.load(p)
            self.assertEqual(loaded.hotkey, "mouse_x2")
            self.assertEqual(loaded.server_urls, ["http://192.168.1.50:8765"])

    def test_logger_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_log = AppLogger(Path(tmp))
            app_log.record_transcription(
                backend="LAN-cuda",
                elapsed_ms=145.2,
                duration_sec=2.5,
                raw_text="コンフィーUIテスト",
                text="ComfyUIテスト",
            )
            self.assertEqual(app_log.last_text, "ComfyUIテスト")
            entries = app_log.get_history()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].backend, "LAN-cuda")
            self.assertTrue(app_log.history_txt.exists())
            app_log.close()

    def test_clean_asr_text(self) -> None:
        self.assertEqual(clean_asr_text("/sil"), "")
        self.assertEqual(clean_asr_text("  <|ja|>こんにちは /sil "), "こんにちは")
        self.assertEqual(clean_asr_text("普通の文章です。"), "普通の文章です。")

    def test_modifiers_never_suppressed(self) -> None:
        for key in ("ralt", "lalt", "rctrl", "lctrl", "rshift", "lshift"):
            kind, vk = parse_binding(key)
            self.assertEqual(kind, "key")
            self.assertIn(vk, MODIFIER_VKS)


def _kb_event(vk: int, msg: int, flags: int = 0):
    import ctypes
    from client.input_hook import KBDLLHOOKSTRUCT

    st = KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=0)
    return st, ctypes.addressof(st), msg


class TestFillerRemoval(unittest.TestCase):
    def setUp(self) -> None:
        from shared.fillers import FillerRemover

        self.r = FillerRemover()

    def test_user_example(self) -> None:
        self.assertEqual(
            self.r.remove("うん、そういえば、うん。うん、そういえばどうなんだろうなあ。"),
            "そういえば。そういえばどうなんだろうなあ。",
        )

    def test_common_fillers(self) -> None:
        self.assertEqual(self.r.remove("えーと、明日の会議は、あのー、10時からです。"), "明日の会議は、10時からです。")
        self.assertEqual(self.r.remove("えーっとそういえば資料まだだよね。"), "そういえば資料まだだよね。")
        self.assertEqual(self.r.remove("えーーと、あのぉ、うーん、何だっけ？"), "何だっけ？")
        self.assertEqual(self.r.remove("待ってくださいね、うん、うん。"), "待ってくださいね。")
        self.assertEqual(self.r.remove("うんうん、わかった。"), "わかった。")

    def test_words_inside_sentences_are_kept(self) -> None:
        for s in ("あの人がまあまあ良いと言っていた。", "ええ、そうです。", "なんかいい感じ。", "その件は確認します。"):
            self.assertEqual(self.r.remove(s), s)

    def test_only_fillers_becomes_empty(self) -> None:
        self.assertEqual(self.r.remove("うん。"), "")

    def test_dictionary_fillers_editable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dict.json"
            p.write_text(json.dumps({"fillers": ["まあ"]}, ensure_ascii=False), encoding="utf-8")
            dic = CustomDictionary(p)
            self.assertEqual(dic.remove_fillers("まあ、うん、いいか。"), "うん、いいか。")


class TestModulesImport(unittest.TestCase):
    def test_all_client_modules_import(self) -> None:
        """画面のファイルなど、起動時には読み込まれないモジュールの構文エラーも拾う。"""
        import importlib

        for name in (
            "client.main",
            "client.settings_ui",
            "client.ui",
            "client.watchdog",
            "client.app_context",
            "client.startup",
            "client.llm_refine",
            "client.uia_watcher",
            "shared.normalize",
            "shared.kana",
            "shared.voice_commands",
            "shared.correction_learner",
            "shared.self_correction",
        ):
            importlib.import_module(name)


class TestTextNormalize(unittest.TestCase):
    def setUp(self) -> None:
        from shared.normalize import TextNormalizer

        self.n = TextNormalizer()

    def test_kanji_numbers(self) -> None:
        cases = {
            "五千九十とかなるのはちょっとな": "5090とかなるのはちょっとな",
            "二十歳になった": "20歳になった",
            "十二時に集合": "12時に集合",
            "三十分後に": "30分後に",
            "五万三千円でした": "5万3000円でした",
            "一億二千万人": "1億2000万人",
            "三点五パーセント上がった": "3.5%上がった",
            "十パーセントオフ": "10%オフ",
            "二〇二六年": "2026年",
            "百円ショップ": "100円ショップ",
        }
        for src, want in cases.items():
            self.assertEqual(self.n.normalize(src), want, src)

    def test_single_digits_and_decimals(self) -> None:
        cases = {
            "五を押して": "5を押して",
            "バージョン五にして": "バージョン5にして",
            "答えは五です": "答えは5です",
            "五": "5",
            "OPAS五点五とOPAS四点八": "OPAS5.5とOPAS4.8",
            "五点五です": "5.5です",
            # 辞書で「五→5」にしていて「5点5」になっていても小数にする
            "OPAS5点5とOPAS4点8": "OPAS5.5とOPAS4.8",
            "4点八": "4.8",
            "四点8": "4.8",
            "一か月": "1か月",
        }
        for src, want in cases.items():
            self.assertEqual(self.n.normalize(src), want, src)

    def test_digits_inside_words_kept(self) -> None:
        for s in ("一緒に行く", "一番いい", "もう一度", "二人で", "第五回", "一つください", "五つある",
                  "一か八か", "九九を覚える", "一から十まで", "七夕", "九州に行く", "一応", "唯一の", "三点セット",
                  "十分間に合う"):
            self.assertEqual(self.n.normalize(s), s)

    def test_idioms_kept(self) -> None:
        for s in ("十分間に合う", "八百屋さん", "五十嵐さん", "千葉に行く", "万が一のため", "一人で行く",
                  "二三日待って", "一点一点確認", "百人一首", "十人十色", "三人で", "第一回"):
            self.assertEqual(self.n.normalize(s), s)

    def test_terms_and_width(self) -> None:
        self.assertEqual(self.n.normalize("ジーピーユーはアールティーエックス五千九十"), "GPUはRTX5090")
        self.assertEqual(self.n.normalize("ピーディーエフをユーアールエルで送る"), "PDFをURLで送る")
        self.assertEqual(self.n.normalize("アイデアがある"), "アイデアがある")
        self.assertEqual(self.n.normalize("５０９０とＧＰＵ"), "5090とGPU")

    def test_builtin_can_be_disabled_from_dictionary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dict.json"
            p.write_text(json.dumps({"builtin_off": ["クロード"]}, ensure_ascii=False), encoding="utf-8")
            dic = CustomDictionary(p)
            self.assertEqual(dic.normalize("クロードとジェミニ"), "クロードとGemini")

    def test_reading_registration_matches_katakana_word_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "dict.json"
            p.write_text(json.dumps({"replacements": {"すーぱーべーす": "Supabase", "あい": "藍"}}, ensure_ascii=False), encoding="utf-8")
            dic = CustomDictionary(p)
            self.assertEqual(dic.apply_replacements("スーパーベースを使う"), "Supabaseを使う")
            self.assertEqual(dic.apply_replacements("アイデアがある"), "アイデアがある")
            self.assertEqual(dic.apply_replacements("ぐあいが悪い"), "ぐあいが悪い")


class TestHotkeyMenuMask(unittest.TestCase):
    """右Alt を押したとき、メニューバーにフォーカスを奪われない対策が入ることを確認する。"""

    def _make_hook(self, hotkey: str, suppress: bool = True):
        from unittest import mock
        import client.input_hook as ih

        hook = ih.GlobalInputHook(
            hotkey=hotkey,
            cancel_key="esc",
            suppress_hotkey=suppress,
            on_press=lambda: None,
            on_release=lambda: None,
            on_cancel=lambda: None,
            is_recording_fn=lambda: False,
        )
        hook._stopped = True  # no threads started; keep atexit no-op
        patcher = mock.patch.object(ih, "send_menu_mask")
        self.addCleanup(patcher.stop)
        return hook, patcher.start()

    def _drain(self, hook):
        out = []
        while not hook._event_queue.empty():
            out.append(hook._event_queue.get_nowait()[0])
        return out

    def test_ralt_whole_keystroke_hidden_from_apps(self) -> None:
        """右Alt は押す・押しっぱなし・離すの全部をアプリに渡さない（Discord などでフォーカスが外れない）。"""
        from client.input_hook import WM_SYSKEYDOWN, WM_KEYUP

        hook, mask = self._make_hook("ralt")
        for msg in (WM_SYSKEYDOWN, WM_SYSKEYDOWN, WM_SYSKEYDOWN, WM_KEYUP):
            _st, ptr, m = _kb_event(0xA5, msg)
            self.assertEqual(hook._keyboard_proc(0, m, ptr), 1)
        self.assertEqual(mask.call_count, 0)
        self.assertEqual(self._drain(hook), ["press", "release"])
        self.assertEqual(hook._suppressed_down, set())

    def test_swallowed_down_always_gets_swallowed_up_even_after_rebinding(self) -> None:
        from client.input_hook import WM_SYSKEYDOWN, WM_KEYUP

        hook, _mask = self._make_hook("ralt")
        _st, ptr, m = _kb_event(0xA5, WM_SYSKEYDOWN)
        self.assertEqual(hook._keyboard_proc(0, m, ptr), 1)
        hook.update_bindings("f2", "esc", True)  # 押している最中にキー変更
        _st, ptr, m = _kb_event(0xA5, WM_KEYUP)
        self.assertEqual(hook._keyboard_proc(0, m, ptr), 1, "押す側を消したら離す側も必ず消す")
        # 次の右Alt は普通の Alt として通る
        _st, ptr, m = _kb_event(0xA5, WM_SYSKEYDOWN)
        self.assertNotEqual(hook._keyboard_proc(0, m, ptr), 1)

    def test_not_swallowed_down_up_passes_through(self) -> None:
        """フック開始前から押されていたキーの「離す」は通す（片側だけ消さない）。"""
        from client.input_hook import WM_KEYUP

        hook, _mask = self._make_hook("ralt")
        _st, ptr, m = _kb_event(0xA5, WM_KEYUP)
        self.assertNotEqual(hook._keyboard_proc(0, m, ptr), 1)

    def test_ralt_passthrough_mode_sends_menu_mask(self) -> None:
        from client.input_hook import WM_SYSKEYDOWN, WM_KEYUP

        hook, mask = self._make_hook("ralt", suppress=False)
        for msg in (WM_SYSKEYDOWN, WM_SYSKEYDOWN, WM_KEYUP):
            _st, ptr, m = _kb_event(0xA5, msg)
            self.assertNotEqual(hook._keyboard_proc(0, m, ptr), 1)
        self.assertEqual(mask.call_count, 1)

    def test_injected_events_are_ignored(self) -> None:
        from client.input_hook import WM_SYSKEYDOWN

        hook, mask = self._make_hook("ralt")
        _st, ptr, m = _kb_event(0xA5, WM_SYSKEYDOWN, flags=0x10)
        hook._keyboard_proc(0, m, ptr)
        self.assertEqual(mask.call_count, 0)
        self.assertEqual(self._drain(hook), [])

    def test_non_alt_hotkey_has_no_mask(self) -> None:
        from client.input_hook import WM_KEYDOWN, WM_KEYUP

        hook, mask = self._make_hook("f2")
        for msg in (WM_KEYDOWN, WM_KEYUP):
            _st, ptr, m = _kb_event(0x71, msg)
            self.assertEqual(hook._keyboard_proc(0, m, ptr), 1)  # suppressed
        self.assertEqual(mask.call_count, 0)
        self.assertEqual(self._drain(hook), ["press", "release"])


class TestAppBusyAndPasteFlow(unittest.TestCase):
    def _make_app(self):
        from unittest import mock
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = ClientConfig(beep_enabled=False)
        app._processing = False
        app._press_ignored = False
        app._toggle_active = False
        app._ai_recording = False
        app._ai_selected_text = ""
        app._last_audio = None
        app._last_audio_path = Path(tempfile.gettempdir()) / "onseiman_test_last.wav"
        app._max_timer = None
        app._paste_target = 0
        app._config_lock = __import__("threading").Lock()
        app.hook = mock.MagicMock()
        app._tray = None
        app._status_text = ""
        app.config_path = Path(tempfile.gettempdir()) / "onseiman_missing_config.json"
        app.ui = mock.MagicMock()
        app.recorder = mock.MagicMock()
        app.transcriber = mock.MagicMock()
        app.transcriber.last_backend_status = "LAN (cuda)"
        app._live = None
        app._rec_status = ("", "#000000")
        app._last_paste = None
        app.dictionary = mock.MagicMock()
        app.dictionary.hotwords_csv = ""
        app.app_logger = mock.MagicMock()
        return app

    def test_press_while_processing_keeps_overlay(self) -> None:
        app = self._make_app()
        app._processing = True
        app._on_hotkey_press()
        app._on_hotkey_release()
        app.recorder.start.assert_not_called()
        app.recorder.stop.assert_not_called()
        app.ui.hide_overlay.assert_not_called()
        self.assertIn("認識中", app.ui.show_status.call_args_list[0].args[0])

    def test_release_marks_busy_before_worker_runs(self) -> None:
        from unittest import mock

        app = self._make_app()
        app.config.hotkey_mode = "hold"
        app.recorder.stop.return_value = (np.full(16000, 0.05, dtype=np.float32), 1.0, False)
        with mock.patch("client.main.threading.Thread") as th:
            app._on_hotkey_release()
        self.assertTrue(app._processing)
        th.return_value.start.assert_called_once()

    def test_toggle_press_starts_release_keeps_recording_second_press_finishes(self) -> None:
        from unittest import mock

        app = self._make_app()  # hotkey_mode は既定で toggle
        app.recorder.start.return_value = True
        app.recorder.stop.return_value = (np.full(16000, 0.05, dtype=np.float32), 1.0, False)
        app._process_audio = mock.MagicMock()
        if True:
            app._on_hotkey_press()      # 1回目: 録音開始
            app._on_hotkey_release()    # 指を離す: 何もしない
            app.recorder.stop.assert_not_called()
            self.assertTrue(app._toggle_active)
            self.assertIsNotNone(app._max_timer)
            app._on_hotkey_press()      # 2回目: 確定
            app._on_hotkey_release()
        app.recorder.start.assert_called_once()
        app.recorder.stop.assert_called_once()
        self.assertFalse(app._toggle_active)
        self.assertIsNone(app._max_timer)
        import time
        time.sleep(0.1)
        app._process_audio.assert_called_once()  # 認識処理へ
        app.ui.show_pill.assert_called_once()      # 録音中は × と ✓ の小さなバー（既定）
        app.ui.hide_pill.assert_called()            # 確定したら消える

    def test_text_indicator_mode_shows_guide(self) -> None:
        from unittest import mock

        app = self._make_app()
        app.config.recording_indicator = "text"
        app.recorder.start.return_value = True
        app._on_hotkey_press()
        app.ui.show_pill.assert_not_called()
        self.assertIn("もう一度", app.ui.show_status.call_args_list[0].args[0])
        app._cancel_max_timer()

    def test_toggle_cancel_then_next_press_starts_new_recording(self) -> None:
        app = self._make_app()
        app.recorder.start.return_value = True
        app._on_hotkey_press()
        app._on_cancel()
        self.assertFalse(app._toggle_active)
        self.assertIsNone(app._max_timer)
        app._on_hotkey_press()  # キャンセル後の押下は「新しい録音の開始」
        self.assertEqual(app.recorder.start.call_count, 2)
        app.recorder.stop.assert_not_called()
        app._cancel_max_timer()

    def test_toggle_max_duration_auto_finishes(self) -> None:
        from unittest import mock

        app = self._make_app()
        app.config.max_duration_sec = 0.05
        app.recorder.start.return_value = True
        app.recorder.stop.return_value = (np.full(16000, 0.05, dtype=np.float32), 1.0, False)
        app.hook.run_on_worker.side_effect = lambda fn: fn()
        app._process_audio = mock.MagicMock()
        app._on_hotkey_press()
        import time
        time.sleep(0.4)
        app.recorder.stop.assert_called_once()
        self.assertFalse(app._toggle_active)
        app._process_audio.assert_called_once()

    def test_silent_recording_is_not_sent(self) -> None:
        from unittest import mock

        app = self._make_app()
        app.recorder.start.return_value = True
        app.recorder.stop.return_value = (np.zeros(16000, dtype=np.float32), 1.0, False)
        app._process_audio = mock.MagicMock()
        app._on_hotkey_press()
        app._on_hotkey_press()
        import time
        time.sleep(0.1)
        app._process_audio.assert_not_called()
        self.assertIn("マイクの音が入っていません", app.ui.show_status.call_args_list[-1].args[0])
        app.app_logger.record_failure.assert_called_once()  # 無音でも履歴に残して「再試行」できる

    def test_paste_failure_is_reported(self) -> None:
        from unittest import mock
        import threading
        from client.paste import PasteResult

        app = self._make_app()
        app._busy_lock = threading.Lock()
        app.dictionary = mock.MagicMock()
        app.dictionary.remove_fillers.side_effect = lambda t: t
        app.dictionary.apply_replacements.side_effect = lambda t: t
        app.dictionary.normalize.side_effect = lambda t: t
        app.app_logger = mock.MagicMock()
        app.transcriber.transcribe.return_value = mock.MagicMock(text="こんにちは", backend="LAN", elapsed_ms=100.0)
        with mock.patch("client.main.paste_text", return_value=PasteResult(False, "elevated_window", True, "管理者")):
            with self.assertLogs("onseiman.client", level="WARNING") as logs:
                app._process_audio(np.ones(16000, dtype=np.float32) * 0.1, 1.0)
        self.assertIn("貼り付け失敗", logs.output[0])
        msg = app.ui.show_status.call_args_list[-1].args[0]
        self.assertIn("Ctrl+V", msg)
        self.assertFalse(app._processing)


class TestNewFeatures(unittest.TestCase):
    def test_voice_commands_newline_and_safety(self) -> None:
        from shared.voice_commands import apply_voice_commands

        self.assertEqual(apply_voice_commands("こんにちは。改行、次の行です。"), "こんにちは。\n次の行です。")
        self.assertEqual(apply_voice_commands("1番目かいぎょう2番目"), "1番目\n2番目")
        self.assertEqual(apply_voice_commands("テスト カイギョウ テスト2"), "テスト\nテスト2")
        # 通常の日本語名詞・動詞は壊さない
        for safe in ("改行コードを変えて", "改行文字を入れる", "自動改行をオン", "ここで改行する", "改行なし"):
            self.assertEqual(apply_voice_commands(safe), safe)

    def test_app_rules_chat_trailing_period(self) -> None:
        from client.app_context import apply_app_rules

        self.assertEqual(apply_app_rules("了解です。", "Discord.exe"), "了解です")
        self.assertEqual(apply_app_rules("行1。\n行2。", "slack.exe"), "行1\n行2")
        # 疑問符や連続句点は残す
        self.assertEqual(apply_app_rules("本当に？", "Discord.exe"), "本当に？")
        self.assertEqual(apply_app_rules("なるほど。。", "Discord.exe"), "なるほど。。")
        # エディタやメモ帳ではそのまま
        self.assertEqual(apply_app_rules("了解です。", "Code.exe"), "了解です。")
        self.assertEqual(apply_app_rules("了解です。", "Discord.exe", enabled=False), "了解です。")
        # デフォルト設定で全アプリ末尾「。」削除が有効
        self.assertTrue(ClientConfig().strip_trailing_period)

    def test_quick_add_dictionary_replacement_and_hotword(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "dict.json"
            p.write_text(json.dumps({"hotwords": ["ComfyUI"], "replacements": {}}, ensure_ascii=False), encoding="utf-8")
            dic = CustomDictionary(p)
            dic.add_replacement("すーぱーべーす", "Supabase", also_hotword=True)
            dic.add_replacement("ギッドハブ", "GitHub", also_hotword=True)
            dic.add_replacement("山駄さん", "山田さん", also_hotword=True)
            self.assertEqual(dic.apply_replacements("スーパーベースを使う"), "Supabaseを使う")
            self.assertEqual(dic.apply_replacements("ギッドハブアクションは使いません"), "GitHubアクションは使いません")
            self.assertEqual(dic.apply_replacements("山駄さんの場合"), "山田さんの場合")
            self.assertIn("Supabase", dic.hotwords)
            self.assertIn("山田さん", dic.hotwords)

    def test_silence_auto_stop_only_after_speech(self) -> None:
        import time
        from client.recorder import AudioRecorder

        fired = []
        rec = AudioRecorder(
            sample_rate=16000,
            auto_stop_on_silence=True,
            silence_stop_sec=0.12,
            silence_rms_threshold=0.01,
            on_silence_timeout=lambda: fired.append(time.perf_counter()),
        )
        rec._recording = True
        rec._start_time = time.perf_counter()
        rec._speech_detected = False
        rec._last_speech_time = rec._start_time
        rec._silence_fired = False

        quiet = np.zeros((1024, 1), dtype=np.float32)
        loud = np.full((1024, 1), 0.08, dtype=np.float32)

        # 話し始める前の無音では発火しない
        rec._callback(quiet, 1024, None, None)
        time.sleep(0.16)
        rec._callback(quiet, 1024, None, None)
        self.assertEqual(len(fired), 0)

        # 発話を検知したあとに無音が続くと1回だけ発火する
        rec._callback(loud, 1024, None, None)
        self.assertEqual(len(fired), 0)
        time.sleep(0.16)
        rec._callback(quiet, 1024, None, None)
        rec._callback(quiet, 1024, None, None)
        self.assertEqual(len(fired), 1)

    def test_llm_refiner_ollama_and_openai_mock(self) -> None:
        from unittest import mock
        from client.llm_refine import LLMRefiner

        with mock.patch("client.llm_refine.requests.get") as mget, mock.patch("client.llm_refine.requests.post") as mpost:
            mget.return_value = mock.MagicMock(status_code=200, json=lambda: {"models": [{"name": "qwen2.5:7b"}]})
            mpost.return_value = mock.MagicMock(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"message": {"content": "明後日の15時でお願いします。"}},
            )
            ref = LLMRefiner("http://192.168.1.50:11434")
            res = ref.refine_dictation("明日、あいや明後日の3時でお願いします")
            self.assertEqual(res.text, "明後日の15時でお願いします。")
            self.assertEqual(res.model, "qwen2.5:7b")

        with mock.patch("client.llm_refine.requests.post") as mpost:
            mpost.return_value = mock.MagicMock(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": "Hello world"}}]},
            )
            ref2 = LLMRefiner("http://192.168.1.50:1234/v1", model="local-llm")
            res2 = ref2.rewrite_selection("こんにちは世界", "英語にして")
            self.assertEqual(res2.text, "Hello world")

    def test_secondary_ai_hotkey_binding(self) -> None:
        from client.input_hook import GlobalInputHook, KBDLLHOOKSTRUCT, WM_KEYDOWN, WM_KEYUP
        import ctypes

        hook = GlobalInputHook(
            "ralt", "esc", True,
            lambda: None, lambda: None, lambda: None, lambda: False,
            secondary_hotkey="f9",
            on_secondary_press=lambda: None,
            on_secondary_release=lambda: None,
        )
        self.assertEqual(hook.secondary_binding, ("key", 0x78))
        st_down = KBDLLHOOKSTRUCT(vkCode=0x78, scanCode=0, flags=0, time=0, dwExtraInfo=0)
        st_up = KBDLLHOOKSTRUCT(vkCode=0x78, scanCode=0, flags=0, time=0, dwExtraInfo=0)
        self.assertEqual(hook._keyboard_proc(0, WM_KEYDOWN, ctypes.addressof(st_down)), 1)
        self.assertEqual(hook._keyboard_proc(0, WM_KEYUP, ctypes.addressof(st_up)), 1)
        events = []
        while not hook._event_queue.empty():
            events.append(hook._event_queue.get_nowait()[0])
        self.assertEqual(events, ["secondary_press", "secondary_release"])

    def test_startup_command_builder(self) -> None:
        from client.startup import build_startup_command

        cmd = build_startup_command(ROOT_DIR)
        self.assertIn("client\\main.py", cmd)
        self.assertIn("pythonw.exe", cmd.lower())

    def test_correction_extraction_and_safety(self) -> None:
        from shared.correction_learner import CorrectionCandidateStore, extract_corrections

        # 敬称付き自動拡張
        self.assertEqual(
            extract_corrections("山駄さんの場合、どうするのかな？", "山田さんの場合、どうするのかな？"),
            [("山駄さん", "山田さん")],
        )
        # 1文字差分は熟語単位に自動拡張（実→出 ではなく 実力→出力）
        self.assertEqual(
            extract_corrections("実力テキストに含める", "出力テキストに含める"),
            [("実力", "出力")],
        )
        self.assertEqual(
            extract_corrections("再起動してください", "再始動してください"),
            [("再起動", "再始動")],
        )
        # 拡張しても1文字だけの置換は誤爆防止のため拾わない
        self.assertEqual(extract_corrections("私が行くよ", "俺が行くよ"), [])
        # 複合語の一部修正
        self.assertEqual(
            extract_corrections("ギッドハブアクションは使いません", "GitHubアクションは使いません"),
            [("ギッドハブ", "GitHub")],
        )
        # 既存の長文が入力欄にあってもペースト部分だけ切り出して抽出できる
        base = "以前から書いていた長文のメモです。\n山駄さんの場合どうする？\n末尾の署名"
        edited = "以前から書いていた長文のメモです。\n山田さんの場合どうする？\n末尾の署名"
        self.assertEqual(
            extract_corrections("山駄さんの場合どうする？", edited, baseline_full=base),
            [("山駄さん", "山田さん")],
        )
        # 短い1フレーズ丸ごとの書き直し（カタカナ→ひらがな・英字など共通文字0でも拾う）
        self.assertEqual(
            extract_corrections("ゾングリコロコロダンブリ", "どんぐりころころどんぶりこ"),
            [("ゾングリコロコロダンブリ", "どんぐりころころどんぶりこ")],
        )
        # 助詞の手直しや単なる追記・別文章への書き換えは拾わない
        self.assertEqual(extract_corrections("これはテストです。", "これがテストです。"), [])
        self.assertEqual(extract_corrections("今日は晴れです。", "今日は晴れです。明日も晴れです。"), [])
        self.assertEqual(extract_corrections("おはよう。", "全く別の文章に書き換えました。"), [])

        # 候補ストアの回数カウント＆無視リスト
        with tempfile.TemporaryDirectory() as tmp:
            st = CorrectionCandidateStore(Path(tmp) / "cands.json")
            c1 = st.record("山駄さん", "山田さん", example_before="山駄さん", example_after="山田さん")
            self.assertIsNotNone(c1)
            self.assertEqual(c1.count, 1)
            c2 = st.record("山駄さん", "山田さん")
            self.assertEqual(c2.count, 2)
            st.remove("山駄さん", "山田さん", ignore_future=True)
            self.assertIsNone(st.record("山駄さん", "山田さん"))

    def test_self_correction_detection_and_rule_fallback(self) -> None:
        from shared.self_correction import has_self_correction, is_over_truncated, rule_based_self_correction

        self.assertTrue(has_self_correction("今すぐデプロイして。いやいやそうじゃなくて、先にテストして。"))
        self.assertTrue(has_self_correction("明日の3時に、じゃなくて4時に集合で。"))
        self.assertTrue(has_self_correction("5090で回して、あ、違う、4070tiで回して。"))
        self.assertTrue(has_self_correction("うんちして、いやいやそうじゃなくてちんこして"))
        # 普通の文章や、後ろに新しい言い直し内容がない文末は反応しない
        self.assertFalse(has_self_correction("今日はいい天気ですね。"))
        self.assertFalse(has_self_correction("それは違うと思います。"))
        self.assertFalse(has_self_correction("いやらしい表現は避けてください。"))
        self.assertFalse(has_self_correction("いいのかな？どんな感じなのかな？いや、やっぱりそうじゃないのかな。"))
        self.assertFalse(has_self_correction("やっぱりそういう時ってどうなるのかな。いや、やっぱりそうじゃなくて。"))
        # 削りすぎ安全ガード
        self.assertTrue(is_over_truncated("いいのかな？どんな感じなのかな？いや、やっぱりそうじゃないのかな。", "いいのかな？"))
        self.assertFalse(is_over_truncated("5090で回して、あ、じゃなくてミニPCの方で動かして。", "ミニPCの方で動かして。"))

        self.assertEqual(
            rule_based_self_correction("今すぐデプロイして。いやいやそうじゃなくて、先にテストして。"),
            "先にテストして。",
        )
        self.assertEqual(
            rule_based_self_correction("お疲れ様です。今すぐデプロイして。いやいやそうじゃなくて、先にテストして。"),
            "お疲れ様です。先にテストして。",
        )
        self.assertEqual(
            rule_based_self_correction("5090で回して、いやそうじゃなくて4070tiで回して。"),
            "4070tiで回して。",
        )
        self.assertEqual(
            rule_based_self_correction("うんちして、いやいやそうじゃなくてちんこして"),
            "ちんこして",
        )
        self.assertEqual(
            rule_based_self_correction("やっぱりそういう時ってどうなるのかな。うんちして、いやいやそうじゃなくてちんこして"),
            "やっぱりそういう時ってどうなるのかな。ちんこして",
        )

    def test_web_ui_server_endpoints(self) -> None:
        import requests
        from client.web_ui import WebUIServer

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg_path = p / "config.json"
            dict_path = p / "dict.json"
            ClientConfig().save(cfg_path)
            dict_path.write_text(json.dumps({"hotwords": ["ComfyUI"], "replacements": {"コンフィー": "Comfy"}}), encoding="utf-8")
            quick_calls = []
            srv = WebUIServer(
                dict_path=dict_path,
                config_path=cfg_path,
                get_history_fn=lambda: [],
                get_status_info=lambda: ("LAN (cpu)", "ralt"),
                get_learning_candidates_fn=lambda: [],
                on_copy_text=lambda _t: None,
                on_quick_add_dict=lambda s, d, h: quick_calls.append((s, d, h)),
                on_retranscribe_last=lambda: None,
                on_approve_candidate=lambda _s, _d: None,
                on_reject_candidate=lambda _s, _d: None,
                on_learn_from_manual_edit=lambda _b, _a: 1,
                get_hotkey=lambda: "ralt",
                preferred_port=0,
            )
            url = srv.start()
            try:
                r_html = requests.get(f"{url}/", timeout=3)
                self.assertEqual(r_html.status_code, 200)
                self.assertIn("ONSEIMAN", r_html.text)
                self.assertEqual(requests.get(f"{url}/style.css", timeout=3).status_code, 200)
                self.assertEqual(requests.get(f"{url}/bg.jpg", timeout=3).status_code, 200)

                st = requests.get(f"{url}/api/state", timeout=3).json()
                self.assertEqual(st["backend"], "LAN (cpu)")
                self.assertEqual(st["hotkey"], "ralt")

                d = requests.get(f"{url}/api/dict", timeout=3).json()
                self.assertEqual(d["replacements"]["コンフィー"], "Comfy")
                r_save = requests.post(
                    f"{url}/api/dict",
                    json={"replacements": {"ゾングリ": "どんぐり"}, "hotwords": ["どんぐり"], "fillers": ["えーと"], "builtin_off": []},
                    timeout=3,
                )
                self.assertEqual(r_save.status_code, 200)
                self.assertEqual(json.loads(dict_path.read_text(encoding="utf-8"))["replacements"]["ゾングリ"], "どんぐり")

                r_q = requests.post(f"{url}/api/quick_dict", json={"src": "山駄さん", "dst": "山田さん", "also_hotword": True}, timeout=3)
                self.assertEqual(r_q.status_code, 200)
                self.assertEqual(quick_calls, [("山駄さん", "山田さん", True)])

                s = requests.get(f"{url}/api/settings", timeout=3).json()
                self.assertTrue(s["strip_trailing_period"])
                s["strip_trailing_period"] = False
                self.assertEqual(requests.post(f"{url}/api/settings", json=s, timeout=3).status_code, 200)
                self.assertFalse(ClientConfig.load(cfg_path).strip_trailing_period)
            finally:
                srv.stop()

    def test_app_instantiation_and_web_ui_wiring(self) -> None:
        from client.main import OnseiManApp

        app = OnseiManApp()
        try:
            self.assertIsNotNone(app.web_ui)
            payload = app.web_ui.get_state_payload()
            self.assertIn("backend", payload)
            self.assertIn("history", payload)
        finally:
            app.web_ui.stop()
            app.app_logger.close()


class TestHotwordBudget(unittest.TestCase):
    def test_hotwords_capped_and_segment_shortened(self) -> None:
        import json as _json
        import tempfile as _tf
        from pathlib import Path as _P
        from unittest import mock as _mock
        from shared.dictionary import CustomDictionary
        from client.main import OnseiManApp

        with _tf.TemporaryDirectory() as dd:
            p = _P(dd) / "dict.json"
            p.write_text(_json.dumps({"hotwords": [f"w{i}" for i in range(60)], "replacements": {"x": "最新"}},
                                     ensure_ascii=False), encoding="utf-8")
            d = CustomDictionary(p)
            words = d.active_hotwords()
            self.assertEqual(len(words), CustomDictionary.MAX_HOTWORDS)
            self.assertEqual(words[-1], "最新")  # 新しく登録したものが残る
            app = OnseiManApp.__new__(OnseiManApp)
            app.config = ClientConfig(recognition_place="local")
            app.dictionary = d
            # Haas-ja は 1 回に 1 つの発話（15 秒まで）。優先語の数では短くならない
            self.assertEqual(app._max_segment_sec(), 15.0)
            # 認識役がもっと短い長さを言えば、それに合わせる
            app.config = ClientConfig(recognition_place="server", server_urls=["http://a:1"])
            app.transcriber = _mock.MagicMock()
            app.transcriber.preferred_server_info.return_value = {"max_segment_sec": 10.0}
            self.assertEqual(app._max_segment_sec(), 10.0)


if __name__ == "__main__":
    unittest.main()
