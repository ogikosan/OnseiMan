"""録音中に届いた区切りを、裏で順番に認識しておく。

録音が終わったら finish() で残りの区切りを待ち、全部の結果を順番どおりにつなぐ。
認識に失敗した区切りは、最後にもう一度だけ認識し直す。それでも失敗したらエラーにする
（一部だけ黙って欠けた文を入力しないため）。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np

from shared.segmenter import join_texts

logger = logging.getLogger("onseiman.live")


@dataclass
class LiveResult:
    text: str
    backend: str
    segments: int
    wait_ms: float        # 録音終了から全部の認識が終わるまでの時間（体感の待ち時間）
    asr_ms_total: float   # 各区切りの認識時間の合計


class LiveTranscription:
    def __init__(
        self,
        transcribe_fn: Callable[[np.ndarray], object],
        on_progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        """transcribe_fn(samples) は .text / .backend / .elapsed_ms を持つ結果を返す。"""
        self._transcribe_fn = transcribe_fn
        self._on_progress = on_progress
        self._queue: "queue.Queue[Optional[tuple[int, np.ndarray]]]" = queue.Queue()
        self._lock = threading.Lock()
        self._samples: Dict[int, np.ndarray] = {}
        self._texts: Dict[int, str] = {}
        self._errors: Dict[int, Exception] = {}
        self._backend = ""
        self._asr_ms = 0.0
        self._count = 0
        self._cancelled = False
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="onseiman-live-asr", daemon=True)
        self._worker.start()

    @property
    def segment_count(self) -> int:
        return self._count

    def submit(self, samples: np.ndarray) -> None:
        with self._lock:
            if self._closed:
                return
            idx = self._count
            self._count += 1
            self._samples[idx] = samples
        self._queue.put((idx, samples))

    def text_so_far(self) -> str:
        with self._lock:
            done = []
            for i in range(self._count):
                if i not in self._texts:
                    break
                done.append(self._texts[i])
        return join_texts(done)

    def _recognize(self, idx: int, samples: np.ndarray) -> None:
        try:
            res = self._transcribe_fn(samples)
            with self._lock:
                self._texts[idx] = getattr(res, "text", "") or ""
                self._errors.pop(idx, None)
                self._backend = getattr(res, "backend", "") or self._backend
                self._asr_ms += float(getattr(res, "elapsed_ms", 0.0) or 0.0)
        except Exception as exc:
            logger.warning("区切り %d の認識に失敗: %s", idx + 1, exc)
            with self._lock:
                self._errors[idx] = exc

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if self._cancelled:
                continue
            idx, samples = item
            self._recognize(idx, samples)
            if self._on_progress is not None and not self._cancelled:
                try:
                    self._on_progress(self.text_so_far())
                except Exception:
                    pass

    def cancel(self) -> None:
        self._cancelled = True
        with self._lock:
            self._closed = True
        self._queue.put(None)

    def finish(self, timeout_sec: float = 120.0) -> LiveResult:
        t0 = time.perf_counter()
        with self._lock:
            self._closed = True
        self._queue.put(None)
        self._worker.join(timeout=timeout_sec)
        if self._worker.is_alive():
            raise TimeoutError("認識が時間内に終わりませんでした")
        failed = sorted(self._errors)
        for idx in failed:  # 失敗した区切りはもう一度だけ
            self._recognize(idx, self._samples[idx])
        if self._errors:
            first = self._errors[min(self._errors)]
            raise RuntimeError(f"{len(self._errors)} 個の区切りを認識できませんでした: {first}")
        texts: List[str] = [self._texts.get(i, "") for i in range(self._count)]
        return LiveResult(
            text=join_texts(texts),
            backend=self._backend,
            segments=self._count,
            wait_ms=(time.perf_counter() - t0) * 1000.0,
            asr_ms_total=self._asr_ms,
        )
