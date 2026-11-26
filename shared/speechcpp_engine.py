"""speech.cpp の worker で Haas-ja を動かす（Python も CUDA も要らない）。

`speech worker <モデル>` を 1 つ起動して、AI を読み込んだまま待たせ、JSON Lines でやりとりする
（https://github.com/nyosegawa/speech.cpp/blob/main/docs/worker.md 、protocol 4）。
アプリの「このパソコン」の聞き取りと、認識役（server/app.py）の両方で使う。

- Haas は「1 回に 1 つの発話」を渡す作り（いくつもの文が入った長い録音だと、文がまるごと抜けることがある）。
  そのため、一度に渡す長さは MAX_SEGMENT_SEC まで（OnseiMan はそれより短く区切って渡す）。
- 辞書の優先語は keywords（1 行に 1 語）で渡す。英字の略語は大文字に直す（cpu → CPU）。
- worker が落ちたら、次の聞き取りのときに起動し直す。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from shared.acronyms import fix_acronyms
from shared.speechcpp_setup import find_exe, model_path

logger = logging.getLogger("onseiman.speechcpp")

MAX_SEGMENT_SEC = 15.0
CREATE_NO_WINDOW = 0x08000000
START_TIMEOUT_SEC = 180.0
REQUEST_TIMEOUT_SEC = 60.0


def default_device() -> str:
    # Mac は Apple の GPU（Metal）が速くて正確。Windows は CPU（内蔵 GPU では文の終わりが欠けることがあった）
    return "gpu" if sys.platform == "darwin" else "cpu"


def keyword_lines(hotwords: str, readings: Optional[dict] = None) -> list:
    """speech.cpp の keywords（1 行に 1 語。「書き方=読み,読み」）にする。"""
    readings = readings or {}
    out = []
    for w in (x.strip() for x in hotwords.split(",")):
        if not w or "=" in w or "\n" in w:
            continue
        rs = [r.strip() for r in readings.get(w, []) if r and r.strip() and "," not in r and "=" not in r and "\n" not in r]
        out.append(f"{w}={','.join(rs)}" if rs else w)
    return out


class SpeechCppEngine:
    def __init__(self, root: Path, device: str = "", threads: int = 0, command: Optional[list] = None) -> None:
        self.root = Path(root)
        self._command = command   # テスト用: speech.cpp の代わりに動かすもの
        self.device = device or default_device()
        self.threads = threads
        self.active_provider = f"speech.cpp-{self.device}"
        self.model_name = "Haas-ja-v1.0"
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._seq = 0

    # ---------- worker ----------
    def _reader(self, proc: subprocess.Popen, q: "queue.Queue[Optional[dict]]") -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line))
                except ValueError:
                    logger.warning("speech.cpp から読めない行が来ました: %s", line[:200])
        finally:
            q.put(None)   # 終わった印

    def _start(self) -> None:
        if self._command:
            cmd = list(self._command)
        else:
            exe = find_exe(self.root)
            model = model_path(self.root)
            if exe is None or not model.is_file():
                raise RuntimeError("speech.cpp か Haas-ja のデータがありません")
            cmd = [str(exe), "worker", str(model), "--device", self.device]
            if self.threads > 0:
                cmd += ["--threads", str(self.threads)]
        env = dict(os.environ, SPEECH_NO_UPDATE_CHECK="1")
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        q: "queue.Queue[Optional[dict]]" = queue.Queue()
        threading.Thread(target=self._reader, args=(proc, q), daemon=True, name="speechcpp-reader").start()
        try:
            msg = q.get(timeout=START_TIMEOUT_SEC)
        except queue.Empty:
            proc.kill()
            raise RuntimeError("speech.cpp の起動が終わりませんでした")
        if not msg or msg.get("type") != "ready":
            proc.kill()
            err = (msg or {}).get("error") or {}
            raise RuntimeError(f"speech.cpp を起動できませんでした: {err.get('message') or msg}")
        self._proc, self._lines = proc, q
        logger.info("speech.cpp の Haas-ja を読み込みました (%.1f 秒, %s)", time.perf_counter() - t0, self.device)

    def load(self) -> None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()  # type: ignore[union-attr]
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
                self._proc = None

    def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    # ---------- 聞き取り ----------
    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        hotwords: str = "",
        language: Optional[str] = None,
        itn: Optional[bool] = None,
        readings: Optional[dict] = None,
    ) -> Tuple[str, float]:
        """戻り値は (文, ミリ秒)。language・itn は使わない（日本語専用）。
        readings: {書き方: [読み, ...]}（優先語の読み。聞き取る段階でその言葉にしやすくなる）。"""
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim > 1:
            samples = samples.mean(axis=1)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
        words = keyword_lines(hotwords, readings)
        t0 = time.perf_counter()
        with self._lock:
            for attempt in range(3):
                if self._proc is None or self._proc.poll() is not None:
                    self._start()
                self._seq += 1
                rid = f"r{self._seq}"
                try:
                    result = self._request(rid, pcm, int(sample_rate), words)
                except (_WorkerDied, OSError, queue.Empty) as exc:
                    # worker が落ちた・返事がない: 止めて、1 回だけ起動し直してやり直す
                    logger.warning("speech.cpp をやり直します: %s", exc)
                    self._kill()
                    if attempt == 0:
                        continue
                    raise RuntimeError("speech.cpp で聞き取れませんでした") from exc
                if result is None:   # 使えない優先語が混ざっていた。優先語なしでもう一度
                    words = []
                    continue
                return fix_acronyms(result), (time.perf_counter() - t0) * 1000.0
            raise RuntimeError("speech.cpp で聞き取れませんでした")

    def _kill(self) -> None:
        try:
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass
        self._proc = None

    def _request(self, rid: str, pcm: bytes, sample_rate: int, words: list) -> Optional[str]:
        """1 回分を送って、文を返す。優先語が原因で断られたら None。"""
        step = 2 * 16000 * 4   # 4 秒ずつ送る
        for i, off in enumerate(range(0, max(len(pcm), 1), step)):
            self._send({"type": "chunk", "id": rid, "seq": i, "pcm": base64.b64encode(pcm[off:off + step]).decode("ascii")})
        req = {"type": "transcribe", "id": rid, "sample_rate": sample_rate}
        if words:
            req["keywords"] = "\n".join(words)
        self._send(req)
        self._proc.stdin.flush()  # type: ignore[union-attr]
        deadline = time.monotonic() + REQUEST_TIMEOUT_SEC
        while True:
            msg = self._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            if msg is None:
                raise _WorkerDied("speech.cpp が止まりました")
            if msg.get("id") != rid or msg.get("type") == "progress":
                continue   # ほかの返事・進み具合
            if msg.get("type") == "end":
                return str(msg.get("text") or "").strip()
            err = msg.get("error") or {}
            if words and err.get("option") == "keywords":
                logger.warning("優先語を使えませんでした: %s", err.get("message"))
                return None
            raise RuntimeError(f"speech.cpp: {err.get('message') or msg.get('type')}")


class _WorkerDied(Exception):
    pass
