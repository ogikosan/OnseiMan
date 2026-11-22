"""OnseiMan LAN 音声認識 API サーバー (Fun-ASR Nano Multilingual / sherpa-onnx)."""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import wave
from pathlib import Path
from typing import Optional

# Ensure pip-installed NVIDIA CUDA 12 / cuDNN 9 DLLs inside venv are discoverable before sherpa_onnx loads
def _register_nvidia_dll_dirs() -> None:
    if os.name != "nt":
        return
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return
    added: list[str] = []
    for bin_dir in sorted(base.glob("*/bin")):
        if bin_dir.is_dir():
            p = str(bin_dir)
            try:
                os.add_dll_directory(p)
            except Exception:
                pass
            added.append(p)
    if added:
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")


_register_nvidia_dll_dirs()

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.asr_engine import FunASRNanoEngine
from shared.audio import decode_wav_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("onseiman.server")

app = FastAPI(title="OnseiMan ASR Server", version="1.0.0")
engine: Optional[FunASRNanoEngine] = None


@app.get("/health")
def health() -> dict:
    if engine is None:
        return {"status": "initializing"}
    return {
        "status": "ok",
        "model": engine.model_dir.name,
        "provider": engine.active_provider,
    }


MAX_AUDIO_BYTES = 30 * 1024 * 1024
MAX_HOTWORDS_CHARS = 4000


@app.middleware("http")
async def _limit_body(request, call_next):
    """受け取る前に大きさを見て、大きすぎる送信は読み込まずに断る。"""
    from fastapi.responses import JSONResponse

    try:
        size = int(request.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size > MAX_AUDIO_BYTES + 64 * 1024:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    return await call_next(request)


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    hotwords: str = Form(default=""),
    language: str = Form(default=""),
    itn: bool = Form(default=True),
) -> dict:
    if engine is None:
        raise HTTPException(status_code=503, detail="ASR engine is not initialized")

    # 大きすぎる送信は断る（16kHz・16bit なら 30MB で約 15 分。OnseiMan は区切って送るので十分）
    raw_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")
    if len(raw_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")
    if len(hotwords) > MAX_HOTWORDS_CHARS:
        hotwords = hotwords[:MAX_HOTWORDS_CHARS]

    try:
        samples, sr = decode_wav_bytes(raw_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid WAV audio: {exc}") from exc

    if samples.size == 0:
        return {"text": "", "elapsed_ms": 0.0, "provider": engine.active_provider}

    text, elapsed_ms = engine.transcribe(
        samples=samples,
        sample_rate=sr,
        hotwords=hotwords,
        language=language,
        itn=itn,
    )
    # 話した内容はログに残さない（文字数だけ）
    logger.info(
        "認識完了 (%.0f ms, provider=%s, %.2fs audio, %d文字)",
        elapsed_ms,
        engine.active_provider,
        len(samples) / max(sr, 1),
        len(text),
    )
    return {
        "text": text,
        "elapsed_ms": round(elapsed_ms, 1),
        "provider": engine.active_provider,
    }


def main() -> None:
    global engine

    parser = argparse.ArgumentParser(description="OnseiMan LAN 音声認識サーバー")
    parser.add_argument("--host", default=os.environ.get("ONSEIMAN_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ONSEIMAN_PORT", "8765")))
    parser.add_argument(
        "--model-dir",
        default=os.environ.get(
            "ONSEIMAN_MODEL_DIR",
            str(ROOT_DIR / "models" / "sherpa-onnx-funasr-nano-int8-2025-12-30"),
        ),
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("ONSEIMAN_PROVIDER", "cuda"),
        choices=["cuda", "cpu"],
        help="推論プロバイダ (GPU PC では cuda、未対応時は自動で cpu にフォールバック)",
    )
    parser.add_argument("--threads", type=int, default=int(os.environ.get("ONSEIMAN_THREADS", "4")))
    args = parser.parse_args()

    engine = FunASRNanoEngine(
        model_dir=args.model_dir,
        provider=args.provider,
        num_threads=args.threads,
    )
    engine.load()

    logger.info("サーバー起動: http://%s:%d (provider=%s)", args.host, args.port, engine.active_provider)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
