"""OnseiMan Windows タスクトレイ常駐クライアント。"""
from __future__ import annotations

import ctypes
import faulthandler
import json
import logging
import os
import sys
import threading
import time
import winsound
from pathlib import Path
from typing import Optional


def _lock_dpi_unaware() -> None:
    """画面の拡大率の扱いを「拡大率を考えない」に固定する（Windows は最初に決めたものしか受け付けない）。

    OnseiMan の画面（Tk）と録音中の表示（client/pill.py）は、この扱いで座標を計算している。
    手直しの見張りが使う uiautomation は、読み込んだときにアプリ全体を「画面ごとの実際の画素」に
    切り替えてしまう。最初の入力のあとから座標が拡大率の分（150% なら 1.5 倍）ずれ、
    録音中の表示が画面の外に出て「最初の 1 回しか出ない」ようになっていた（2026-11-23）。
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(0)  # PROCESS_DPI_UNAWARE
    except Exception:
        pass


_lock_dpi_unaware()

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
import pystray  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from client.paths import DATA_DIR, FROZEN, LOG_DIR  # noqa: E402
from client.version import __version__  # noqa: E402

from client.caret import get_caret_rect
from client.audio_mute import OtherAudioMuter
from client.app_context import apply_app_rules, get_window_process_name, strip_chat_trailing_period
from client.config import ClientConfig
from client.input_hook import GlobalInputHook
from client.llm_refine import LLMRefiner
from client.logger import AppLogger
from client.live_session import LiveTranscription
from client.paste import capture_paste_target, capture_selected_text, paste_text, set_clipboard_text, undo_paste
from client.recorder import AudioRecorder, list_input_devices
from client.startup import is_startup_enabled, set_startup_enabled
from client.transcriber import HybridTranscriber
from client.ui import UIController
from client.uia_watcher import EditCorrectionWatcher
from client.web_ui import WebUIServer
from client.watchdog import Heartbeat, release_stuck_modifiers, spawn_watchdog
from shared.audio import decode_wav_bytes, samples_to_wav_bytes
from shared.correction_learner import CorrectionCandidateStore, extract_corrections
from shared.dictionary import CustomDictionary
from shared.dict_sync import DEFAULT_NAME, NAME_RE, DictSync, FolderStore, ServerStore
from shared.fillers import DEFAULT_FILLERS
from shared.segmenter import join_texts, split_on_pauses
from shared.self_correction import has_self_correction, is_over_truncated, rule_based_self_correction
from shared.voice_commands import apply_voice_commands, is_undo_command

logger = logging.getLogger("onseiman.client")

_crash_log_file = None


def _enable_crash_log() -> None:
    """Python ごと落ちる種類のエラー（不正なメモリアクセス等）でも、全スレッドの実行箇所を logs/crash.log に残す。"""
    global _crash_log_file
    try:
        log_dir = LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        _crash_log_file = open(log_dir / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_crash_log_file, all_threads=True)
    except Exception:
        pass


def _make_icon_image(color: str) -> Image.Image:
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    # Simple microphone glyph in white
    draw.rounded_rectangle((24, 15, 40, 38), radius=8, fill="white")
    draw.arc((18, 24, 46, 46), start=0, end=180, fill="white", width=4)
    draw.line((32, 46, 32, 53), fill="white", width=4)
    draw.line((23, 53, 41, 53), fill="white", width=4)
    return img


def _open_path_in_shell(path: Path) -> None:
    """ログなどのテキストをメモ帳で開く（拡張子の関連付けに頼ると「アプリを選択」が出るため）。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    import subprocess

    notepad = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "notepad.exe"
    subprocess.Popen([str(notepad), str(path)])


class OnseiManApp:
    def __init__(self) -> None:
        self.config_path = DATA_DIR / "config.json"
        self.config = ClientConfig.load(self.config_path)
        self._config_mtime = self.config_path.stat().st_mtime if self.config_path.exists() else -1.0

        self.app_logger = AppLogger(LOG_DIR)

        dict_path = Path(self.config.dict_path)
        if not dict_path.is_absolute():
            dict_path = DATA_DIR / dict_path
        self.dictionary = CustomDictionary(dict_path)
        self.candidate_store = CorrectionCandidateStore(LOG_DIR / "learning_candidates.json")
        try:
            moved = self.candidate_store.dismiss_rewrites()
            if moved:
                logger.info("文の書き直しだった学習候補 %d 件を「消した候補」に移しました（戻せます）", moved)
        except Exception:
            logger.exception("学習候補の片付けに失敗しました")
        self.edit_watcher = EditCorrectionWatcher()

        self.ui = UIController(
            show_overlay=self.config.show_overlay,
            get_history_fn=self.app_logger.get_history,
            on_change_hotkey=self._start_hotkey_capture,
            on_open_dict=lambda: self.ui.open_dict_editor(),
            on_open_config=lambda: self.ui.open_settings(),
            get_status_info=lambda: (self.transcriber.last_backend_status, self._hotkey_label()),
            on_quick_add_dict=self._quick_add_dict,
            on_retranscribe_last=self._retranscribe_last,
            get_learning_candidates_fn=self.candidate_store.list_candidates,
            on_approve_candidate=self._approve_learning_candidate,
            on_reject_candidate=self._reject_learning_candidate,
            on_learn_from_manual_edit=self._learn_from_manual_edit,
        )
        self.recorder = AudioRecorder(
            sample_rate=self.config.sample_rate,
            device=self.config.microphone,
            max_duration_sec=self.config.max_duration_sec,
            on_level=self.ui.update_level,
            auto_stop_on_silence=self._is_toggle_mode() and self.config.auto_stop_on_silence,
            silence_stop_sec=self.config.silence_stop_sec,
            silence_rms_threshold=self.config.silence_rms_threshold,
            on_silence_timeout=self._on_silence_timeout,
            keep_open=self.config.keep_mic_open,
            preroll_sec=self.config.preroll_sec,
            on_segment=self._on_segment,
            segment_kwargs=self._segment_kwargs(),
            on_no_signal=self._on_no_signal,
        )
        self.transcriber = HybridTranscriber(self.config, DATA_DIR)
        self.dict_sync = DictSync(
            self.dictionary.path,
            LOG_DIR / "dict_sync.json",
            self._dict_store,
            default_fillers=list(DEFAULT_FILLERS),
            on_changed=self._on_dict_synced,
        )
        self.audio_muter = OtherAudioMuter(LOG_DIR / "muted_sessions.json")
        self.ui.on_overlay_moved = self._on_overlay_moved
        self.ui.set_overlay_pinned(self.config.overlay_position)
        self.ui.set_overlay_place(self.config.overlay_place)
        self.ui.on_pill_cancel = lambda: self.hook.run_on_worker(self._on_cancel)
        self.ui.on_pill_finish = lambda: self.hook.run_on_worker(self._finish_from_pill)
        self.ui.on_pill_undo = lambda: self.hook.run_on_worker(self._undo_last_paste)
        self._config_lock = threading.Lock()
        self.ui.configure_editors(
            dict_path=self.dictionary.path,
            config_path=self.config_path,
            on_config_saved=self._on_settings_saved,
            on_dict_saved=lambda: self.dictionary.reload(force=True),
            list_microphones=list_input_devices,
            get_hotkey=lambda: self.config.hotkey,
            get_ai_hotkey=lambda: self.config.ai_hotkey,
            on_change_ai_hotkey=self._start_ai_hotkey_capture,
            on_clear_ai_hotkey=self._clear_ai_hotkey,
            is_startup_enabled=is_startup_enabled,
            set_startup_enabled=lambda enabled: set_startup_enabled(enabled, ROOT_DIR),
        )
        self.web_ui = WebUIServer(
            dict_path=self.dictionary.path,
            config_path=self.config_path,
            audio_dir=LOG_DIR / "audio",
            get_history_fn=self.app_logger.get_history,
            get_status_info=lambda: (self.transcriber.last_backend_status, self._hotkey_label()),
            get_learning_candidates_fn=self.candidate_store.list_candidates,
            on_copy_text=set_clipboard_text,
            on_quick_add_dict=self._quick_add_dict,
            on_retranscribe_last=lambda: threading.Thread(target=self._retranscribe_last, daemon=True).start(),
            on_retry_entry=lambda ts, audio: threading.Thread(
                target=self._retry_entry, args=(ts, audio), daemon=True
            ).start(),
            on_approve_candidate=self._approve_learning_candidate,
            on_reject_candidate=self._reject_learning_candidate,
            on_restore_candidate=self._restore_learning_candidate,
            on_learn_from_manual_edit=self._learn_from_manual_edit,
            on_dict_saved=self._on_dict_saved,
            on_config_saved=self._on_settings_saved,
            get_dict_sync_status=self.dict_sync.status,
            on_dict_sync_now=self.dict_sync.sync_once,
            list_microphones=list_input_devices,
            get_hotkey=lambda: self.config.hotkey,
            on_change_hotkey=self._start_hotkey_capture,
            get_ai_hotkey=lambda: self.config.ai_hotkey,
            on_change_ai_hotkey=self._start_ai_hotkey_capture,
            on_clear_ai_hotkey=self._clear_ai_hotkey,
            is_startup_enabled=is_startup_enabled,
            set_startup_enabled=lambda enabled: set_startup_enabled(enabled, ROOT_DIR),
            browser_profile_dir=DATA_DIR / "browser_profile",
        )
        self.web_ui.get_stats_fn = self.app_logger.stats
        self.web_ui.get_server_name_fn = self._current_server_name
        self.web_ui.get_dismissed_candidates_fn = self.candidate_store.list_dismissed
        self.web_ui.get_update_fn = self._update_payload
        self.web_ui.get_outdated_servers_fn = self._outdated_servers
        self.web_ui.get_server_info_fn = self._server_info_for_ui
        self.web_ui.get_extra_hotkey_fn = lambda: str(getattr(self.config, "hotkey_extra", "") or "")
        self.web_ui.on_capture_extra_hotkey = self._start_extra_hotkey_capture
        self.web_ui.on_clear_extra_hotkey = self._clear_extra_hotkey
        self.web_ui.on_run_update = self._run_github_update
        self.web_ui.on_check_update = lambda: self._check_github_update(manual=True)
        # このパソコンを「声を文字にする役」にする（設定画面から。Windows でソースのまま動かしているときだけ）
        if os.name == "nt" and not FROZEN:
            from client.host_setup import HostSetup

            self.web_ui.host_setup = HostSetup(ROOT_DIR)
        # GitHub に出ている新しい版（なければ None）
        self.update_info: Optional[dict] = None
        self._update_notified = ""
        self.hook = GlobalInputHook(
            hotkey=self.config.hotkey,
            cancel_key=self.config.cancel_key,
            suppress_hotkey=self.config.suppress_hotkey,
            on_press=self._on_hotkey_press,
            on_release=self._on_hotkey_release,
            on_cancel=self._on_cancel,
            is_recording_fn=lambda: self.recorder.is_recording,
            secondary_hotkey=self.config.ai_hotkey,
            on_secondary_press=self._on_ai_hotkey_press,
            on_secondary_release=self._on_ai_hotkey_release,
            extra_hotkeys=self._extra_hotkeys(),
        )

        self._busy_lock = threading.Lock()
        self._processing = False
        self._press_ignored = False
        self._toggle_active = False          # toggle モードで録音中か（2回目の押下で確定する）
        self._ai_recording = False           # 今回の録音が AI 整形 / 選択書き換えモードか
        self._ai_selected_text = ""          # AI 書き換えモード開始時の選択テキスト
        self._paste_target = 0               # 録音開始時の入力先ウィンドウ
        self._last_audio: Optional[tuple[np.ndarray, int, float]] = None
        self._last_audio_path = LOG_DIR / "last_audio.wav"
        self._max_timer: Optional[threading.Timer] = None
        self._live: Optional[LiveTranscription] = None   # 録音中に区切りを裏で認識しているセッション
        self._rec_status: tuple[str, str] = ("", "#1e3a8a")  # 録音中の案内文と色（途中経過の表示に使う）
        self._last_paste: Optional[dict] = None           # 「今のなし」で取り消す対象
        self._tray: Optional[pystray.Icon] = None
        self._status_text = "初期化中..."
        self._stop_event = threading.Event()
        self.heartbeat: Optional[Heartbeat] = None

    def _beep(self, freq: int, duration_ms: int) -> None:
        if not self.config.beep_enabled:
            return

        def _play() -> None:
            try:
                winsound.Beep(freq, duration_ms)
            except Exception:
                pass

        threading.Thread(target=_play, daemon=True).start()

    def _set_tray_state(self, color: str, status: str) -> None:
        self._status_text = status
        if self._tray is not None:
            self._tray.icon = _make_icon_image(color)
            self._tray.title = f"OnseiMan | {status} | キー: {self.config.hotkey}"

    def _on_settings_saved(self) -> None:
        self._reload_config_if_changed()
        self._restore_idle_icon()
        self.ui.refresh_history_if_open()
        self.dict_sync.kick()

    def _dict_store(self):
        """辞書の預け先（合わせない設定なら None）。"""
        cfg = self.config
        name = cfg.dict_share_name if NAME_RE.match(cfg.dict_share_name or "") else DEFAULT_NAME
        if cfg.dict_share == "server":
            url = self.transcriber._chosen() or next(iter(self.transcriber._server_list()), "")
            if not url:
                return None
            info = self._server_info_for_ui().get(url) or {}
            display = (getattr(cfg, "server_names", {}) or {}).get(url) or info.get("name") or ""
            return ServerStore(url, name, display=display)
        if cfg.dict_share == "folder" and cfg.dict_share_folder:
            return FolderStore(cfg.dict_share_folder, name)
        return None

    def _on_dict_saved(self) -> None:
        self.dictionary.reload(force=True)
        self.dict_sync.kick()

    def _on_dict_synced(self) -> None:
        """ほかのパソコンで登録した言葉が入ってきた。"""
        self.dictionary.reload(force=True)

    def _reload_config_if_changed(self) -> None:
        with self._config_lock:
            self._reload_config_locked()

    def _reload_config_locked(self) -> None:
        if not self.config_path.exists():
            return
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        new_cfg = ClientConfig.load(self.config_path)
        self.config = new_cfg
        self.transcriber.config = new_cfg
        self.recorder.device = new_cfg.microphone
        self.recorder.max_duration_sec = new_cfg.max_duration_sec
        self.recorder.auto_stop_on_silence = self._is_toggle_mode() and new_cfg.auto_stop_on_silence
        self.recorder.silence_stop_sec = new_cfg.silence_stop_sec
        self.recorder.silence_rms_threshold = new_cfg.silence_rms_threshold
        self.recorder.preroll_sec = new_cfg.preroll_sec
        self.recorder.segment_kwargs = self._segment_kwargs()
        self.recorder.set_keep_open(new_cfg.keep_mic_open)
        if new_cfg.keep_mic_open:
            # マイクの変更もここで反映される（違うマイクなら開き直す）
            threading.Thread(target=self.recorder.warm_up, daemon=True).start()
        self.ui.show_overlay_enabled = new_cfg.show_overlay
        self.ui.set_overlay_pinned(new_cfg.overlay_position)
        self.ui.set_overlay_place(new_cfg.overlay_place)
        try:
            self.hook.update_bindings(
                new_cfg.hotkey,
                new_cfg.cancel_key,
                new_cfg.suppress_hotkey,
                secondary_hotkey=new_cfg.ai_hotkey,
                extra_hotkeys=self._extra_hotkeys(new_cfg),
            )
        except ValueError as exc:
            logger.error("設定のホットキーが無効です: %s", exc)
        logger.info("config.json を再読み込みしました")

    def _is_toggle_mode(self) -> bool:
        return str(self.config.hotkey_mode).lower() != "hold"

    def _other_audio(self, op: str) -> None:
        """録音中にほかのアプリの音を消す／戻す。"""
        muter = getattr(self, "audio_muter", None)
        if muter is None:
            return
        if op == "mute":
            if self.config.mute_other_audio:
                muter.mute_others()
        else:
            muter.restore()

    def _recognize_safely(self, samples, sample_rate: int, hotwords: str):
        """認識して、話した長さの割に文字が少なすぎるときは、ヒント（辞書の言葉）なしでもう一度認識する。

        モデルは一度に扱える量に上限があり、ヒントが多いと文の後半が消えることがある。
        区切りの長さで防いでいるが、念のため結果を確かめて、話したことを捨てないようにする。
        """
        res = self.transcriber.transcribe(samples=samples, sample_rate=sample_rate, hotwords_csv=hotwords)
        dur = len(samples) / float(max(sample_rate, 1))
        text = getattr(res, "text", "") or ""
        if hotwords and dur >= 5.0 and len(text) < dur * 1.0 and self._had_voice(samples, dur):
            again = self.transcriber.transcribe(samples=samples, sample_rate=sample_rate, hotwords_csv="")
            if len(getattr(again, "text", "") or "") > len(text) * 1.5:
                logger.warning(
                    "認識結果が短すぎたため、ヒントなしで認識し直しました (%.1fs: %d文字 → %d文字)",
                    dur, len(text), len(again.text),
                )
                again.elapsed_ms = float(getattr(again, "elapsed_ms", 0.0)) + float(getattr(res, "elapsed_ms", 0.0))
                return again
        return res

    def _had_voice(self, samples, duration: float) -> bool:
        """声らしい音が入っていたか。入っていない短い録音（押し間違い・試し押し）は履歴に残さない。"""
        try:
            if duration < 1.0:
                return False
            frame = 320  # 20ms
            n = len(samples) // frame
            if n == 0:
                return False
            x = np.asarray(samples[: n * frame], dtype=np.float32).reshape(n, frame)
            loud = np.sqrt((x * x).mean(axis=1)) >= float(self.config.silence_rms_threshold)
            return int(loud.sum()) >= 10  # 0.2 秒以上は声がある
        except Exception:
            return True

    def _use_pill(self) -> bool:
        return str(getattr(self.config, "recording_indicator", "pill")) == "pill"

    def _finish_from_pill(self) -> None:
        """バーの ✓ を押した（入力ワーカー上で実行）。トグル録音中だけ確定する。"""
        if self._toggle_active and self.recorder.is_recording:
            self._finish_recording()

    def _on_overlay_moved(self, pos: Optional[tuple]) -> None:
        """表示をドラッグした（pos）／ダブルクリックで戻した（None）とき、場所を設定に保存する。"""
        with self._config_lock:
            self._reload_config_locked()
            self.config.overlay_position = [int(pos[0]), int(pos[1])] if pos else None
            try:
                self.config.save(self.config_path)
                self._config_mtime = self.config_path.stat().st_mtime
            except Exception as exc:
                logger.warning("表示の位置を保存できませんでした: %s", exc)
        logger.info("録音中の表示の位置: %s", f"{pos[0]},{pos[1]} に固定" if pos else "カーソルの近く")

    def _max_segment_sec(self) -> float:
        """一度に認識する長さの上限。優先語が多いほど短くする（多いと長い発話の後半が消えるため）。

        実測（Fun-ASR Nano）: 18 秒は優先語 20 語まで、15 秒は 28 語まで、12 秒は 36 語まで欠けない。
        → 「秒数 + 優先語の数 × 0.4 ≤ 25」を目安にする。
        """
        # GPU で聞き取る版（Haas-ja）は、優先語が多くても長い話を聞き取れる。認識役が教えてくれる長さを使う
        info = {}
        try:
            if self.config.recognition_place != "local":
                info = self.transcriber.preferred_server_info()
        except Exception:
            info = {}
        if info.get("engine") == "haas" and info.get("max_segment_sec"):
            return max(6.0, float(info["max_segment_sec"]))
        try:
            if self.config.recognition_place == "local" and self.transcriber.local_uses_haas():
                from shared.speechcpp_engine import MAX_SEGMENT_SEC

                return MAX_SEGMENT_SEC
        except Exception:
            pass
        try:
            n = len(self.dictionary.active_hotwords())
        except Exception:
            n = 0
        return max(6.0, min(float(self.config.segment_max_sec), 25.0 - 0.4 * n))

    def _segment_kwargs(self) -> Optional[dict]:
        """話の切れ目で区切る設定（区切らない設定なら None）。"""
        if not self.config.live_segmentation:
            return None
        max_sec = self._max_segment_sec()
        return {
            "rms_threshold": self.config.silence_rms_threshold,
            "pause_sec": self.config.segment_pause_sec,
            # 区切るのは、一度に聞き取れる長さの近くまで来たときだけ。短い区切りは前後のつながりが切れて
            # 聞き間違いが増え、区切り目に「。」も入るので、上限の 85% までは区切らずに通しで聞き取る
            "min_segment_sec": max_sec * 0.85,
            "max_segment_sec": max_sec,
        }

    def _on_segment(self, samples: np.ndarray) -> None:
        """録音スレッドから呼ばれる: 切れた区切りを裏の認識へ回す。"""
        live = self._live
        if live is not None:
            live.submit(samples)

    def _on_live_progress(self, text_so_far: str) -> None:
        """区切りの認識が終わるたびに、録音中の表示へ「ここまでの文」を出す。"""
        if not text_so_far or not self.recorder.is_recording:
            return
        if self._use_pill():
            self.ui.pill_live_text(text_so_far.replace("\n", " "))
            return
        status, bg = self._rec_status
        tail = text_so_far.replace("\n", " ")
        tail = tail if len(tail) <= 30 else "…" + tail[-30:]
        self.ui.show_status(f"{status}\n{tail}", bg=bg)

    def _on_no_signal(self) -> None:
        """録音開始から 1.5 秒たってもマイクの音がまったく来ないとき（録音は続ける）。"""
        logger.warning("録音中ですがマイクの音が入っていません（ミュート・入力デバイスを確認してください）")
        if self._use_pill():
            self.ui.pill_warn()
            return
        self.ui.show_status(
            "⚠ マイクの音が入っていません（ミュート・入力デバイスを確認） / Esc でキャンセル", bg="#991b1b"
        )

    def _start_live_session(self) -> None:
        if self._live is not None:
            self._live.cancel()
            self._live = None
        if not self.config.live_segmentation:
            return
        hotwords = self.dictionary.hotwords_csv
        sample_rate = self.config.sample_rate
        self._live = LiveTranscription(
            lambda s: self._recognize_safely(s, sample_rate, hotwords),
            on_progress=self._on_live_progress,
        )

    def _drop_live_session(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.cancel()

    def _hotkey_label(self) -> str:
        if self._is_toggle_mode():
            return f"{self.config.hotkey}（1回押して開始 / もう1回で確定）"
        return f"{self.config.hotkey}（押している間だけ録音）"

    def _cancel_max_timer(self) -> None:
        if self._max_timer is not None:
            self._max_timer.cancel()
            self._max_timer = None

    def _on_max_duration(self) -> None:
        """toggle モードで押し忘れても、上限時間で自動的に確定する（入力ワーカー上で実行）。"""
        self._max_timer = None
        if self._toggle_active:
            logger.info("録音が上限 %.0f 秒に達したため自動で確定します", self.config.max_duration_sec)
            self._finish_recording()

    def _on_silence_timeout(self) -> None:
        """録音スレッドから呼ばれる無音タイムアウト通知。入力ワーカーへ安全に転送する。"""
        self.hook.run_on_worker(self._finish_on_silence)

    def _finish_on_silence(self) -> None:
        if self._is_toggle_mode() and self._toggle_active and self.recorder.is_recording:
            logger.info("無音が %.1f 秒続いたため自動で確定します", self.config.silence_stop_sec)
            self._finish_recording()

    def _begin_recording(self, *, ai_mode: bool) -> None:
        if self._is_toggle_mode() and self._toggle_active:
            # 2回目の押下: 録音を止めて確定・入力
            self._finish_recording()
            return
        self._reload_config_if_changed()
        if self._processing:
            self._press_ignored = True
            self._beep(300, 60)
            self.ui.show_status("◌ 前の音声を認識中です。終わってから押してください", bg="#4c1d95")
            logger.info("認識中のためホットキーを無視しました")
            return
        self._press_ignored = False
        if hasattr(self, "edit_watcher") and self.edit_watcher is not None:
            self.edit_watcher.cancel()
        paste_target = capture_paste_target()
        selected_text = ""
        if ai_mode:
            selected_text = capture_selected_text(paste_target)

        self.recorder.auto_stop_on_silence = self._is_toggle_mode() and self.config.auto_stop_on_silence
        self.recorder.silence_stop_sec = self.config.silence_stop_sec
        self.recorder.silence_rms_threshold = self.config.silence_rms_threshold
        self.recorder.segment_kwargs = self._segment_kwargs()
        self._start_live_session()

        if self.recorder.start():
            self._paste_target = paste_target
            self._ai_recording = ai_mode
            self._ai_selected_text = selected_text
            self._beep(1040 if ai_mode else 880, 55)
            self._set_tray_state("#0284c7" if ai_mode else "#2563eb", "AI録音中" if ai_mode else "録音中")
            cancel = self.config.cancel_key.upper()
            active_key = self.config.ai_hotkey if ai_mode else self.config.hotkey
            if not ai_mode and self._extra_hotkeys():
                active_key = f"{self.config.hotkey}（か {self._extra_hotkeys()[0]}）"
            if self._is_toggle_mode():
                self._toggle_active = True
                self._cancel_max_timer()
                timer = threading.Timer(
                    self.config.max_duration_sec, lambda: self.hook.run_on_worker(self._on_max_duration)
                )
                timer.daemon = True
                timer.start()
                self._max_timer = timer
                auto_hint = f" / 無音{self.config.silence_stop_sec:g}秒で自動確定" if self.config.auto_stop_on_silence else ""
                guide = f"もう一度 {active_key} で確定{auto_hint} / {cancel} でキャンセル"
            else:
                guide = f"離すと確定 / {cancel} でキャンセル"
            if ai_mode and selected_text:
                prefix = "✦ AI書き換え録音中（選択テキストあり）"
                bg = "#0f766e"
            elif ai_mode:
                prefix = "✦ AI整形録音中"
                bg = "#0369a1"
            else:
                prefix = "● 録音中"
                bg = "#1e3a8a"
            self._rec_status = (f"{prefix}... ({guide})", bg)
            # 表示を入力中のカーソルの近くに出す（録音を始めてから調べるので、録音開始は遅れない）
            try:
                rect, how = get_caret_rect()
            except Exception:
                rect, how = None, ""
            self.ui.set_overlay_anchor(rect, how)
            self._other_audio("mute")
            if self._use_pill():
                self.ui.show_pill()
            else:
                self.ui.show_status(f"{prefix}... ({guide})", bg=bg)
            logger.info(
                "録音開始 (hotkey=%s, mode=%s, ai=%s, selected=%d文字)",
                active_key,
                self.config.hotkey_mode,
                ai_mode,
                len(selected_text),
            )
        else:
            self._drop_live_session()
            self._beep(300, 150)
            self._set_tray_state("#dc2626", "マイクエラー")
            self.ui.show_status("✕ マイク録音に失敗しました", bg="#991b1b", auto_hide_ms=2000)

    def _on_hotkey_press(self) -> None:
        self._begin_recording(ai_mode=False)

    def _on_ai_hotkey_press(self) -> None:
        self._begin_recording(ai_mode=True)

    def _on_cancel(self) -> None:
        self.ui.hide_pill()
        self._other_audio("restore")
        self._toggle_active = False
        self._ai_recording = False
        self._ai_selected_text = ""
        self._cancel_max_timer()
        cancelled = self.recorder.cancel()
        self._drop_live_session()
        if cancelled:
            self._beep(400, 90)
            logger.info("録音をキャンセルしました")
            self.ui.show_status("✕ キャンセルしました", bg="#4b5563", auto_hide_ms=900)
            self._restore_idle_icon()

    def _on_hotkey_release(self) -> None:
        if self._press_ignored:
            self._press_ignored = False
            return
        if self._is_toggle_mode():
            return
        self._finish_recording()

    def _on_ai_hotkey_release(self) -> None:
        self._on_hotkey_release()

    def _save_last_audio(self, samples: np.ndarray, sample_rate: int, duration: float) -> None:
        self._last_audio = (samples.copy(), sample_rate, duration)
        try:
            self._last_audio_path.parent.mkdir(parents=True, exist_ok=True)
            self._last_audio_path.write_bytes(samples_to_wav_bytes(samples, sample_rate))
        except Exception as exc:
            logger.debug("直前音声の保存をスキップしました: %s", exc)

    AUDIO_KEEP = 100

    def _save_recording_audio(self, samples: np.ndarray, sample_rate: int) -> str:
        """録音を logs/audio に保存してファイル名を返す（履歴の「再試行」で使う）。新しい 100 個だけ残す。"""
        try:
            from datetime import datetime

            audio_dir = LOG_DIR / "audio"
            audio_dir.mkdir(parents=True, exist_ok=True)
            stem = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            # Windows の時計は細かく刻まれないことがあり、続けて保存すると同じ名前になる → 番号を足す
            name, n = stem + ".wav", 1
            while (audio_dir / name).exists():
                name, n = f"{stem}-{n}.wav", n + 1
            (audio_dir / name).write_bytes(samples_to_wav_bytes(samples, sample_rate))
            files = sorted(audio_dir.glob("*.wav"))
            for old in files[: max(0, len(files) - self.AUDIO_KEEP)]:
                old.unlink(missing_ok=True)
            return name
        except Exception as exc:
            logger.debug("録音の保存をスキップしました: %s", exc)
            return ""

    def _retry_entry(self, timestamp: str, audio: str) -> None:
        """履歴の「再試行」: その録音をもう一度認識し、できたら履歴を書き換えてコピーする。"""
        if self._processing or self.recorder.is_recording:
            self.ui.show_status("◌ 録音または認識中です", bg="#4c1d95", auto_hide_ms=1200)
            return
        # 名前は録音フォルダの中の wav だけ（フォルダの外を指す名前は受け付けない）
        if not audio or Path(audio).name != audio or not audio.lower().endswith(".wav"):
            audio = ""
        entry = self.app_logger.find(timestamp, audio) if audio else None
        path = LOG_DIR / "audio" / audio if audio else None
        if entry is None or path is None or not path.exists():
            self.ui.show_status("この履歴の音声は残っていません", bg="#4b5563", auto_hide_ms=1800)
            return
        try:
            samples, sr = decode_wav_bytes(path.read_bytes())
        except Exception as exc:
            self.ui.show_status(f"音声を読めませんでした: {exc}", bg="#991b1b", auto_hide_ms=2000)
            return
        self.dictionary.reload(force=True)
        self._process_audio(samples, len(samples) / float(max(sr, 1)), paste_target=0, ai_mode=False,
                            selected_text="", do_paste=False, retry_of=entry)

    def _load_last_audio(self) -> Optional[tuple[np.ndarray, int, float]]:
        if self._last_audio is not None:
            return self._last_audio
        if self._last_audio_path.exists():
            try:
                samples, sr = decode_wav_bytes(self._last_audio_path.read_bytes())
                return samples, sr, len(samples) / float(max(sr, 1))
            except Exception as exc:
                logger.warning("保存済み音声の読み込みに失敗: %s", exc)
        return None

    def _format_recognized_text(self, raw_text: str, target_hwnd: int = 0) -> str:
        text = self.dictionary.remove_fillers(raw_text) if self.config.remove_fillers else raw_text
        text = self.dictionary.apply_replacements(text)
        text = self.dictionary.normalize(text) if self.config.normalize_text else text
        text = apply_voice_commands(text) if self.config.voice_commands else text
        if self.config.strip_trailing_period:
            text = strip_chat_trailing_period(text)
        elif self.config.app_specific_rules and target_hwnd:
            proc = get_window_process_name(target_hwnd)
            text = apply_app_rules(text, proc, enabled=True, chat_apps=self.config.chat_apps)
        return text

    def _expand_snippet(self, text: str) -> Optional[str]:
        """発話全体が定型文の呼び出し語なら、その定型文を返す（違えば None）。"""
        try:
            body, hit = self.dictionary.apply_snippets(text)
        except Exception:
            return None
        return body if hit is True and isinstance(body, str) and body else None

    def _finish_recording(self) -> None:
        self.ui.hide_pill()
        self._other_audio("restore")
        self._toggle_active = False
        ai_mode = self._ai_recording
        selected_text = self._ai_selected_text
        self._ai_recording = False
        self._ai_selected_text = ""
        self._cancel_max_timer()
        # stop() は最後の区切りを live へ渡してから戻る
        samples, duration, was_cancelled = self.recorder.stop()
        live, self._live = self._live, None
        if was_cancelled:
            if live is not None:
                live.cancel()
            return
        if samples is None or duration < self.config.min_duration_sec:
            if live is not None:
                live.cancel()
            logger.info("短すぎる録音のため破棄しました (%.2fs)", duration)
            self.ui.hide_overlay()
            self._restore_idle_icon()
            return

        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) > 0 else 0.0
        if rms < 1e-5:
            if live is not None:
                live.cancel()
            last = list(self.app_logger.get_history()[:1])
            prev = getattr(last[0], "failed", "") if last else ""
            if not (isinstance(prev, str) and prev.startswith("音声が無音です")):  # 続けて同じ失敗は残さない
                self.app_logger.record_failure(
                    reason="音声が無音です（マイクの音が入っていません）",
                    duration_sec=duration,
                    audio=self._save_recording_audio(samples, self.config.sample_rate),
                )
            self.ui.refresh_history_if_open()
            logger.warning("録音が無音でした (%.2fs, rms=%.6f)。マイクのミュートや入力デバイスを確認してください", duration, rms)
            self._beep(300, 150)
            self.ui.show_status(
                "✕ マイクの音が入っていません（ミュート・入力デバイスを確認）", bg="#991b1b", auto_hide_ms=3000
            )
            self._restore_idle_icon()
            return

        self._save_last_audio(samples, self.config.sample_rate, duration)
        audio_name = self._save_recording_audio(samples, self.config.sample_rate)
        self._beep(660, 50)
        self._processing = True
        threading.Thread(
            target=self._process_audio,
            args=(samples, duration, self._paste_target, ai_mode, selected_text, True, live, audio_name),
            daemon=True,
        ).start()

    def _transcribe_whole(self, samples: np.ndarray, duration: float) -> tuple[str, str, float]:
        """区切りながらの認識を使わなかったときの認識。長い音声はここで区切ってから認識する
        （モデルは約 30 秒を超えると結果が空になるため）。戻り値は (文, 認識した場所, ミリ秒)。"""
        max_sec = self._max_segment_sec()
        kwargs = self._segment_kwargs() or {
            "rms_threshold": self.config.silence_rms_threshold,
            "pause_sec": 0.6,
            "min_segment_sec": max_sec * 0.85,
            "max_segment_sec": max_sec,
        }
        parts = [samples]
        if duration > kwargs["max_segment_sec"] + 2.0:
            parts = split_on_pauses(samples, self.config.sample_rate, **kwargs)
            logger.info("長い音声のため %d 個に区切って認識します (%.1fs)", len(parts), duration)
        hotwords = self.dictionary.hotwords_csv
        texts, backend, total_ms = [], "", 0.0
        for part in parts:
            res = self._recognize_safely(part, self.config.sample_rate, hotwords)
            texts.append(res.text)
            backend = res.backend
            total_ms += res.elapsed_ms
        if len(parts) > 1:
            backend = f"{backend}×{len(parts)}"
        return join_texts(texts), backend, total_ms

    def _undo_last_paste(self) -> None:
        """「今のなし」: 直前に入力した文を取り消す。安全に戻せないときは何もしない。"""
        last = self._last_paste
        if not last:
            self.ui.show_status("取り消せる直前の入力がありません", bg="#4b5563", auto_hide_ms=1800)
            return
        if time.monotonic() - last["time"] > 180:
            self.ui.show_status("時間がたっているため取り消しませんでした（Ctrl+Z を使ってください）", bg="#92400e", auto_hide_ms=2600)
            return
        if self.hook.typed_key_count != last["keys"]:
            self.ui.show_status("入力後にキーを打っているため取り消しませんでした（Ctrl+Z を使ってください）", bg="#92400e", auto_hide_ms=2600)
            return
        proc = get_window_process_name(last["hwnd"])
        ok, how = undo_paste(last["text"], last["hwnd"], proc)
        preview = last["text"].replace("\n", " ")
        preview = preview if len(preview) <= 24 else preview[:24] + "…"
        if ok:
            self._last_paste = None
            logger.info("直前の入力を取り消しました (%s, %s): %s", how, proc, last["text"])
            self.ui.show_status(f"↶ 取り消しました: {preview}", bg="#065f46", auto_hide_ms=1800)
        else:
            logger.info("取り消しできませんでした (%s)", how)
            msg = (
                "入力したウィンドウが前面にないため取り消しませんでした"
                if how == "other_window"
                else "取り消しに失敗しました（Ctrl+Z を使ってください）"
            )
            self.ui.show_status(msg, bg="#92400e", auto_hide_ms=2600)

    def _process_audio(
        self,
        samples,
        duration: float,
        paste_target: int = 0,
        ai_mode: bool = False,
        selected_text: str = "",
        do_paste: bool = True,
        live: Optional[LiveTranscription] = None,
        audio_name: str = "",
        retry_of=None,
    ) -> None:
        with self._busy_lock:
            self._processing = True
            self._set_tray_state("#7c3aed", "認識中...")
            self.ui.show_status(
                "◌ 文字にしています" if self._use_pill() else f"◌ 認識中...（{self.friendly_backend(self.transcriber.last_backend_status)}）",
                bg="#4c1d95",
            )
            try:
                rms = float((samples * samples).mean() ** 0.5) if len(samples) > 0 else 0.0
                logger.info("送信開始: %.2fs (rms=%.4f, ai=%s)", duration, rms, ai_mode)
                if live is not None and live.segment_count > 0:
                    lr = live.finish()
                    raw_text = lr.text
                    backend_label = lr.backend if lr.segments == 1 else f"{lr.backend}×{lr.segments}"
                    # 表示する時間は「話し終えてから全部そろうまで」の待ち時間
                    total_ms = lr.wait_ms
                    if lr.segments > 1:
                        logger.info(
                            "区切り %d 個で認識（認識時間の合計 %.0fms / 話し終えてからの待ち %.0fms）",
                            lr.segments, lr.asr_ms_total, lr.wait_ms,
                        )
                else:
                    if live is not None:
                        live.cancel()  # 声らしい区切りが無かった: 念のため全体を認識する
                    raw_text, backend_label, total_ms = self._transcribe_whole(samples, duration)
                asr_backend = backend_label
                final_text = self._format_recognized_text(raw_text, target_hwnd=paste_target)

                if final_text and do_paste and not ai_mode and self.config.undo_voice_command and is_undo_command(final_text):
                    self._undo_last_paste()
                    return

                if not final_text:
                    if raw_text:
                        logger.info("言いよどみだけだったため入力しませんでした: %s", raw_text)
                        self.ui.show_status(f"（言いよどみだけだったので入力しませんでした: {raw_text[:20]}）", bg="#374151", auto_hide_ms=1500)
                    else:
                        logger.info("認識結果が空でした (%.2fs, %s)", duration, backend_label)
                        self.ui.show_status("（音声が認識されませんでした）", bg="#374151", auto_hide_ms=1100)
                        if retry_of is None and self._had_voice(samples, duration):
                            self.app_logger.record_failure(
                                reason="音声が認識されませんでした", duration_sec=duration,
                                audio=audio_name, backend=backend_label,
                            )
                            self.ui.refresh_history_if_open()
                else:
                    snippet = None if ai_mode else self._expand_snippet(final_text)
                    if snippet is not None:
                        # 定型文: 登録した文をそのまま入れる
                        final_text = snippet
                        backend_label = f"{asr_backend}+定型文"
                    elif ai_mode:
                        self.ui.show_status(
                            "✦ AI で書き換え中..." if selected_text else "✦ AI で文章を整えています...",
                            bg="#0f766e",
                        )
                        try:
                            refiner = LLMRefiner(
                                base_url=self.config.llm_base_url,
                                model=self.config.llm_model,
                                api_key=self.config.llm_api_key,
                                timeout_sec=self.config.llm_timeout_sec,
                            )
                            llm_res = (
                                refiner.rewrite_selection(selected_text, final_text)
                                if selected_text
                                else refiner.refine_dictation(final_text)
                            )
                            if llm_res.text:
                                final_text = llm_res.text
                                backend_label = f"{asr_backend}+AI"
                                total_ms += llm_res.elapsed_ms
                        except Exception as exc:
                            logger.warning("AI 整形に失敗したため通常テキストを使用します: %s", exc)
                            self.ui.show_status(f"⚠ AI整形エラー（元の認識結果を使います）: {exc}", bg="#92400e", auto_hide_ms=3000)
                    elif self.config.auto_refine_self_correction and has_self_correction(final_text):
                        # 言い直し表現が含まれるときだけ整理（Mac mini 等の LLM が設定されていれば 0.3s 整理、なければ 0ms ルール）
                        refined_done = False
                        if self.config.llm_base_url:
                            self.ui.show_status("✦ 言い直しを整理しています...", bg="#0f766e")
                            try:
                                refiner = LLMRefiner(
                                    base_url=self.config.llm_base_url,
                                    model=self.config.llm_model,
                                    api_key=self.config.llm_api_key,
                                    timeout_sec=min(self.config.llm_timeout_sec, 6.0),
                                )
                                llm_res = refiner.resolve_self_correction(final_text)
                                if llm_res.text and not is_over_truncated(final_text, llm_res.text):
                                    final_text = llm_res.text
                                    backend_label = f"{asr_backend}+LLM整理"
                                    total_ms += llm_res.elapsed_ms
                                    refined_done = True
                                elif llm_res.text:
                                    logger.info("言い直し LLM 結果が削りすぎのためルールへ切替: %r -> %r", final_text, llm_res.text)
                            except Exception as exc:
                                logger.debug("言い直し LLM 整理をスキップし軽量ルールへフォールバック: %s", exc)
                        if not refined_done:
                            ruled = rule_based_self_correction(final_text)
                            if ruled and ruled != final_text and not is_over_truncated(final_text, ruled):
                                final_text = ruled
                                backend_label = f"{asr_backend}+言い直し整理"

                    if self.config.strip_trailing_period and snippet is None:
                        final_text = strip_chat_trailing_period(final_text)

                    if retry_of is not None:
                        self.app_logger.update_entry(
                            retry_of, backend=backend_label, elapsed_ms=round(total_ms, 1),
                            raw_text=raw_text, text=final_text, failed="",
                        )
                    else:
                        self.app_logger.record_transcription(
                            backend=backend_label,
                            elapsed_ms=total_ms,
                            duration_sec=duration,
                            raw_text=raw_text,
                            text=final_text,
                            audio=audio_name,
                        )
                    self.ui.refresh_history_if_open()
                    preview = final_text.replace("\n", " ")
                    preview = preview if len(preview) <= 28 else preview[:28] + "…"
                    if not do_paste:
                        set_clipboard_text(final_text)
                        self.ui.show_status(
                            f"✓ 再認識してコピーしました: {preview} ({total_ms:.0f}ms)",
                            bg="#065f46",
                            auto_hide_ms=2000,
                        )
                        logger.info("再認識完了 [%s / %.0fms]: %s", backend_label, total_ms, final_text)
                    else:
                        pasted = paste_text(
                            final_text,
                            restore_clipboard=self.config.restore_clipboard,
                            paste_delay_ms=self.config.paste_delay_ms,
                            restore_delay_ms=self.config.clipboard_restore_delay_ms,
                            target_hwnd=paste_target,
                        )
                        if pasted.ok:
                            if self._use_pill():
                                self.ui.show_done(final_text.replace("\n", " "), undo=True)
                            self._last_paste = {
                                "hwnd": pasted.hwnd or paste_target,
                                "text": final_text,
                                "time": time.monotonic(),
                                "keys": self.hook.typed_key_count,
                            }
                            if not self._use_pill():
                                self.ui.show_status(
                                    f"✓ {preview} ({total_ms:.0f}ms)",
                                    bg="#065f46",
                                    auto_hide_ms=1300,
                                )
                            logger.info(
                                "入力完了 [%s / %.0fms / %.2fs] 貼り付け先=%r: %s",
                                backend_label,
                                total_ms,
                                duration,
                                pasted.window_title,
                                final_text,
                            )
                            if self.config.learn_from_edits and hasattr(self, "edit_watcher") and self.edit_watcher is not None:
                                self.edit_watcher.watch_after_paste(
                                    final_text,
                                    paste_target,
                                    self._on_detected_corrections,
                                )
                        else:
                            reasons = {
                                "elevated_window": "管理者権限のウィンドウには自動入力できません",
                                "no_window": "入力先のウィンドウが見つかりません",
                                "own_window": "入力先が見つかりません（貼り付けたい場所をクリックしてください）",
                                "sendinput_failed": "キー送信に失敗しました",
                                "clipboard_failed": "クリップボードに書き込めませんでした",
                            }
                            why = reasons.get(pasted.reason, pasted.reason)
                            tail = "。Ctrl+V で貼り付けてください" if pasted.clipboard_kept else ""
                            self._beep(300, 120)
                            self.ui.show_status(f"⚠ {why}{tail}: {preview}", bg="#92400e", auto_hide_ms=4000)
                            logger.warning(
                                "貼り付け失敗 [%s] 貼り付け先=%r クリップボード保持=%s: %s",
                                pasted.reason,
                                pasted.window_title,
                                pasted.clipboard_kept,
                                final_text,
                            )
            except Exception as exc:
                logger.exception("音声認識エラー: %s", exc)
                if retry_of is None:
                    try:
                        self.app_logger.record_failure(
                            reason=f"認識エラー: {exc}", duration_sec=duration, audio=audio_name
                        )
                        self.ui.refresh_history_if_open()
                    except Exception:
                        pass
                self._beep(260, 180)
                self._set_tray_state("#dc2626", "認識エラー")
                self.ui.show_status(f"✕ エラー: {exc}", bg="#991b1b", auto_hide_ms=2600)
            finally:
                self._processing = False
                self._restore_idle_icon()

    def _retranscribe_last(self) -> None:
        if self._processing or self.recorder.is_recording:
            self.ui.show_status("◌ 録音または認識中です", bg="#4c1d95", auto_hide_ms=1200)
            return
        loaded = self._load_last_audio()
        if loaded is None:
            self.ui.show_status("再認識できる直前の音声がありません", bg="#4b5563", auto_hide_ms=1500)
            return
        samples, _sr, duration = loaded
        self.dictionary.reload(force=True)
        self._process_audio(samples, duration, paste_target=0, ai_mode=False, selected_text="", do_paste=False)

    def _quick_add_dict(self, src: str, dst: str, also_hotword: bool) -> None:
        self.dictionary.add_replacement(src, dst, also_hotword=also_hotword)
        self.ui.show_status(f"✓ 辞書に追加しました: {src} → {dst}", bg="#065f46", auto_hide_ms=1800)

    def _on_detected_corrections(self, pairs: list[tuple[str, str]], before: str, after: str) -> None:
        existing = [src for src, _dst in getattr(self.dictionary, "_replacements", [])]
        auto_added: list[tuple[str, str]] = []
        queued: list[tuple[str, str]] = []
        threshold = int(self.config.auto_learn_after_count)

        from shared.correction_learner import is_whole_rewrite

        for src, dst in pairs:
            if is_whole_rewrite(src, dst, before, after):
                # 音声で入れたあとに、別の文を打ち直しただけ（聞き間違いの直しではない）
                logger.info("文の書き直しのため学習候補にしませんでした（%d文字）", len(src))
                continue
            cand = self.candidate_store.record(
                src,
                dst,
                example_before=before,
                example_after=after,
                existing_replacements=existing,
            )
            if cand is None:
                continue
            # 3 文字以下の短い言い方は、ふだんの言葉まで変わりやすいので自動では登録せず、必ず聞く
            # ×で消したことのある直しは、自動では登録せず聞く
            if (threshold > 0 and cand.count >= threshold and len(src) > 3
                    and not self.candidate_store.is_dismissed(src, dst)):
                self.dictionary.add_replacement(src, dst, also_hotword=True)
                self.candidate_store.remove(src, dst, ignore_future=False)
                auto_added.append((src, dst))
                logger.info("同じ修正を %d 回検知したため自動で辞書登録しました: %s → %s", cand.count, src, dst)
            else:
                queued.append((src, dst))

        self.ui.refresh_history_if_open()
        if auto_added:
            s, d = auto_added[0]
            self.ui.show_status(f"✓ 自動学習しました: {s} → {d}", bg="#065f46", auto_hide_ms=2600)
        elif queued:
            s, d = queued[0]
            self.ui.show_learning_toast(s, d)

    def _approve_learning_candidate(self, src: str, dst: str) -> None:
        self.dictionary.add_replacement(src, dst, also_hotword=True)
        self.candidate_store.remove(src, dst, ignore_future=False)
        self.ui.show_status(f"✓ 辞書に登録しました: {src} → {dst}", bg="#065f46", auto_hide_ms=1800)

    def _reject_learning_candidate(self, src: str, dst: str) -> None:
        self.candidate_store.dismiss(src, dst)

    def _restore_learning_candidate(self, body: dict) -> None:
        """×の取り消し（画面の「元に戻す」）。"""
        from shared.correction_learner import LearningCandidate

        try:
            count = max(1, min(int(body.get("count", 1)), 1000))
        except (TypeError, ValueError):
            count = 1
        self.candidate_store.restore(LearningCandidate(
            src=str(body.get("src", "")).strip()[:200],
            dst=str(body.get("dst", "")).strip()[:200],
            count=count,
            example_before=str(body.get("example_before", ""))[:2000],
            example_after=str(body.get("example_after", ""))[:2000],
            updated_at=str(body.get("updated_at", ""))[:32],
        ))

    def _learn_from_manual_edit(self, before: str, after: str) -> int:
        pairs = extract_corrections(before, after)
        if not pairs:
            return 0
        existing = [src for src, _dst in getattr(self.dictionary, "_replacements", [])]
        added = 0
        for src, dst in pairs:
            cand = self.candidate_store.record(
                src,
                dst,
                example_before=before,
                example_after=after,
                existing_replacements=existing,
            )
            if cand is not None:
                added += 1
        return added

    def _current_server_name(self) -> str:
        """いま使っているサーバーのパソコンの名前（分からなければ空）。"""
        if not str(self.transcriber.last_backend_status).startswith("LAN"):
            return ""
        url = getattr(self.transcriber, "_preferred", "") or ""
        names = getattr(self.config, "server_names", {}) or {}
        return names.get(url) or names.get(url.rstrip("/")) or ""

    @staticmethod
    def friendly_backend(status: str) -> str:
        """「LAN (cpu)」などを、使う人に分かる言葉にする。"""
        s = status or ""
        if s.startswith("LAN"):
            return "サーバーで認識"
        if s.startswith("Local"):
            return "このパソコンで認識"
        if s == "未接続":
            return "認識できる場所がありません"
        return s or "準備中"

    def _restore_idle_icon(self) -> None:
        status = self.transcriber.last_backend_status
        label = self.friendly_backend(status)
        if status.startswith("LAN"):
            self._set_tray_state("#16a34a", f"待機中（{label}）")
        elif status.startswith("Local"):
            self._set_tray_state("#ca8a04", f"待機中（{label}）")
        else:
            self._set_tray_state("#6b7280", f"待機中（{label}）")

    def _health_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self.recorder.is_recording and not self._processing:
                self.transcriber.check_server_health()
                self._remember_server_names()
                self._restore_idle_icon()
            self._stop_event.wait(15.0)

    def _remember_server_names(self) -> None:
        """認識役が返した名前を、まだ名前のないサーバーに覚えておく（止まっているときも名前で出せるように）。
        自分で付けた名前は変えない。"""
        try:
            with self._config_lock:
                self._reload_config_locked()
                names = dict(getattr(self.config, "server_names", {}) or {})
                added = False
                for url in list(self.config.server_urls or []):
                    info = self.transcriber.server_info.get(url.rstrip("/")) or self.transcriber.server_info.get(url) or {}
                    if url not in names and info.get("name"):
                        names[url] = str(info["name"])[:64]
                        added = True
                if added:
                    self.config.server_names = names
                    self.config.save(self.config_path)
                    self._config_mtime = self.config_path.stat().st_mtime
        except Exception:
            logger.exception("サーバーの名前を覚えられませんでした")

    def _start_hotkey_capture(self) -> None:
        self.ui.show_status(
            "割り当てたいキーまたはマウスサイドボタンを押してください (Escで中止)",
            bg="#0369a1",
        )

        def on_captured(new_key: str) -> None:
            if not new_key:
                self.ui.show_status("ホットキー変更を中止しました", bg="#4b5563", auto_hide_ms=1200)
                return
            try:
                self.hook.update_bindings(new_key, self.config.cancel_key, self.config.suppress_hotkey)
                self.config.hotkey = new_key
                self.config.save(self.config_path)
                self._config_mtime = self.config_path.stat().st_mtime
                self._restore_idle_icon()
                self.ui.refresh_history_if_open()
                self.ui.show_status(f"✓ ホットキーを「{new_key}」に変更しました", bg="#065f46", auto_hide_ms=1800)
            except Exception as exc:
                self.ui.show_status(f"✕ 変更失敗: {exc}", bg="#991b1b", auto_hide_ms=2000)

        self.hook.start_capture_next_input(on_captured)

    def _start_ai_hotkey_capture(self) -> None:
        self.ui.show_status(
            "AI 整形・書き換えに割り当てたいキーまたはマウスボタンを押してください (Escで中止)",
            bg="#0f766e",
        )

        def on_captured(new_key: str) -> None:
            if not new_key:
                self.ui.show_status("AI 整形キー変更を中止しました", bg="#4b5563", auto_hide_ms=1200)
                return
            if new_key == self.config.hotkey:
                self.ui.show_status("✕ 通常の録音キーとは別のキーを選んでください", bg="#991b1b", auto_hide_ms=2000)
                return
            try:
                self.hook.update_bindings(
                    self.config.hotkey,
                    self.config.cancel_key,
                    self.config.suppress_hotkey,
                    secondary_hotkey=new_key,
                )
                self.config.ai_hotkey = new_key
                self.config.save(self.config_path)
                self._config_mtime = self.config_path.stat().st_mtime
                self.ui.show_status(f"✓ AI 整形キーを「{new_key}」に変更しました", bg="#065f46", auto_hide_ms=1800)
            except Exception as exc:
                self.ui.show_status(f"✕ 変更失敗: {exc}", bg="#991b1b", auto_hide_ms=2000)

        self.hook.start_capture_next_input(on_captured)

    def _extra_hotkeys(self, cfg=None) -> list:
        """もう 1 つの録音キー（無効な書き方なら使わない）。"""
        from client.input_hook import parse_binding

        name = str(getattr(cfg or self.config, "hotkey_extra", "") or "").strip()
        if not name:
            return []
        try:
            parse_binding(name)
        except ValueError:
            logger.error("もう 1 つの録音キーの書き方が正しくありません: %s", name)
            return []
        return [name]

    def _start_extra_hotkey_capture(self) -> None:
        self.ui.show_status(
            "もう 1 つの録音キーにしたいキーまたはマウスのボタンを押してください (Escで中止)",
            bg="#0369a1",
        )

        def on_captured(new_key: str) -> None:
            if not new_key:
                self.ui.show_status("もう 1 つの録音キーの設定を中止しました", bg="#4b5563", auto_hide_ms=1200)
                return
            used = {self.config.hotkey: "録音キー", self.config.ai_hotkey: "AI で整えるキー", self.config.cancel_key: "やめるキー"}
            if new_key in used:
                self.ui.show_status(f"✕ {used[new_key]}とは別のキーを選んでください", bg="#991b1b", auto_hide_ms=2000)
                return
            try:
                self.config.hotkey_extra = new_key
                self.hook.update_bindings(
                    self.config.hotkey,
                    self.config.cancel_key,
                    self.config.suppress_hotkey,
                    extra_hotkeys=[new_key],
                )
                self.config.save(self.config_path)
                self._config_mtime = self.config_path.stat().st_mtime
                self._restore_idle_icon()
                self.ui.show_status(f"✓ もう 1 つの録音キーを「{new_key}」にしました", bg="#065f46", auto_hide_ms=1800)
            except Exception as exc:
                self.ui.show_status(f"✕ 設定できませんでした: {exc}", bg="#991b1b", auto_hide_ms=2000)

        self.hook.start_capture_next_input(on_captured)

    def _clear_extra_hotkey(self) -> None:
        self.hook.update_bindings(
            self.config.hotkey,
            self.config.cancel_key,
            self.config.suppress_hotkey,
            extra_hotkeys=[],
        )
        self.config.hotkey_extra = ""
        self.config.save(self.config_path)
        self._config_mtime = self.config_path.stat().st_mtime
        self.ui.show_status("もう 1 つの録音キーを外しました", bg="#4b5563", auto_hide_ms=1400)

    def _clear_ai_hotkey(self) -> None:
        self.hook.update_bindings(
            self.config.hotkey,
            self.config.cancel_key,
            self.config.suppress_hotkey,
            secondary_hotkey="",
        )
        self.config.ai_hotkey = ""
        self.config.save(self.config_path)
        self._config_mtime = self.config_path.stat().st_mtime
        self.ui.show_status("AI 整形キーを解除しました", bg="#4b5563", auto_hide_ms=1400)

    def _build_tray_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _item: f"OnseiMan {__version__} | {self._status_text} | キー: {self.config.hotkey}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "OnseiMan の画面を開く",
                lambda _icon, _item: self.web_ui.open_window("history"),
                default=True,
            ),
            pystray.MenuItem(
                "直前に入れた文をコピー",
                lambda _icon, _item: self._copy_last_result(),
            ),
            pystray.MenuItem(
                "直前の録音をもう一度文字にする",
                lambda _icon, _item: threading.Thread(target=self._retranscribe_last, daemon=True).start(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "録音のキーを変える（次に押したキーにする）",
                lambda _icon, _item: self._start_hotkey_capture(),
            ),
            pystray.MenuItem(
                "AI で整えるキーを変える（次に押したキーにする）",
                lambda _icon, _item: self._start_ai_hotkey_capture(),
            ),
            pystray.MenuItem(
                "辞書",
                lambda _icon, _item: self.web_ui.open_window("dict"),
            ),
            pystray.MenuItem(
                "設定",
                lambda _icon, _item: self.web_ui.open_window("settings"),
            ),
            pystray.MenuItem(
                "使い方",
                lambda _icon, _item: self.web_ui.open_window("help"),
            ),
            pystray.MenuItem(
                "動きの記録を開く（困ったときに作った人へ）",
                lambda _icon, _item: _open_path_in_shell(self.app_logger.log_file),
            ),
            pystray.MenuItem(
                "入れた文の一覧を開く（テキスト）",
                lambda _icon, _item: _open_path_in_shell(self.app_logger.history_txt),
            ),
            pystray.MenuItem(
                lambda _item: (f"新しい版にする（{self.update_info['version']}）" if self.update_info else "更新を確認"),
                lambda _icon, _item: self._on_tray_update(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("終了", lambda _icon, _item: self.quit()),
        )

    # ---- GitHub の新しい版（フォルダを置いて使う形） ----
    UPDATE_CHECK_SEC = 24 * 3600

    def _prepare_haas(self) -> None:
        """このパソコンで聞き取るときの Haas-ja（speech.cpp）の部品を、裏で用意する（はじめの 1 回。約 0.7GB）。

        揃うまでは、これまでの Fun-ASR で聞き取る。揃ったら、次の聞き取りから Haas-ja に替わる。
        """
        from shared import speechcpp_setup

        if (getattr(self.config, "local_engine", "haas") or "haas") != "haas" or speechcpp_setup.platform_key() is None:
            return
        if not speechcpp_setup.ready(ROOT_DIR):
            if self._stop_event.wait(20):   # 起動の直後は、ほかの確かめと重ならないように少し待つ
                return
            logger.info("Haas-ja（speech.cpp）の部品を用意します")
            try:
                speechcpp_setup.ensure(ROOT_DIR, cancel=self._stop_event)
            except Exception as exc:
                logger.warning("Haas-ja の部品を用意できませんでした（Fun-ASR で聞き取ります）: %s", exc)
                return
            logger.info("Haas-ja の部品を用意しました")
            self.transcriber.reset_local()
            self.recorder.segment_kwargs = self._segment_kwargs()
            if self.config.recognition_place == "local":
                self.ui.show_status("✓ 聞き間違いの少ない Haas-ja で聞き取るようになりました", bg="#065f46", auto_hide_ms=4000)
        if self.config.recognition_place == "local" or self.config.local_fallback:
            self.transcriber.preload_local()

    GPU_HOST_RETRY_SEC = 24 * 3600

    def _auto_gpu_host(self) -> None:
        """NVIDIA の GPU があるパソコンなら、ほかのパソコンの声も GPU で文字にできるよう、裏で自動で準備する。

        ほかのパソコンの「別のパソコン」の一覧に出るには、このパソコン側の準備（Haas-ja・ファイアウォール・自動で動く登録）が
        必ず要るので、画面に出さずに OnseiMan がやる。失敗したら、やり直すのは 1 日に 1 回まで。
        動画を作る AI などで GPU の記憶を使い切るパソコンは、config.json の gpu_host_auto を false にする。
        """
        host = getattr(self.web_ui, "host_setup", None)
        # 切る方法: config.json の gpu_host_auto を false にするか、OnseiMan のフォルダに gpu_host_off という空のファイルを置く
        # （ファイルの印は、古い版が設定を保存し直しても消えない）
        if host is None or not getattr(self.config, "gpu_host_auto", True) or (ROOT_DIR / "gpu_host_off").exists():
            return
        if self._stop_event.wait(45):   # 起動の直後は、ほかの確かめと重ならないように少し待つ
            return
        state_path = LOG_DIR / "gpu_host_auto.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        try:
            st = host.status()
            if st.get("registered") == "gpu" or st.get("gpu_ready") or (st.get("job") or {}).get("running"):
                return   # もう準備してある（ログインしたときに自動で動く）
            if time.time() - float(state.get("last", 0)) < self.GPU_HOST_RETRY_SEC:
                return
            gpu = host._check_gpu()
            host._cache["gpu"] = gpu
            if not gpu.get("ok"):
                return   # NVIDIA の GPU が無い・ドライバーが古い（ふだん話すだけのパソコン）
            logger.info("GPU があるので、ほかのパソコンからも使えるように準備します: %s", gpu.get("message"))
            self.ui.show_status("◌ GPU があるので、ほかのパソコンの声もここで文字にできるよう準備します（10〜20 分）。"
                                "管理者の確認が出たら「はい」", bg="#0369a1", auto_hide_ms=8000)
            host.start_server("gpu")
            state = {"last": time.time(), "result": None}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            while not self._stop_event.wait(10):
                job = host.status().get("job") or {}
                if not job.get("running"):
                    break
            code = (host.status().get("job") or {}).get("result")
            state["result"] = code
            state_path.write_text(json.dumps(state), encoding="utf-8")
            if code == 0:
                logger.info("GPU で聞き取る準備ができました")
                self.ui.show_status("✓ 準備できました。ほかのパソコンの「別のパソコン」で、このパソコンが見つかります",
                                    bg="#065f46", auto_hide_ms=6000)
            elif code == 3010:
                self.ui.show_status("WSL2 を入れました。パソコンを再起動すると、続きを自動でします", bg="#92400e", auto_hide_ms=8000)
            else:
                logger.warning("GPU で聞き取る準備がうまくいきませんでした（記録: logs\\host_setup.log）: %s", code)
        except Exception:
            logger.exception("GPU で聞き取る準備を始められませんでした")

    def _update_loop(self) -> None:
        """起動したときと、そのあと 1 日 1 回、GitHub に新しい版がないか確かめる。"""
        while not self._stop_event.is_set():
            self._check_github_update(manual=False)
            self._stop_event.wait(self.UPDATE_CHECK_SEC)

    def _check_github_update(self, manual: bool = False) -> Optional[dict]:
        if FROZEN:
            self._check_update(manual)
            return None
        from client import github_update

        try:
            info = github_update.check()
        except Exception as exc:
            logger.info("更新の確認に失敗しました: %s", exc)
            if manual:
                self.ui.show_status("更新を確認できませんでした（インターネット接続を確認）", bg="#92400e", auto_hide_ms=2400)
            return self.update_info
        self.update_info = info
        if self._tray is not None:
            try:
                self._tray.update_menu()
            except Exception:
                pass
        if info is None:
            if manual:
                self.ui.show_status(f"✓ 最新版です（{__version__}）", bg="#065f46", auto_hide_ms=1800)
            return None
        logger.info("新しい版があります: %s → %s", __version__, info["version"])
        if manual or self._update_notified != info["version"]:
            self._update_notified = info["version"]
            self.ui.show_status(
                f"新しい版 {info['version']} があります（右上の版のボタンから更新）",
                bg="#0369a1",
                auto_hide_ms=5000,
            )
        return info

    _TS_CACHE_SEC = 600.0

    def _tailscale_names(self) -> dict:
        """IP → (パソコンの名前, Tailscale の IP)。古い認識役は自分の名前を返さないので、名前と「同じパソコンか」はここから引く。

        画面からの問い合わせ（3 秒ごと）の途中で Tailscale を待つと、Tailscale の返事が遅いとき
        （相手のパソコンがつながったり切れたりしたときなど。最大 6 秒）に画面の更新が止まる。
        そこで、取ってある一覧だけを返し、古ければ裏で取り直す。"""
        cache = getattr(self, "_ts_cache", None)
        if cache is None or time.monotonic() - cache[0] >= self._TS_CACHE_SEC:
            self._refresh_tailscale_names_later()
        return cache[1] if cache else {}

    def _refresh_tailscale_names_later(self) -> None:
        if getattr(self, "_ts_refreshing", False):
            return
        self._ts_refreshing = True

        def run() -> None:
            try:
                self._refresh_tailscale_names()
            finally:
                self._ts_refreshing = False

        threading.Thread(target=run, name="tailscale-names", daemon=True).start()

    def _refresh_tailscale_names(self) -> dict:
        table: dict = {}
        try:
            from client.discovery import tailscale_peers

            for p in tailscale_peers():
                for ip in [p.get("ip")] + list(p.get("lan_ips") or []):
                    if ip:
                        table[ip] = (str(p.get("name") or ""), str(p.get("ip") or ""))
        except Exception:
            pass
        self._ts_cache = (time.monotonic(), table)
        return table

    def _server_info_for_ui(self) -> dict:
        """画面に出すサーバーの情報。名前を返さない古い認識役は、Tailscale の名前で補う。"""
        out = {}
        ts = self._tailscale_names()
        for url in list(self.config.server_urls or []):
            info = dict(self.transcriber.server_info.get(url.rstrip("/")) or self.transcriber.server_info.get(url) or {})
            ip = url.split("//", 1)[-1].rsplit(":", 1)[0]
            # 名前を返さない古い認識役や、いま止まっている認識役は、Tailscale の一覧の名前を使う
            if not info.get("name") and ip in ts:
                info["name"] = ts[ip][0]
            if info:
                out[url] = info
        return out

    def _outdated_servers(self) -> list:
        """プログラムが古い認識役（このパソコンの一式と指紋が違い、版も古いもの）。"""
        from client.updater import is_newer
        from shared.server_fingerprint import fingerprint

        if not getattr(self, "_local_fingerprint", ""):
            self._local_fingerprint = fingerprint(ROOT_DIR)
        out = []
        seen = set()
        names = dict(getattr(self.config, "server_names", {}) or {})
        for url in list(self.config.server_urls or []):
            info = self.transcriber.server_info.get(url.rstrip("/")) or self.transcriber.server_info.get(url)
            if not info or info.get("fingerprint") == self._local_fingerprint:
                continue
            ver = info.get("version") or ""
            if ver and not is_newer(__version__, ver):
                continue  # 認識役のほうが新しい（このパソコンの更新はお知らせの帯で）
            # 同じパソコンが Tailscale と家のネットワークの両方で登録されていたら、1 つにまとめる
            ip = url.split("//", 1)[-1].rsplit(":", 1)[0]
            ts_name, ts_ip = self._tailscale_names().get(ip, ("", ""))
            key = info.get("id") or ts_ip or info.get("name") or url
            if key in seen:
                continue
            seen.add(key)
            name = names.get(url) or info.get("name") or ts_name
            out.append({"url": url, "name": f"{name}（{ip}）" if name else ip, "version": ver or "1.1.0 以前"})
        return out

    def _update_payload(self) -> Optional[dict]:
        if not self.update_info:
            return None
        from client import github_update

        # 開発用のフォルダ（git）でも押せる。update.ps1 が、作業中の変更や手元だけの記録があれば何も変えずに止める
        return {**self.update_info, "can_run": True, "dev": github_update.is_dev_folder(ROOT_DIR)}

    def _on_tray_update(self) -> None:
        if self.update_info:
            self.web_ui.open_window("history")  # 画面の上のお知らせから更新する（確認してから）
        else:
            threading.Thread(target=self._check_github_update, args=(True,), daemon=True).start()

    def _run_github_update(self) -> None:
        """「今すぐ更新」: 更新を別の窓で始めて、OnseiMan を終了する（更新が済んだら起動し直す）。"""
        from client import github_update

        if not self.update_info:
            raise ValueError("新しい版はありません")

        def _go() -> None:
            # 録音中・認識中なら終わるまで待つ
            while self.recorder.is_recording or self._processing:
                self._stop_event.wait(0.5)
            try:
                github_update.launch_update(ROOT_DIR, os.getpid())
            except Exception as exc:
                logger.warning("更新を始められませんでした: %s", exc)
                self.ui.show_status(f"✕ 更新を始められませんでした: {exc}", bg="#991b1b", auto_hide_ms=3000)
                return
            logger.info("更新のため終了します: %s → %s", __version__, self.update_info["version"])
            self.ui.show_status("更新しています。まもなく起動し直します…", bg="#0369a1")
            self._stop_event.wait(0.8)
            self.quit()

        threading.Thread(target=_go, daemon=True).start()

    def _check_update(self, manual: bool = False) -> None:
        """新しいバージョンがあれば、聞いてから更新する（配布版のみ）。"""
        from client import updater

        if not FROZEN:
            if manual:
                self.ui.show_status("開発中の版では更新を確認しません", bg="#4b5563", auto_hide_ms=1800)
            return
        try:
            info = updater.fetch_latest()
        except Exception as exc:
            logger.info("更新の確認に失敗しました: %s", exc)
            if manual:
                self.ui.show_status("更新を確認できませんでした（インターネット接続を確認）", bg="#92400e", auto_hide_ms=2400)
            return
        if info is None or not updater.is_newer(info.version):
            if manual:
                self.ui.show_status(f"✓ 最新版です（{__version__}）", bg="#065f46", auto_hide_ms=1800)
            return
        logger.info("新しいバージョンがあります: %s → %s", __version__, info.version)
        if not updater.ask_user(info):
            return
        # 録音中・認識中なら終わるまで待つ
        while self.recorder.is_recording or self._processing:
            self._stop_event.wait(0.5)
        try:
            last = {"pct": -1}

            def progress(done: int, total: int) -> None:
                pct = done * 100 // total if total else 0
                if pct // 10 != last["pct"] // 10:
                    last["pct"] = pct
                    self.ui.show_status(f"↓ 更新をダウンロードしています… {pct}%", bg="#0369a1")

            path = updater.download_installer(info, on_progress=progress)
        except Exception as exc:
            logger.warning("更新のダウンロードに失敗しました: %s", exc)
            self.ui.show_status(f"✕ 更新できませんでした: {exc}", bg="#991b1b", auto_hide_ms=3000)
            return
        logger.info("更新を始めます: %s", path)
        self.ui.show_status("更新しています。まもなく起動し直します…", bg="#0369a1")
        updater.launch_installer(path)
        self.quit()

    def _copy_last_result(self) -> None:
        text = self.app_logger.last_text
        if text:
            set_clipboard_text(text)
            self.ui.show_status("✓ 直前の結果をコピーしました", bg="#065f46", auto_hide_ms=1100)
        else:
            self.ui.show_status("まだ認識履歴がありません", bg="#4b5563", auto_hide_ms=1100)

    def quit(self) -> None:
        logger.info("OnseiMan を終了します")
        if self.heartbeat is not None:
            self.heartbeat.mark_quitting()  # 見張りに「正常終了なので再起動しない」と伝える
        self._stop_event.set()
        self.hook.stop()
        try:
            self.audio_muter.stop()
        except Exception:
            pass
        try:
            self.recorder.close()
        except Exception:
            pass
        self.web_ui.stop()
        self.ui.stop()
        if self._tray is not None:
            self._tray.stop()
        self.app_logger.close()

    def run(self) -> None:
        if not _acquire_single_instance():
            logger.info("OnseiMan は既に起動しています（2重起動をスキップ）")
            self.app_logger.close()
            return

        logger.info("OnseiMan %s を起動します (hotkey=%s)", __version__, self.config.hotkey)
        # 前回落ちたときに Alt などが押しっぱなし扱いで残っていたら解放する
        release_stuck_modifiers(logger)
        # 前回落ちたときに、録音中に消したほかのアプリの音が消えたままなら戻す
        self.audio_muter.restore_leftover()

        # キーボード保護: フックスレッドの心拍を別プロセスが見張り、途絶えたら本体を止めてキーボードを解放する
        try:
            self.heartbeat = Heartbeat.create()
            self.hook.heartbeat = self.heartbeat
        except Exception:
            logger.exception("心拍の共有メモリを作れませんでした（見張りなしで起動します）")
        self.hook.start()
        self.dict_sync.start()
        if self.config.keep_mic_open:
            threading.Thread(target=self.recorder.warm_up, daemon=True).start()
        if self.heartbeat is not None:
            if spawn_watchdog(os.getpid()) is not None:
                logger.info("キーボード保護の見張りを起動しました")

        self._tray = pystray.Icon(
            "OnseiMan",
            _make_icon_image("#6b7280"),
            f"OnseiMan | 起動中 | キー: {self.config.hotkey}",
            menu=self._build_tray_menu(),
        )
        self._tray.run_detached()
        logger.info("タスクトレイアイコンを起動しました")

        def _on_ui_ready() -> None:
            self.web_ui.open_window("history")
            logger.info("Web UI メインウィンドウを表示しました: %s", self.web_ui.url)

            def _init_health() -> None:
                if self.transcriber.check_server_health() is None and (
                    self.config.local_fallback or not self.transcriber.uses_servers()
                ):
                    self.ui.show_status("◌ 音声認識の準備をしています…", bg="#4c1d95")
                    self.transcriber.preload_local()
                self._restore_idle_icon()
                self.ui.refresh_history_if_open()
                self.ui.show_status(
                    f"✓ 準備できました（{self.friendly_backend(self.transcriber.last_backend_status)}）",
                    bg="#065f46",
                    auto_hide_ms=2500,
                )
                logger.info("初期ヘルスチェック完了: %s", self.transcriber.last_backend_status)
                self._refresh_tailscale_names_later()  # 名前の一覧を裏で取っておく（画面を待たせない）
                if FROZEN:
                    threading.Thread(target=self._check_update, daemon=True).start()
                else:
                    threading.Thread(target=self._update_loop, name="update-check", daemon=True).start()
                    threading.Thread(target=self._auto_gpu_host, name="gpu-host", daemon=True).start()
                threading.Thread(target=self._prepare_haas, name="haas-setup", daemon=True).start()
                self._health_poll_loop()

            threading.Thread(target=_init_health, daemon=True).start()

        self.ui.run_mainloop(on_ready=_on_ui_ready)


_instance_mutex = None


def _acquire_single_instance() -> bool:
    """2重起動を防ぐ。すでにこのプロセスが持っていれば True、他の OnseiMan が動いていれば False。"""
    global _instance_mutex
    if _instance_mutex:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "Global\\OnseiManClientSingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def _prepare_first_run() -> None:
    """配布版の初回起動: 設定と空の辞書を、ユーザーのフォルダに作る（作者の辞書や設定は入れない）。"""
    import json

    from shared.fillers import DEFAULT_FILLERS

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg_path = DATA_DIR / "config.json"
    if not cfg_path.exists():
        cpu = os.cpu_count() or 4
        cfg = ClientConfig(
            hotkey="ralt",
            hotkey_mode="toggle",
            recognition_place="local",   # このパソコンだけで認識する
            server_urls=[],
            local_fallback=True,
            local_num_threads=max(2, min(8, cpu // 2)),
            llm_base_url="",            # AI 整形は使わない（使う人は設定で入れる）
        )
        cfg.save(cfg_path)
    dict_path = DATA_DIR / "dict.json"
    if not dict_path.exists():
        dict_path.write_text(
            json.dumps(
                {"hotwords": [], "replacements": {}, "fillers": list(DEFAULT_FILLERS), "builtin_off": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _ensure_model(app: "OnseiManApp") -> bool:
    """このパソコンで認識するのにモデルが無ければ、ダウンロード画面を出す。続けてよければ True。"""
    from client.model_setup import model_ready, run_setup_window, show_message

    cfg = app.config
    model_dir = Path(cfg.local_model_dir)
    if not model_dir.is_absolute():
        model_dir = DATA_DIR / model_dir
    place = (cfg.recognition_place or ("server" if cfg.server_urls else "local"))
    if (place == "server" and not cfg.local_fallback) or model_ready(model_dir):
        return True
    if run_setup_window(model_dir.parent):
        return True
    if place == "server" and cfg.server_urls:
        return True  # サーバーがあればモデル無しでも動ける
    show_message(
        "OnseiMan",
        "音声認識のデータが用意できなかったため終了します。\n"
        "インターネットにつながった状態で、もう一度 OnseiMan を起動してください。",
    )
    return False


def _selftest(out_path: str) -> int:
    """配布版の中身の確認用（画面もキーボードの監視も使わない）。結果を out_path に書く。"""
    import json
    import traceback

    report: dict = {"frozen": FROZEN, "data_dir": str(DATA_DIR)}
    try:
        from client.web_ui import WEB_DIR

        report["web_files"] = sorted(p.name for p in WEB_DIR.iterdir())
        report["microphones"] = list_input_devices()
        import uiautomation  # noqa: F401

        report["uiautomation"] = True
        from pycaw.pycaw import AudioUtilities

        report["audio_sessions"] = len(AudioUtilities.GetAllSessions())
        from client.pill import StatusPill  # noqa: F401
        model_dir = Path(sys.argv[sys.argv.index("--selftest-model") + 1]) if "--selftest-model" in sys.argv else None
        if model_dir:
            from shared.asr_engine import FunASRNanoEngine

            eng = FunASRNanoEngine(model_dir=model_dir, provider="cpu", num_threads=4)
            eng.load()
            wav = model_dir / "test_wavs" / "ja.wav"
            samples, sr = decode_wav_bytes(wav.read_bytes())
            text, ms = eng.transcribe(samples=samples, sample_rate=sr)
            report["asr_text"] = text
            report["asr_ms"] = round(ms)
        report["ok"] = True
    except Exception:
        report["ok"] = False
        report["error"] = traceback.format_exc()
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(_selftest(sys.argv[sys.argv.index("--selftest") + 1]))
    if "--watchdog" in sys.argv:
        # 配布版: 同じ exe を見張り役として起動したとき
        from client.watchdog import run_watchdog

        i = sys.argv.index("--watchdog")
        sys.exit(run_watchdog(int(sys.argv[i + 1]), restart="--no-restart" not in sys.argv))
    if FROZEN:
        # 画面なしの exe では標準出力が無いので、書き込みで落ちないよう捨て先を用意する
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
        _prepare_first_run()
    _enable_crash_log()
    if not _acquire_single_instance():
        logging.getLogger("onseiman.client").info("OnseiMan は既に起動しています（2重起動をスキップ）")
        return
    app = OnseiManApp()
    if not _ensure_model(app):  # このパソコンで認識するのにデータが無ければ、ダウンロード画面を出す
        app.app_logger.close()
        return
    app.run()


if __name__ == "__main__":
    main()
