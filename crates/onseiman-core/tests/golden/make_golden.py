"""Rust 版が Python 版と同じ答えを出すかを比べるための「正解」を書き出す。

作り方（OnseiMan のフォルダで）: .venv\Scripts\python.exe crates\onseiman-core\tests\golden\make_golden.py
できるもの: このフォルダの *.json（Rust のテスト tests/golden.rs が読む）

音は、どちらの言語でも同じ数になるよう、整数の計算だけで作る（sin などは言語で細かい値が変わるため）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from shared.acronyms import fix_acronyms  # noqa: E402
from shared.dict_format import clean_data  # noqa: E402
from shared.segmenter import join_texts, split_on_pauses  # noqa: E402

OUT = Path(__file__).resolve().parent


def noise(n: int, seed: int, amp_milli: int) -> np.ndarray:
    """整数だけで作る雑音（線形合同法）。値は -amp〜amp（amp = amp_milli / 1000）。"""
    out = np.empty(n, dtype=np.float32)
    x = seed & 0x7FFFFFFF
    for i in range(n):
        x = (x * 1103515245 + 12345) & 0x7FFFFFFF
        out[i] = np.float32(((x >> 8) % 2001 - 1000) * amp_milli) / np.float32(1_000_000)
    return out


def build(plan: list, sr: int) -> np.ndarray:
    """plan: [[秒(ミリ秒), 大きさ(1/1000), 種], ...] をつないだ音"""
    return np.concatenate([noise(ms * sr // 1000, seed, amp) for ms, amp, seed in plan])


SEGMENT_CASES = [
    # 名前, 1 秒あたりの数, 音の並び, 区切り方
    ("pauses", 16000, [[9000, 300, 1], [1000, 0, 2], [9000, 300, 3], [1000, 0, 4], [5000, 300, 5]],
     {"max_segment_sec": 15.0, "min_segment_sec": 4.0}),
    ("no_pause_forced_cut", 16000, [[20000, 300, 6], [300, 3, 7], [20000, 300, 8]],
     {"max_segment_sec": 15.0, "min_segment_sec": 4.0}),
    ("quiet_tail_dropped", 16000, [[6000, 300, 9], [8000, 1, 10]], {"max_segment_sec": 15.0, "min_segment_sec": 4.0}),
    ("short_pauses_kept_together", 16000, [[2000, 300, 11], [300, 0, 12], [2000, 300, 13], [300, 0, 14], [2000, 300, 15]],
     {"max_segment_sec": 15.0, "min_segment_sec": 4.0}),
    ("other_rate_and_options", 44100, [[7000, 200, 16], [900, 5, 17], [7000, 200, 18]],
     {"max_segment_sec": 10.0, "min_segment_sec": 3.0, "pause_sec": 0.6, "rms_threshold": 0.02, "search_back_sec": 4.0}),
    ("defaults", 8000, [[19000, 250, 19], [700, 0, 20], [3000, 250, 21]], {}),
]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    seg = []
    for name, sr, plan, opts in SEGMENT_CASES:
        parts = split_on_pauses(build(plan, sr), sr, **opts)
        seg.append({"name": name, "sample_rate": sr, "plan": plan, "options": opts, "lengths": [int(len(p)) for p in parts]})
    (OUT / "segmenter.json").write_text(json.dumps(seg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")

    texts = ["cpuとgpuの違い", "llm を api で呼ぶ", "apple okay pcb", "rtx5090 と vram 24gb", "Ok CPU Api", "", "pc。os、ui/ux（ai）",
             "これはidですか、pinですか", "html css json sql aws mcp dgx", "tvとvrとarとps5"]
    joins = [["こんにちは。", " 元気ですか"], ["hello", "world"], ["RTX", "5090", "です"], ["", "  ", "a"], ["あ", "b", "c", "い"], []]
    dicts = [
        {"hotwords": [" くっきり版 ", "", "くっきり版", 5090, "RTX", None, True, 1.5], "replacements": {" クッキリバン ": "くっきり版", "  ": "x", "a": 1},
         "snippets": "おかしな形", "ほかの項目": 1},
        {}, {"fillers": None, "builtin_off": ["a", "a", " b"], "snippets": {"じゅうしょ": "東京都\n千代田区"}},
        {"hotwords": ["あ" * 300], "fillers": "x", "replacements": {"い" * 300: "う" * 20000}},
        [1, 2], "文字",
    ]
    other = {
        "acronyms": [[t, fix_acronyms(t)] for t in texts],
        "join_texts": [[j, join_texts(j)] for j in joins],
        "clean_data": [[d, clean_data(d)] for d in dicts],
        "clean_data_with_default_fillers": [[d, clean_data(d, ["えーと", "あのー"])] for d in dicts[:3]],
    }
    (OUT / "text.json").write_text(json.dumps(other, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("書き出しました:", OUT)


if __name__ == "__main__":
    main()
