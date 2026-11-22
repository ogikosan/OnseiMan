from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("onseiman.asr")


def _find_model_file(model_dir: Path, candidates: list[str]) -> Path:
    for name in candidates:
        p = model_dir / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"モデルファイルが見つかりません ({model_dir}): 候補={candidates}"
    )


def _find_tokenizer_dir(model_dir: Path) -> Path:
    for name in ["Qwen3-0.6B", "tokenizer"]:
        p = model_dir / name
        if p.is_dir():
            return p
    # Fallback: check if tokenizer files exist directly in a subdirectory
    for child in model_dir.iterdir():
        if child.is_dir() and (child / "vocab.json").exists():
            return child
    if (model_dir / "vocab.json").exists():
        return model_dir
    raise FileNotFoundError(f"tokenizer ディレクトリが見つかりません: {model_dir}")


class FunASRNanoEngine:
    """Thread-safe wrapper around sherpa-onnx OfflineRecognizer (Fun-ASR Nano Multilingual)."""

    def __init__(
        self,
        model_dir: str | Path,
        provider: str = "cpu",
        num_threads: int = 4,
        language: str = "",
        itn: bool = True,
        debug: bool = False,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.requested_provider = provider
        self.active_provider = provider
        self.num_threads = num_threads
        self.default_language = language
        self.default_itn = itn
        self.debug = debug
        self._lock = threading.Lock()
        self._recognizer = None

    def load(self) -> None:
        with self._lock:
            if self._recognizer is not None:
                return

            try:
                import sherpa_onnx
            except ImportError as exc:
                raise RuntimeError(
                    "sherpa-onnx がインストールされていません。`pip install sherpa-onnx` を実行してください。"
                ) from exc

            if not hasattr(sherpa_onnx.OfflineRecognizer, "from_funasr_nano"):
                raise RuntimeError(
                    f"現在の sherpa-onnx ({getattr(sherpa_onnx, '__version__', 'unknown')}) は "
                    "Fun-ASR Nano に対応していません。最新版へアップデートしてください。"
                )

            if not self.model_dir.exists():
                raise FileNotFoundError(
                    f"モデルディレクトリが存在しません: {self.model_dir}\n"
                    "`python download_model.py` を実行してモデルをダウンロードしてください。"
                )

            encoder_adaptor = _find_model_file(
                self.model_dir, ["encoder_adaptor.int8.onnx", "encoder_adaptor.onnx"]
            )
            llm = _find_model_file(
                self.model_dir, ["llm.int8.onnx", "llm.onnx"]
            )
            embedding = _find_model_file(
                self.model_dir, ["embedding.int8.onnx", "embedding.onnx"]
            )
            tokenizer = _find_tokenizer_dir(self.model_dir)

            providers_to_try = [self.requested_provider]
            if self.requested_provider != "cpu":
                providers_to_try.append("cpu")

            last_err: Optional[Exception] = None
            for prov in providers_to_try:
                try:
                    t0 = time.perf_counter()
                    logger.info(
                        "Fun-ASR Nano モデルを読み込み中... (provider=%s, dir=%s)",
                        prov,
                        self.model_dir,
                    )
                    kwargs = dict(
                        encoder_adaptor=str(encoder_adaptor),
                        llm=str(llm),
                        embedding=str(embedding),
                        tokenizer=str(tokenizer),
                        num_threads=self.num_threads,
                        provider=prov,
                        debug=self.debug,
                    )
                    # Optional kwargs supported by newer sherpa-onnx versions
                    try:
                        self._recognizer = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
                            **kwargs,
                            language=self.default_language,
                            itn=self.default_itn,
                        )
                    except TypeError:
                        self._recognizer = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
                            **kwargs
                        )

                    self.active_provider = prov
                    elapsed = (time.perf_counter() - t0) * 1000
                    logger.info(
                        "Fun-ASR Nano 読み込み完了 (provider=%s, %.0f ms)",
                        prov,
                        elapsed,
                    )
                    return
                except Exception as exc:
                    last_err = exc
                    logger.warning(
                        "provider=%s での読み込みに失敗しました: %s", prov, exc
                    )

            raise RuntimeError(f"Fun-ASR Nano モデルの読み込みに失敗しました: {last_err}")

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        hotwords: str = "",
        language: Optional[str] = None,
        itn: Optional[bool] = None,
    ) -> Tuple[str, float]:
        """Transcribe float32 mono audio samples. Returns (text, elapsed_ms)."""
        if self._recognizer is None:
            self.load()

        if samples.dtype != np.float32:
            samples = samples.astype(np.float32)
        if samples.ndim > 1:
            samples = samples.mean(axis=1).astype(np.float32)

        t0 = time.perf_counter()
        with self._lock:
            stream = self._recognizer.create_stream()

            # Per-stream dynamic options (supported in sherpa-onnx with FunASR-Nano hotword PR)
            if hasattr(stream, "set_option"):
                if hotwords:
                    try:
                        stream.set_option("hotwords", hotwords)
                    except Exception as exc:
                        logger.debug("stream.set_option(hotwords) skipped: %s", exc)
                lang_val = self.default_language if language is None else language
                if lang_val:
                    try:
                        stream.set_option("language", lang_val)
                    except Exception as exc:
                        logger.debug("stream.set_option(language) skipped: %s", exc)
                itn_val = self.default_itn if itn is None else itn
                try:
                    stream.set_option("itn", "1" if itn_val else "0")
                except Exception as exc:
                    logger.debug("stream.set_option(itn) skipped: %s", exc)

            stream.accept_waveform(sample_rate, samples)
            self._recognizer.decode_stream(stream)
            text = (stream.result.text or "").strip()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return text, elapsed_ms
