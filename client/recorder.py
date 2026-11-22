from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

import numpy as np
import sounddevice as sd

from shared.audio import samples_to_wav_bytes
from shared.segmenter import Segmenter

logger = logging.getLogger("onseiman.recorder")


def _mme_hostapi_index() -> Optional[int]:
    for i, api in enumerate(sd.query_hostapis()):
        if api.get("name") == "MME":
            return i
    return None


def list_input_devices() -> List[str]:
    """設定画面に出すマイク名の一覧（同じマイクが方式違いで重複しないよう MME のものだけ）。"""
    try:
        mme = _mme_hostapi_index()
        names: List[str] = []
        for d in sd.query_devices():
            if d["max_input_channels"] <= 0 or (mme is not None and d["hostapi"] != mme):
                continue
            name = str(d["name"])
            if "サウンド マッパー" in name or "Sound Mapper" in name:
                continue
            if name not in names:
                names.append(name)
        return names
    except Exception as exc:
        logger.warning("マイク一覧を取得できませんでした: %s", exc)
        return []


def resolve_input_device(device: Optional[int | str]) -> Optional[int]:
    """設定のマイク（名前または番号）を、今つながっている機器の番号に変える。

    番号は USB の抜き差しでずれるので、設定には名前を保存し、録音のたびにここで探す。
    見つからなければ既定のマイク（None）を使う。
    """
    if device is None or device == "":
        return None
    if isinstance(device, int):
        return device
    try:
        mme = _mme_hostapi_index()
        candidates = [
            (i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0
        ]
        for want_mme in (True, False):
            for i, d in candidates:
                if want_mme and mme is not None and d["hostapi"] != mme:
                    continue
                if str(d["name"]) == device:
                    return i
        for i, d in candidates:
            if str(d["name"]).startswith(device) or device.startswith(str(d["name"])):
                return i
    except Exception as exc:
        logger.warning("マイクを探せませんでした: %s", exc)
    logger.warning("設定のマイク %r が見つからないため、既定のマイクを使います", device)
    return None


class AudioRecorder:
    """Thread-safe audio recorder using sounddevice (16kHz mono float32).

    - PortAudio の開閉は _op_lock で直列化する（連打・キャンセルで同時に開閉しない）。
    - keep_open=True なら録音していない間もマイクを開いたままにし、直近 preroll_sec 秒を
      輪っか状に持っておく。キーを押す少し前の声（言い出しの一音）も録れ、開く待ち時間も無くなる。
      端末のスリープ復帰やマイクの抜き差しで止まっていたら、次の録音開始時に開き直す。
    - segment_kwargs を渡すと、録音しながら話の切れ目で区切り、区切りごとに on_segment を呼ぶ
      （長い発話でも認識できる。話している間に前半の認識を進められる）。
    """

    STALE_STREAM_SEC = 0.6
    NO_SIGNAL_CHECK_SEC = 1.5

    def __init__(
        self,
        sample_rate: int = 16000,
        device: Optional[int | str] = None,
        max_duration_sec: float = 120.0,
        on_level: Optional[Callable[[float], None]] = None,
        auto_stop_on_silence: bool = False,
        silence_stop_sec: float = 1.8,
        silence_rms_threshold: float = 0.012,
        on_silence_timeout: Optional[Callable[[], None]] = None,
        keep_open: bool = False,
        preroll_sec: float = 0.4,
        on_segment: Optional[Callable[[np.ndarray], None]] = None,
        segment_kwargs: Optional[dict] = None,
        on_no_signal: Optional[Callable[[], None]] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.max_duration_sec = max_duration_sec
        self.on_level = on_level
        self.auto_stop_on_silence = auto_stop_on_silence
        self.silence_stop_sec = silence_stop_sec
        self.silence_rms_threshold = silence_rms_threshold
        self.on_silence_timeout = on_silence_timeout
        self.keep_open = keep_open
        self.preroll_sec = preroll_sec
        self.on_segment = on_segment
        self.segment_kwargs = segment_kwargs  # None なら区切らない
        self.on_no_signal = on_no_signal

        self._op_lock = threading.RLock()
        self._data_lock = threading.Lock()
        self._recording = False
        self._cancelled = False
        self._chunks: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._stream_device: Optional[int] = None
        self._start_time: float = 0.0
        self._speech_detected: bool = False
        self._last_speech_time: float = 0.0
        self._silence_fired: bool = False
        self._ring: Deque[np.ndarray] = deque()
        self._ring_len = 0
        self._last_callback_time = 0.0
        self._segmenter: Optional[Segmenter] = None
        self._peak_rms = 0.0
        self._no_signal_checked = False
        self.segments_emitted = 0

    @property
    def is_recording(self) -> bool:
        # Called from the keyboard hook thread on every Esc press: must never block on a lock.
        # Reading a bool attribute is atomic in CPython.
        return self._recording

    @property
    def is_warm(self) -> bool:
        return self._stream is not None

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            logger.debug("sounddevice status: %s", status)
        chunk = None
        fire_silence = False
        fire_no_signal = False
        segments: List[np.ndarray] = []
        now = time.perf_counter()
        rms = 0.0
        with self._data_lock:
            self._last_callback_time = now
            chunk = indata[:, 0].copy()
            if not self._recording:
                if self.keep_open and self.preroll_sec > 0:
                    self._ring.append(chunk)
                    self._ring_len += len(chunk)
                    limit = int(self.sample_rate * self.preroll_sec)
                    while self._ring and self._ring_len - len(self._ring[0]) >= limit:
                        self._ring_len -= len(self._ring.popleft())
                return
            self._chunks.append(chunk)
            elapsed = now - self._start_time
            if elapsed >= self.max_duration_sec:
                self._recording = False
            if self._segmenter is not None:
                segments = self._segmenter.feed(chunk)
                self.segments_emitted += len(segments)
            if chunk.size > 0:
                rms = float(np.sqrt(np.mean(chunk * chunk)))
                self._peak_rms = max(self._peak_rms, rms)
                if not self._no_signal_checked and elapsed >= self.NO_SIGNAL_CHECK_SEC:
                    self._no_signal_checked = True
                    fire_no_signal = self._peak_rms < 1e-5
                if self.auto_stop_on_silence and self.silence_stop_sec > 0:
                    if rms >= self.silence_rms_threshold:
                        self._speech_detected = True
                        self._last_speech_time = now
                    elif (
                        self._speech_detected
                        and not self._silence_fired
                        and (now - self._last_speech_time) >= self.silence_stop_sec
                    ):
                        self._silence_fired = True
                        fire_silence = True

        if self.on_level is not None and chunk is not None and chunk.size > 0:
            try:
                self.on_level(min(1.0, rms * 10.0))
            except Exception:
                pass
        for seg in segments:
            self._emit_segment(seg)
        if fire_no_signal and self.on_no_signal is not None:
            try:
                self.on_no_signal()
            except Exception:
                pass
        if fire_silence and self.on_silence_timeout is not None:
            try:
                self.on_silence_timeout()
            except Exception:
                pass

    def _emit_segment(self, seg: np.ndarray) -> None:
        if self.on_segment is None:
            return
        try:
            self.on_segment(seg)
        except Exception:
            logger.exception("区切りの受け渡しに失敗しました")

    def _stream_usable(self, want_device: Optional[int]) -> bool:
        stream = self._stream
        if stream is None or self._stream_device != want_device:
            return False
        try:
            if not stream.active:
                return False
        except Exception:
            return False
        return (time.perf_counter() - self._last_callback_time) < self.STALE_STREAM_SEC

    def _open_stream_unlocked(self, device: Optional[int]) -> bool:
        self._close_stream_unlocked()
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=device,
                callback=self._callback,
                blocksize=1024,
            )
            stream.start()
            self._stream = stream
            self._stream_device = device
            self._last_callback_time = time.perf_counter()
            return True
        except Exception as exc:
            logger.error("マイクを開けませんでした: %s", exc)
            self._close_stream_unlocked()
            return False

    def warm_up(self) -> bool:
        """keep_open のとき、録音前からマイクを開いておく。"""
        if not self.keep_open:
            return False
        with self._op_lock:
            if self._recording:
                return True
            device = resolve_input_device(self.device)
            if self._stream_usable(device):
                return True
            with self._data_lock:
                self._ring.clear()
                self._ring_len = 0
            ok = self._open_stream_unlocked(device)
            if ok:
                logger.info("マイクを待機状態で開きました（押す直前の声も録れます）")
            return ok

    def set_keep_open(self, keep_open: bool) -> None:
        with self._op_lock:
            self.keep_open = keep_open
            if not keep_open and not self._recording:
                self._close_stream_unlocked()

    def start(self) -> bool:
        with self._op_lock:
            device = resolve_input_device(self.device)
            reuse = self.keep_open and self._stream_usable(device)
            with self._data_lock:
                if self._recording:
                    return False
                preroll = list(self._ring) if reuse else []
                self._ring.clear()
                self._ring_len = 0
                self._chunks = preroll
                self._cancelled = False
                self._start_time = time.perf_counter()
                self._speech_detected = False
                self._last_speech_time = self._start_time
                self._silence_fired = False
                self._peak_rms = 0.0
                self._no_signal_checked = False
                self.segments_emitted = 0
                self._segmenter = (
                    Segmenter(sample_rate=self.sample_rate, **self.segment_kwargs)
                    if self.segment_kwargs is not None
                    else None
                )
                if self._segmenter is not None and preroll:
                    # 押す前の 0.4 秒ぶんは区切りが出るほど長くないので、戻り値は空
                    self._segmenter.feed(np.concatenate(preroll))
                self._recording = True

            if reuse:
                return True
            if self._open_stream_unlocked(device):
                return True
            logger.error("マイク録音の開始に失敗しました")
            with self._data_lock:
                self._recording = False
                self._segmenter = None
            return False

    def cancel(self) -> bool:
        with self._op_lock:
            with self._data_lock:
                if not self._recording:
                    return False
                self._recording = False
                self._cancelled = True
                self._chunks = []
                self._segmenter = None
            if not self.keep_open:
                self._close_stream_unlocked()
            return True

    def stop(self) -> Tuple[Optional[np.ndarray], float, bool]:
        """Stop recording. Returns (samples or None, duration_sec, was_cancelled).

        区切りを使っているときは、最後の区切り（残り）を on_segment で渡してから戻る。
        """
        with self._op_lock:
            with self._data_lock:
                was_cancelled = self._cancelled
                if not self._recording and not self._chunks:
                    return None, 0.0, was_cancelled
                self._recording = False
                duration = time.perf_counter() - self._start_time
                chunks = self._chunks
                self._chunks = []
                segmenter = self._segmenter
                self._segmenter = None

            if not self.keep_open:
                self._close_stream_unlocked()

            if was_cancelled or not chunks:
                return None, duration, was_cancelled

            if segmenter is not None:
                tail = segmenter.flush()
                if tail is not None:
                    self.segments_emitted += 1
                    self._emit_segment(tail)

            samples = np.concatenate(chunks).astype(np.float32)
            actual_duration = len(samples) / float(self.sample_rate)
            return samples, actual_duration, False

    def close(self) -> None:
        with self._op_lock:
            with self._data_lock:
                self._recording = False
            self._close_stream_unlocked()

    def _close_stream_unlocked(self) -> None:
        stream = self._stream
        self._stream = None
        self._stream_device = None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
