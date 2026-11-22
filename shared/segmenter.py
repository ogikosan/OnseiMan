"""録音を「話の切れ目（短い無音）」で区切る。

Fun-ASR Nano は一度に扱える量に上限があり（約 30 秒を超えると結果が空になる。優先語が多いと
20 秒台から後半が欠ける）、長い発話はそのままでは認識できない。そこで録音しながら切れ目を見つけ、
区切りごとに認識できるようにする。

- 区切りが min_segment_sec 以上たまり、その後 pause_sec 以上の無音が続いたら、無音の終わりで切る
  （しきい値より小さい語尾の声を切り落とさないため）。
- 切れ目がないまま max_segment_sec に達したら、直近数秒のうち一番静かな所で強制的に切る。
- ほぼ完全に無音の区切りは捨てる（認識に送らない）。

録音中の逐次処理（feed）と、保存済み音声の一括分割（split_on_pauses）は同じ規則で動く。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

FRAME_SEC = 0.02


class Segmenter:
    def __init__(
        self,
        sample_rate: int = 16000,
        rms_threshold: float = 0.012,
        pause_sec: float = 0.5,
        min_segment_sec: float = 4.0,
        max_segment_sec: float = 18.0,
        search_back_sec: float = 6.0,
        drop_below_rms: Optional[float] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.rms_threshold = rms_threshold
        # 区切りを捨てる（認識に送らない）のは、ほぼ完全に無音のときだけ。小声を取りこぼさないよう
        # 切れ目の判定（rms_threshold）よりずっと低くする。
        self.drop_below_rms = rms_threshold * 0.25 if drop_below_rms is None else drop_below_rms
        self.frame = max(1, int(sample_rate * FRAME_SEC))
        self.pause_frames = max(1, int(round(pause_sec / FRAME_SEC)))
        self.min_frames = int(round(min_segment_sec / FRAME_SEC))
        self.max_frames = int(round(max_segment_sec / FRAME_SEC))
        self.search_frames = int(round(search_back_sec / FRAME_SEC))
        self._buf = np.zeros(0, dtype=np.float32)   # 現在の区切りの音声
        self._rms: List[float] = []                  # 現在の区切りのフレームごとの音量
        self._pending = np.zeros(0, dtype=np.float32)  # フレームに満たない端数
        self._silent_run = 0

    def feed(self, chunk: np.ndarray) -> List[np.ndarray]:
        """音声を足し、切れた区切り（声を含むものだけ）を返す。"""
        out: List[np.ndarray] = []
        data = np.concatenate([self._pending, np.asarray(chunk, dtype=np.float32).ravel()])
        n_full = len(data) // self.frame
        self._pending = data[n_full * self.frame:]
        if n_full == 0:
            return out
        frames = data[: n_full * self.frame]
        self._buf = np.concatenate([self._buf, frames])
        for i in range(n_full):
            f = frames[i * self.frame:(i + 1) * self.frame]
            r = float(np.sqrt(np.mean(f * f)))
            self._rms.append(r)
            self._silent_run = self._silent_run + 1 if r < self.rms_threshold else 0
            n = len(self._rms)
            cut_at: Optional[int] = None
            if n >= self.min_frames and self._silent_run >= self.pause_frames:
                # 無音と判定した部分は前の区切りに全部含める（小声の語尾を切り落とさない）
                cut_at = n
            elif n >= self.max_frames:
                cut_at = self._quietest_cut(n)
            if cut_at is not None:
                seg = self._take(cut_at)
                if seg is not None:
                    out.append(seg)
        return out

    def _quietest_cut(self, n: int) -> int:
        lo = max(self.min_frames // 2, n - self.search_frames)
        win = 10  # 0.2 秒の平均で一番静かな所
        rms = np.asarray(self._rms, dtype=np.float32)
        best, best_val = n, float("inf")
        for i in range(lo, n - win + 1):
            v = float(rms[i:i + win].mean())
            if v < best_val:
                best, best_val = i + win // 2, v
        return best

    def _take(self, cut_frames: int) -> Optional[np.ndarray]:
        cut = cut_frames * self.frame
        seg, seg_rms = self._buf[:cut], self._rms[:cut_frames]
        self._buf = self._buf[cut:]
        self._rms = self._rms[cut_frames:]
        self._silent_run = 0
        for r in reversed(self._rms):
            if r < self.rms_threshold:
                self._silent_run += 1
            else:
                break
        if not seg_rms or max(seg_rms) < self.drop_below_rms:
            return None
        return seg

    def flush(self) -> Optional[np.ndarray]:
        """残り（最後の区切り）を返す。声がほとんど無ければ None。"""
        tail = np.concatenate([self._buf, self._pending])
        rms = self._rms
        self._buf = np.zeros(0, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._rms = []
        self._silent_run = 0
        if not rms:
            return None  # 1 フレームに満たない端数だけ
        if max(rms) < self.drop_below_rms:
            return None
        return tail


def split_on_pauses(samples: np.ndarray, sample_rate: int = 16000, **kwargs) -> List[np.ndarray]:
    """保存済みの音声を同じ規則で区切る。声が全く検出されないときは元の音声をそのまま 1 つ返す。"""
    seg = Segmenter(sample_rate=sample_rate, **kwargs)
    parts: List[np.ndarray] = []
    block = 1024
    samples = np.asarray(samples, dtype=np.float32).ravel()
    for i in range(0, len(samples), block):
        parts.extend(seg.feed(samples[i:i + block]))
    tail = seg.flush()
    if tail is not None:
        parts.append(tail)
    return parts or ([samples] if len(samples) else [])


def join_texts(texts: List[str]) -> str:
    """区切りごとの認識結果をつなぐ。英数字どうしが接するときだけ空白を入れる。"""
    out = ""
    for t in texts:
        t = (t or "").strip()
        if not t:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and t[0].isascii() and t[0].isalnum():
            out += " "
        out += t
    return out
