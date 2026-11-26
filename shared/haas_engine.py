"""GPU で聞き取る版の認識（Haas-ja-v1.0, Third Intelligence, Apache-2.0）。

Linux と NVIDIA の GPU が要る（Windows では WSL2 の中で動かす。server_gpu.bat が用意する）。
FunASRNanoEngine と同じ形（load / transcribe）で使える。

- 読み込みのあとに、読み込みのときだけ使った GPU の記憶を返す（ほかのアプリ＝動画を作る AI などが使えるように）。
  速くする仕組み（CUDA グラフ）はそのまま使うので、聞き取りの速さは変わらない。
- 辞書の優先語は、Haas の「言葉を覚えさせる仕組み」（Vocabulary）に渡す。
- Haas は英字をすべて小文字で書くので、よく使う略語は大文字に直す（cpu → CPU）。
"""
from __future__ import annotations

import gc
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("onseiman.haas")

MODEL_ID = "third-intelligence/Haas-ja-v1.0"
# 試して確かめた版に固定する（中身が変わると聞き取りも変わるため）
MODEL_REVISION = "660728d39019e5e065046154beca44ac9a66a540"
SAMPLE_RATE = 16000
# 学習は 30 秒までの発話。少し余裕を持たせる
MAX_SEGMENT_SEC = 25.0

# 大文字で書く略語（Haas は英字をすべて小文字で書く）。優先語に入れた言葉は、その書き方が優先される
ACRONYMS = (
    "AI", "API", "CPU", "GPU", "LLM", "PC", "OS", "USB", "SSD", "HDD", "URL", "PDF", "CSV", "SNS",
    "SSH", "WSL", "CUDA", "VRAM", "RAM", "IT", "IP", "LAN", "DM", "QR", "UI", "UX", "PR", "CM",
    "NG", "OK", "HP", "ID", "PIN", "FAQ", "HTML", "CSS", "JSON", "SQL", "AWS", "MCP", "DGX", "RTX",
    "GB", "MB", "TB", "KB", "TV", "PS", "VR", "AR", "GPT",
)
_ACRONYM_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(sorted((a.lower() for a in ACRONYMS), key=len, reverse=True)) + r")(?![A-Za-z])")


def fix_acronyms(text: str) -> str:
    """小文字で出た略語を大文字にする（llm → LLM）。前後が英字のとき（apple など）は触らない。"""
    return _ACRONYM_RE.sub(lambda m: m.group(1).upper(), text)


def _resample(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == SAMPLE_RATE or samples.size == 0:
        return samples
    n = int(round(samples.size * SAMPLE_RATE / float(sample_rate)))
    x_old = np.linspace(0.0, 1.0, num=samples.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=max(n, 1), endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class HaasEngine:
    """Haas-ja を GPU で動かす。スレッドから同時に呼ばれても 1 本ずつ処理する。"""

    def __init__(self, model_id: str = MODEL_ID, revision: str = MODEL_REVISION) -> None:
        self.model_id = model_id
        self.revision = revision
        self.active_provider = "cuda"
        self._lock = threading.Lock()
        self._model = None
        self._vocab_key: Optional[str] = None
        self._vocab = None

    @property
    def model_name(self) -> str:
        return self.model_id.rsplit("/", 1)[-1]

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            import torch
            import haas_asr

            if not torch.cuda.is_available():
                raise RuntimeError("NVIDIA の GPU が見つかりません（GPU で聞き取る版は NVIDIA の GPU が要ります）")
            t0 = time.perf_counter()
            from huggingface_hub import snapshot_download

            path = snapshot_download(self.model_id, revision=self.revision)
            nemo = next(Path(path).glob("*.nemo"), None)
            self._model = haas_asr.load_model(str(nemo) if nemo else self.model_id)
            # 読み込みのときだけ使った記憶を返す（速くする仕組みを作る前なので、速さには響かない）
            gc.collect()
            torch.cuda.empty_cache()
            # 1 回空で動かして、速くする仕組みを先に作っておく（最初の 1 回が遅くならないように）
            silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
            self._model.transcribe([silence], verbose=False)
            torch.cuda.synchronize()
            logger.info(
                "Haas-ja を読み込みました (%.1f 秒, GPU=%s, 使っている記憶 %.0f MB)",
                time.perf_counter() - t0,
                torch.cuda.get_device_name(0),
                torch.cuda.memory_reserved() / 2**20,
            )

    def _vocabulary(self, hotwords: str, readings: Optional[dict] = None):
        words = [w.strip() for w in hotwords.split(",") if w.strip()]
        readings = readings or {}
        vocab = {w: [r for r in readings.get(w, []) if r] for w in words}
        key = repr(sorted(vocab.items()))
        if key != self._vocab_key:
            import haas_asr

            try:
                self._vocab = haas_asr.Vocabulary(vocab) if vocab else None
            except Exception as exc:  # 使えない言葉が混ざっていても、聞き取りは止めない
                logger.warning("優先語を使えませんでした: %s", exc)
                self._vocab = None
            self._vocab_key = key
        return self._vocab

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = SAMPLE_RATE,
        hotwords: str = "",
        language: Optional[str] = None,
        itn: Optional[bool] = None,
        readings: Optional[dict] = None,
    ) -> Tuple[str, float]:
        """16kHz 以外は直してから聞き取る。戻り値は (文, ミリ秒)。language・itn は使わない（日本語専用）。"""
        if self._model is None:
            self.load()
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples.mean(axis=1).astype(np.float32)
        samples = _resample(samples, sample_rate)
        t0 = time.perf_counter()
        with self._lock:
            import haas_asr

            vocab = self._vocabulary(hotwords, readings)
            if vocab:
                out = haas_asr.transcribe(self._model, [samples], vocabulary=vocab)[0]
            else:
                out = self._model.transcribe([samples], verbose=False)[0]
        text = out.text if hasattr(out, "text") else str(out)
        return fix_acronyms(text.strip()), (time.perf_counter() - t0) * 1000.0
