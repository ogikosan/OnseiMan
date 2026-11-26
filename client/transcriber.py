from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import requests

from client.config import ClientConfig
from client.recorder import samples_to_wav_bytes
from shared.asr_engine import FunASRNanoEngine

logger = logging.getLogger("onseiman.transcriber")


def _as_float(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 1.0 <= f <= 120.0 else None

_SPECIAL_TOKEN_RE = re.compile(r"(<\|[^|]+\|>|/sil\b)", re.IGNORECASE)


def clean_asr_text(text: str) -> str:
    """Strip Fun-ASR special tokens such as /sil or <|...|>."""
    if not text:
        return ""
    cleaned = _SPECIAL_TOKEN_RE.sub("", text).strip()
    return cleaned


@dataclass
class TranscriptionResult:
    text: str
    backend: str
    elapsed_ms: float


class HybridTranscriber:
    """Sends audio to LAN GPU server(s) first, with automatic fallback to local CPU sherpa-onnx.

    速さのための工夫:
    - 最後に成功したサーバーを先に使う。失敗したサーバーは DOWN_RETRY_SEC の間は後回しにし、
      落ちたサーバーの接続待ち（1.5 秒）を毎回払わない。定期ヘルスチェックで復帰したら戻す。
    - サーバーとの接続をスレッドごとに使い回す（毎回つなぎ直さない）。
    """

    DOWN_RETRY_SEC = 60.0

    def __init__(self, config: ClientConfig, root_dir: Path) -> None:
        self.config = config
        self.root_dir = root_dir
        self._local_engine: Optional[FunASRNanoEngine] = None
        self._local_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._preferred: str = ""
        self._down_until: dict[str, float] = {}
        self._tls = threading.local()
        self.last_backend_status: str = "待機中"
        # サーバーの URL → {"version", "fingerprint", "name"}（/health で分かったもの）
        self.server_info: dict = {}

    # ------------------------------------------------------------ servers
    def _session(self) -> requests.Session:
        s = getattr(self._tls, "session", None)
        if s is None:
            s = requests.Session()
            self._tls.session = s
        return s

    def uses_servers(self) -> bool:
        place = (getattr(self.config, "recognition_place", "") or "").strip()
        if not place:
            place = "server" if self.config.server_urls else "local"
        return place == "server"

    def _server_list(self) -> list[str]:
        if not self.uses_servers():
            return []  # 「このパソコン」で認識する設定
        seen: list[str] = []
        for u in self.config.server_urls:
            b = (u or "").strip().rstrip("/")
            if b and b not in seen:
                seen.append(b)
        return seen

    def _chosen(self) -> str:
        """利用者が「いつも使う」に選んだサーバー（登録されていなければ空）。"""
        want = (getattr(self.config, "preferred_server", "") or "").strip().rstrip("/")
        return want if want and want in self._server_list() else ""

    def ordered_servers(self) -> list[str]:
        """試す順番: 「いつも使う」サーバー → 最後に成功したサーバー → 他の元気なサーバー（設定順）。
        最近失敗したものは除く。全部が「最近失敗」なら、設定順にすべて試す（ネットワーク復帰直後など）。
        """
        servers = self._server_list()
        now = time.monotonic()
        with self._state_lock:
            alive = [u for u in servers if self._down_until.get(u, 0.0) <= now]
            pref = self._preferred
        for first in (pref, self._chosen()):   # あとに入れたものが先頭になる
            if first in alive:
                alive.remove(first)
                alive.insert(0, first)
        return alive or servers

    def _mark_ok(self, url: str) -> None:
        with self._state_lock:
            self._down_until.pop(url, None)
            self._preferred = url

    def _mark_down(self, url: str) -> None:
        with self._state_lock:
            self._down_until[url] = time.monotonic() + self.DOWN_RETRY_SEC
            if self._preferred == url:
                self._preferred = ""

    def _probe(self, base_url: str) -> Optional[str]:
        try:
            resp = requests.get(
                base_url + "/health", timeout=min(self.config.server_connect_timeout_sec, 1.5)
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    # 認識役の版と指紋（古い版かどうかを知らせるのに使う）
                    self.server_info[base_url] = {
                        "version": str(data.get("version") or "")[:20],
                        "fingerprint": str(data.get("fingerprint") or "")[:64],
                        "name": str(data.get("name") or "")[:64],
                        "id": str(data.get("id") or "")[:64],
                        # 聞き取りの仕組み（funasr = CPU 版、haas = GPU 版）と、一度に送ってよい長さ
                        "engine": str(data.get("engine") or "funasr")[:16],
                        "max_segment_sec": _as_float(data.get("max_segment_sec")),
                    }
                    return str(data.get("provider", "lan"))
        except Exception:
            pass
        return None

    def preferred_server_info(self) -> dict:
        """いま使っている認識役の情報（まだ分からなければ空）。"""
        with self._state_lock:
            pref = self._preferred
        return dict(self.server_info.get(pref) or {}) if pref else {}

    def check_server_health(self) -> Optional[str]:
        """全サーバーを同時に確認し、生死を記録する。使うサーバーの URL（なければ None）を返す。"""
        servers = self._server_list()
        results: dict[str, Optional[str]] = {}
        threads = []
        for u in servers:
            t = threading.Thread(target=lambda u=u: results.__setitem__(u, self._probe(u)), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=3.0)
        for u in servers:
            if results.get(u):
                with self._state_lock:
                    self._down_until.pop(u, None)
            else:
                self._mark_down(u)
        with self._state_lock:
            pref = self._preferred
        want = self._chosen()
        if want and results.get(want):
            chosen = want
        else:
            chosen = pref if pref and results.get(pref) else next((u for u in servers if results.get(u)), None)
        if chosen:
            with self._state_lock:
                self._preferred = chosen
            self.last_backend_status = f"LAN ({results[chosen]})"
            return chosen
        if self.config.local_fallback or not self.uses_servers():
            self.last_backend_status = "Local CPU"
        else:
            self.last_backend_status = "未接続"
        return None

    def preload_local(self) -> None:
        """手元の認識エンジンを先に読み込む（最初の認識で数秒待たないように）。失敗しても続ける。"""
        try:
            with self._local_lock:
                self._get_local_engine()
        except Exception as exc:
            logger.warning("手元の認識エンジンを読み込めませんでした: %s", exc)

    def _get_local_engine(self) -> FunASRNanoEngine:
        if self._local_engine is None:
            model_dir = Path(self.config.local_model_dir)
            if not model_dir.is_absolute():
                model_dir = self.root_dir / model_dir
            self._local_engine = FunASRNanoEngine(
                model_dir=model_dir,
                provider="cpu",
                num_threads=self.config.local_num_threads,
                language=self.config.language,
                itn=self.config.itn,
            )
            self._local_engine.load()
        return self._local_engine

    def _post(self, url: str, wav_bytes: bytes, hotwords_csv: str) -> requests.Response:
        def once() -> requests.Response:
            return self._session().post(
                url,
                files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
                data={
                    "hotwords": hotwords_csv,
                    "language": self.config.language,
                    "itn": "true" if self.config.itn else "false",
                },
                timeout=(
                    self.config.server_connect_timeout_sec,
                    self.config.server_read_timeout_sec,
                ),
            )

        try:
            return once()
        except requests.exceptions.ConnectTimeout:
            raise
        except requests.exceptions.ConnectionError:
            # 使い回した接続がサーバー側で閉じられていただけのことがある。つなぎ直して 1 回だけ再送する。
            self._tls.session = None
            return once()

    def transcribe(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        hotwords_csv: str = "",
    ) -> TranscriptionResult:
        wav_bytes = samples_to_wav_bytes(samples, sample_rate)

        # 1. Try LAN server(s): last good first, recently failed ones skipped
        for base in self.ordered_servers():
            url = f"{base}/transcribe"
            t0 = time.perf_counter()
            try:
                resp = self._post(url, wav_bytes, hotwords_csv)
                resp.raise_for_status()
                payload = resp.json()
                text = clean_asr_text(payload.get("text") or "")
                elapsed = (time.perf_counter() - t0) * 1000.0
                provider = payload.get("provider", "lan")
                self._mark_ok(base)
                self.last_backend_status = f"LAN ({provider})"
                return TranscriptionResult(text=text, backend=f"LAN-{provider}", elapsed_ms=elapsed)
            except Exception as exc:
                self._mark_down(base)
                # 使い回していた接続が壊れている可能性があるので作り直す
                self._tls.session = None
                logger.warning("LAN サーバー %s への送信に失敗（%.0f 秒は後回し）: %s", base, self.DOWN_RETRY_SEC, exc)

        # 2. Fallback to local CPU sherpa-onnx（「このパソコン」の設定なら最初からここ）
        if self.uses_servers() and not self.config.local_fallback:
            raise RuntimeError("LAN サーバーに接続できず、ローカルフォールバックも無効です。")

        logger.info("手元 CPU (sherpa-onnx) へフォールバックして認識します...")
        with self._local_lock:
            engine = self._get_local_engine()
            text, elapsed_ms = engine.transcribe(
            samples=samples,
            sample_rate=sample_rate,
            hotwords=hotwords_csv,
            language=self.config.language,
            itn=self.config.itn,
        )
        text = clean_asr_text(text)
        self.last_backend_status = "Local CPU"
        return TranscriptionResult(text=text, backend="Local-CPU", elapsed_ms=elapsed_ms)
