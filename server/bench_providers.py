"""認識の速さを、計算の担当（CPU / GPU）とスレッド数ごとに測る。

Mac (Apple Silicon) なら GPU は CoreML、Windows + NVIDIA なら CUDA。
例:
  Mac:     .venv_server/bin/python server/bench_providers.py --providers cpu,coreml --threads 4,8
  Windows: .venv\\Scripts\\python.exe server\\bench_providers.py --providers cpu,cuda --threads 4,8

付属の日本語音声 (test_wavs/ja.wav, 8.1秒) と、別々の付属音声をつないだ約 16 秒・約 24 秒の音声で計測し、
各条件の「読み込み時間」と「2回目以降の中央値」と認識結果を表にする。

GPU を指定しても使えないとき、sherpa-onnx は黙って CPU で動く（標準エラーに "Fallback to cpu" と出るだけ）。
そのため条件ごとに別プロセスで動かして出力を調べ、CPU に戻っていたら「使えず」と表示する。
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT_DIR / "models" / "sherpa-onnx-funasr-nano-int8-2025-12-30"
FALLBACK_MARKERS = ("Fallback to cpu", "fallback to cpu", "Fallback to CPU")


def _build_inputs(model_dir: Path):
    import numpy as np

    sys.path.insert(0, str(ROOT_DIR))
    from shared.audio import decode_wav_bytes

    def load(name: str):
        s, sr = decode_wav_bytes((model_dir / "test_wavs" / name).read_bytes())
        return np.asarray(s, dtype=np.float32)

    gap = np.zeros(int(16000 * 0.4), dtype=np.float32)
    ja = load("ja.wav")
    extra = [load(n) for n in ("ja_en_codeswitch.wav", "lyrics.wav")]
    mid = np.concatenate([ja, gap, extra[0]])
    long = np.concatenate([ja, gap, extra[0], gap, extra[1]])
    return [("8秒", ja), (f"{len(mid) / 16000:.0f}秒", mid), (f"{len(long) / 16000:.0f}秒", long)]


def run_single(model_dir: Path, provider: str, threads: int, runs: int) -> dict:
    sys.path.insert(0, str(ROOT_DIR))
    from shared.asr_engine import FunASRNanoEngine

    inputs = _build_inputs(model_dir)
    engine = FunASRNanoEngine(model_dir=model_dir, provider=provider, num_threads=threads)
    t0 = time.perf_counter()
    engine.load()
    out = {"load_ms": (time.perf_counter() - t0) * 1000, "active": engine.active_provider, "results": []}
    for name, s in inputs:
        times, text = [], ""
        for _ in range(runs + 1):
            text, ms = engine.transcribe(samples=s, sample_rate=16000)
            times.append(ms)
        out["results"].append({"name": name, "median_ms": statistics.median(times[1:]), "chars": len(text), "text": text})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default=str(DEFAULT_MODEL))
    ap.add_argument("--providers", default="cpu", help="カンマ区切り。cpu / coreml / cuda")
    ap.add_argument("--threads", default="4", help="カンマ区切り。例 4,8")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--single", nargs=2, metavar=("PROVIDER", "THREADS"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    model_dir = Path(args.model_dir)

    if args.single:
        res = run_single(model_dir, args.single[0], int(args.single[1]), args.runs)
        print("@@RESULT@@" + json.dumps(res, ensure_ascii=False))
        return 0

    rows = []
    for prov in [p.strip() for p in args.providers.split(",") if p.strip()]:
        for th in [int(t) for t in args.threads.split(",") if t.strip()]:
            label = f"{prov} / {th}スレッド"
            print(f"計測中: {label} ...", flush=True)
            proc = subprocess.run(
                [sys.executable, __file__, "--model-dir", str(model_dir), "--runs", str(args.runs),
                 "--single", prov, str(th)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            log = proc.stdout + proc.stderr
            line = next((l for l in proc.stdout.splitlines() if l.startswith("@@RESULT@@")), None)
            if proc.returncode != 0 or line is None:
                rows.append((label, "失敗", log.strip().splitlines()[-1:] or ["?"]))
                continue
            if prov != "cpu" and any(m in log for m in FALLBACK_MARKERS):
                rows.append((label, "使えず（裏で CPU に戻った）", []))
                continue
            res = json.loads(line[len("@@RESULT@@"):])
            truncated = "exceeds KV capacity" in log
            rows.append((label, res, truncated))

    print()
    for label, res, extra in rows:
        if isinstance(res, str):
            print(f"■ {label}: {res} {' '.join(extra) if extra else ''}")
            continue
        parts = [f"{r['name']} {r['median_ms']:.0f}ms({r['chars']}文字)" for r in res["results"]]
        note = "  ※長い音声がモデルの上限を超えて切り詰められました" if extra else ""
        print(f"■ {label}: 読込 {res['load_ms']:.0f}ms | " + " | ".join(parts) + note)
    first = next((r for _, r, _ in rows if isinstance(r, dict)), None)
    if first:
        print(f"\n認識結果(8秒): {first['results'][0]['text']}")
    print("\n見方: GPU の行が CPU の行より明らかに短ければ GPU が効いています。同じくらいなら効いていません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
