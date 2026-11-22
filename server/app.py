"""OnseiMan LAN 音声認識 API サーバー (Fun-ASR Nano Multilingual / sherpa-onnx)."""
from __future__ import annotations

import argparse
import socket
import uuid
import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
from starlette.concurrency import run_in_threadpool

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared.asr_engine import FunASRNanoEngine
from shared.audio import decode_wav_bytes
from shared.segmenter import join_texts, split_on_pauses

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("onseiman.server")

# 説明用のページ（/docs など）は出さない
app = FastAPI(title="OnseiMan ASR Server", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
engine: Optional[FunASRNanoEngine] = None


@app.get("/health")
def health() -> dict:
    if engine is None:
        return {"status": "initializing"}
    return {
        "status": "ok",
        "model": engine.model_dir.name,
        "provider": engine.active_provider,
        # さがす側が「同じパソコンが 2 つの行き方で見つかった」と分かるようにする
        "id": SERVER_ID,
        "name": SERVER_NAME,
    }


def local_addresses() -> list:
    """このパソコンのアドレス（つなぐ側に手で書くとき用）。(種類, IP) の一覧。"""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254.") or any(ip == o[1] for o in out):
                continue
            out.append(("Tailscale" if ip.startswith("100.") else "家のネットワーク", ip))
    except Exception:
        pass
    return out


SERVER_ID = uuid.uuid4().hex          # 起動ごとの番号
SERVER_NAME = socket.gethostname()

MAX_AUDIO_BYTES = 30 * 1024 * 1024
MAX_HOTWORDS_CHARS = 4000
MAX_AUDIO_SEC = 300.0  # OnseiMan は 18 秒以内に区切って送る
CONVERT_TIMEOUT_SEC = 15.0


def segment_max_sec(hotwords: str) -> float:
    """一度に認識に渡す長さ。認識データは一度に扱える量に上限があり（約 30 秒を超えると結果が空になる）、
    優先語が多いほど短くなる。アプリ（client/main.py の _max_segment_sec）と同じ決め方。"""
    n = len([h for h in hotwords.split(",") if h.strip()])
    return max(6.0, min(18.0, 25.0 - 0.4 * n))


def is_m4a(raw: bytes) -> bool:
    """M4A（iPhone のショートカットで録音した音声など）。先頭 4〜8 バイトが ftyp。"""
    return len(raw) >= 12 and raw[4:8] == b"ftyp"


def m4a_command(src: str, dst: str) -> Optional[list]:
    """M4A を 16kHz・モノラルの WAV にする命令。Mac 標準の afconvert、無ければ ffmpeg。どちらも無ければ None。"""
    af = shutil.which("afconvert")
    if af:
        return [af, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, dst]
    ff = shutil.which("ffmpeg")
    if ff:
        # 外からのデータなので、読むのは手元のファイルだけ・mp4 の形として読むよう決めておく
        return [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
                "-f", "mov", "-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", "-t", str(int(MAX_AUDIO_SEC) + 1), "-f", "wav", "-y", dst]
    return None


def convert_m4a(raw: bytes) -> bytes:
    """M4A を WAV にして返す。変換中も大きさを見て、上限を超えたら止める（小さな M4A が巨大な WAV になる対策）。"""
    with tempfile.TemporaryDirectory(prefix="onseiman-") as tmp:
        src, dst = os.path.join(tmp, "in.m4a"), os.path.join(tmp, "out.wav")
        with open(src, "wb") as f:
            f.write(raw)
        cmd = m4a_command(src, dst)
        if cmd is None:
            raise HTTPException(status_code=415, detail="M4A is not supported on this server")
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + CONVERT_TIMEOUT_SEC
        try:
            while proc.poll() is None:
                if os.path.exists(dst) and os.path.getsize(dst) > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="Audio too long")
                if time.monotonic() > deadline:
                    raise HTTPException(status_code=413, detail="Audio conversion took too long")
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode != 0 or not os.path.exists(dst):
            raise HTTPException(status_code=400, detail="Invalid M4A audio")
        if os.path.getsize(dst) > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="Audio too long")
        with open(dst, "rb") as f:
            return f.read()


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

    # WAV と M4A だけを受け付ける（ほかの形式の読み取り処理に、外からのデータを渡さない）
    if is_m4a(raw_bytes):
        raw_bytes = await run_in_threadpool(convert_m4a, raw_bytes)
    if len(raw_bytes) < 44 or raw_bytes[:4] != b"RIFF" or raw_bytes[8:12] != b"WAVE":
        raise HTTPException(status_code=400, detail="Only WAV or M4A audio is accepted")
    try:
        samples, sr = decode_wav_bytes(raw_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid WAV audio") from None
    if not (8000 <= sr <= 48000):
        raise HTTPException(status_code=400, detail="Unsupported sample rate")
    if samples.size / float(sr) > MAX_AUDIO_SEC:
        raise HTTPException(status_code=413, detail="Audio too long")
    language = language[:16]

    if samples.size == 0:
        return {"text": "", "elapsed_ms": 0.0, "provider": engine.active_provider}

    # OnseiMan のアプリは区切って送る。iPhone のショートカットなどから長い音声が来たら、ここで話の切れ目で区切る
    max_sec = segment_max_sec(hotwords)
    if samples.size / float(sr) > max_sec:
        parts = split_on_pauses(samples, sr, max_segment_sec=max_sec, min_segment_sec=min(4.0, max_sec * 0.5))
    else:
        parts = [samples]
    texts, elapsed_ms = [], 0.0
    for part in parts:
        t, ms = engine.transcribe(samples=part, sample_rate=sr, hotwords=hotwords, language=language, itn=itn)
        texts.append(t)
        elapsed_ms += ms
    text = join_texts(texts)
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
    print()
    print("OnseiMan のサーバーが動いています。この窓は開いたままにしてください。")
    print("つなぐ側は、設定の「別のパソコン」→「サーバーをさがす」で自動で見つかります。")
    for kind, ip in local_addresses():
        print(f"  見つからないときに手で書くアドレス（{kind}）: http://{ip}:{args.port}")
    print(flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
