"""OnseiMan の認識役（ほかのパソコンから届いた声を文字にして返す）。

聞き取りは Haas-ja を speech.cpp で動かす（Python も CUDA も要らない。Mac は Apple の GPU、Windows は CPU）。
部品は shared/speechcpp_setup.py が用意する（server_mac.command / server_windows.bat の準備の手順から）。
"""
from __future__ import annotations

import argparse
import socket
import uuid
import io
import logging
import os
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared import speechcpp_setup
from shared.audio import decode_wav_bytes
from shared.dict_format import NAME_RE, clean_data
from shared.segmenter import join_texts, split_on_pauses
from shared.server_fingerprint import fingerprint, read_version
from shared.speechcpp_engine import MAX_SEGMENT_SEC, SpeechCppEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("onseiman.server")

# 説明用のページ（/docs など）は出さない
app = FastAPI(title="OnseiMan ASR Server", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
engine = None  # SpeechCppEngine（main で読み込む）


@app.get("/health")
def health() -> dict:
    if engine is None:
        return {"status": "initializing"}
    return {
        "status": "ok",
        "model": engine.model_name,
        "provider": engine.active_provider,
        "engine": "haas",
        # つなぐ側が、一度に送る長さを決めるのに使う
        "max_segment_sec": MAX_SEGMENT_SEC,
        # さがす側が「同じパソコンが 2 つの行き方で見つかった」と分かるようにする
        "id": SERVER_ID,
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "fingerprint": SERVER_FINGERPRINT,
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
# 自分の版と、プログラムの指紋（Windows のアプリが「認識役が古いか」を確かめるのに使う）
SERVER_VERSION = read_version(ROOT_DIR)
SERVER_FINGERPRINT = fingerprint(ROOT_DIR)

MAX_AUDIO_BYTES = 30 * 1024 * 1024
MAX_HOTWORDS_CHARS = 4000
MAX_AUDIO_SEC = 300.0  # OnseiMan は区切って送る（iPhone のショートカットなどからは長いものも来る）
CONVERT_TIMEOUT_SEC = 15.0


def parse_readings(raw: str) -> dict:
    """優先語の読み（JSON の {書き方: [読み, ...]}）。おかしなものや大きすぎるものは使わない。"""
    if not raw or len(raw) > MAX_HOTWORDS_CHARS * 2:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in list(data.items())[:200]:
        if isinstance(k, str) and isinstance(v, list) and 0 < len(k) <= 60:
            rs = [r for r in v if isinstance(r, str) and 0 < len(r) <= 60][:5]
            if rs:
                out[k] = rs
    return out


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
    keywords: str = Form(default=""),
    # 前の版のアプリが送ってくる（Haas-ja は日本語専用なので使わない）
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
    readings = parse_readings(keywords)

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

    if samples.size == 0:
        return {"text": "", "elapsed_ms": 0.0, "provider": engine.active_provider}

    # OnseiMan のアプリは区切って送る。iPhone のショートカットなどから長い音声が来たら、ここで話の切れ目で区切る
    max_sec = MAX_SEGMENT_SEC
    if samples.size / float(sr) > max_sec:
        parts = split_on_pauses(samples, sr, max_segment_sec=max_sec, min_segment_sec=min(4.0, max_sec * 0.5))
    else:
        parts = [samples]
    texts, elapsed_ms = [], 0.0
    for part in parts:
        t, ms = engine.transcribe(samples=part, sample_rate=sr, hotwords=hotwords, readings=readings)
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


# ---- 辞書を預かる（各パソコンの OnseiMan が、ここを通して辞書を合わせる） ----
# 預かった辞書は server_data/dicts/<名前>.json。利用者のデータなので、更新しても消さない。
DICT_DIR = Path(os.environ.get("ONSEIMAN_DATA_DIR") or (ROOT_DIR / "server_data")) / "dicts"
MAX_DICT_BYTES = 2 * 1024 * 1024
_dict_lock = threading.Lock()


def _dict_path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid dictionary name")
    return DICT_DIR / f"{name}.json"


def _read_dict(path: Path) -> dict:
    if not path.exists():
        return {"store": "", "version": 0, "data": {}}
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
        return {"store": str(j.get("store", "")), "version": int(j.get("version", 0)), "data": j.get("data") or {}}
    except Exception:
        logger.exception("預かった辞書を読めませんでした: %s", path.name)
        raise HTTPException(status_code=500, detail="Stored dictionary is broken") from None


@app.get("/dict/{name}")
def get_dict(name: str) -> dict:
    path = _dict_path(name)
    with _dict_lock:
        return _read_dict(path)


@app.put("/dict/{name}")
async def put_dict(name: str, request: Request) -> dict:
    path = _dict_path(name)
    raw = await request.body()
    if len(raw) > MAX_DICT_BYTES:
        raise HTTPException(status_code=413, detail="Dictionary too large")
    try:
        body = json.loads(raw)
        version = int(body["version"])
        data = body["data"]
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid dictionary") from None
    data = clean_data(data)
    with _dict_lock:
        cur = _read_dict(path)
        if cur["version"] != version:
            # 読んだあとに、ほかのパソコンが書き換えた。読み直して合わせ直してもらう
            raise HTTPException(status_code=409, detail="Dictionary was changed by another computer")
        store = cur["store"] or uuid.uuid4().hex
        new = {"store": store, "version": version + 1, "data": data}
        DICT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    # 中身はログに残さない（件数だけ）
    logger.info("辞書を預かりました: %s 版 %d（%d 件）", name, version + 1,
                sum(len(v) for v in data.values() if isinstance(v, (list, dict))))
    return {"store": store, "version": version + 1}


def main() -> None:
    global engine

    parser = argparse.ArgumentParser(description="OnseiMan の認識役")
    parser.add_argument("--host", default=os.environ.get("ONSEIMAN_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ONSEIMAN_PORT", "8765")))
    parser.add_argument("--threads", type=int, default=int(os.environ.get("ONSEIMAN_THREADS", "4")))
    parser.add_argument("--device", default=os.environ.get("ONSEIMAN_DEVICE", ""),
                        help="speech.cpp を動かす場所（gpu / cpu。空ならおまかせ: Mac は GPU、Windows は CPU）")
    # 前の版の起動の登録（Mac の launchd など）が付けている。受け取って使わない
    parser.add_argument("--provider", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not speechcpp_setup.ready(ROOT_DIR):
        print("聞き取りの部品がありません。server_mac.command（Mac）か server_windows.bat（Windows）で準備してください。", flush=True)
        sys.exit(1)
    engine = SpeechCppEngine(ROOT_DIR, device=args.device, threads=args.threads)
    engine.load()

    logger.info("サーバー起動: http://%s:%d (%s)", args.host, args.port, engine.active_provider)
    print()
    print("OnseiMan のサーバーが動いています。この窓は開いたままにしてください。")
    print("つなぐ側は、設定の「別のパソコン」→「サーバーをさがす」で自動で見つかります。")
    for kind, ip in local_addresses():
        print(f"  見つからないときに手で書くアドレス（{kind}）: http://{ip}:{args.port}")
    print(flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
