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

from client.app_context import strip_chat_trailing_period  # noqa: E402
from shared.acronyms import fix_acronyms  # noqa: E402
from shared.dict_format import clean_data  # noqa: E402
from shared.dictionary import CustomDictionary  # noqa: E402
from shared.fillers import FillerRemover  # noqa: E402
from shared.kana import is_kana_only, kana_variants, to_hiragana, to_katakana  # noqa: E402
from shared.normalize import TextNormalizer  # noqa: E402
from shared.segmenter import join_texts, split_on_pauses  # noqa: E402
from shared.voice_commands import apply_voice_commands, is_undo_command  # noqa: E402

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

    # 段階 2: かな・言いよどみ・標準の変換・音声コマンド・辞書・文末「。」
    filler_cases = [
        "えーと、明日は晴れです。",
        "うん、そういえば、うん。",
        "えーとそういえば、あのー今日は、まあ、いい天気ですね。",
        "あの人はまあまあ元気です。",
        "えーーっと、うん、うん、うん。",
        "えーと、うん。",
        "なんか、それって面白いね。",
        "こんにちは。",
        "",
    ]
    fr = FillerRemover()

    norm_cases = [
        "五千九十円です",
        "二十歳になりました",
        "五万三千円かかる",
        "一億二千万人のデータ",
        "三点五パーセント増えた",
        "四点八と4点八と四点8と5点5",
        "二〇二六年の予定です",
        "バージョン五を使う",
        "五を押してください",
        "一人で行く。一つください。五つある。一緒に行こう。一番好き。二三日かかる。",
        "八百屋で万が一に備えて千葉の百貨店へ。十分です。十分かかります。十分間。",
        "十時に百円持って千円札で払う",
        "ＡＢＣ１２３％とエーアイとピーディーエフとアイデアとコンフィーユーアイ",
        "ギットハブでパイソンとジャバスクリプトとチャットジーピーティー",
    ]
    tn = TextNormalizer()
    tn_off = TextNormalizer(disabled_terms=["エーアイ", "ピーディーエフ"])

    vc_cases = [
        "こんにちは。改行、次の行です。",
        "1番目かいぎょう2番目",
        "テスト カイギョウ テスト2",
        "改行",
        "改行コードを変えて",
        "改行文字を入れる",
        "自動改行をオン",
        "ここで改行する",
        "改行なし",
        "かぎかっここんにちはかぎかっことじ",
        "かっこ開き補足かっこ閉じ",
        "本当にはてなマーク",
        "すごいびっくりマーク",
        "はてなマークを付ける",
    ]
    undo_cases = [
        "今のなし",
        "いまのはなし。",
        "今のを取り消して",
        "取り消し",
        "今の話ではない",
        "普通の文です",
    ]
    period_cases = [
        "了解です。",
        "行1。\n行2。",
        "本当に？",
        "なるほど。。",
        "テスト！",
        "末尾に空白。  ",
    ]

    import tempfile

    dict_obj = {
        "hotwords": ["ComfyUI", "Supabase", "あ", "ひらがなだけ"],
        "replacements": {
            "コンフィー": "Comfy",
            "コンフィーUI": "ComfyUI",
            "すーぱーべーす": "Supabase",
            "あい": "AI",
            "やまださん": "山田さん",
        },
        "snippets": {
            "メールアドレス": "test@example.com",
            "いつもの挨拶": "お世話になっております。\nよろしくお願いします。",
        },
        "builtin_off": ["エーアイ"],
    }
    with tempfile.TemporaryDirectory() as tmp:
        dp = Path(tmp) / "dict.json"
        dp.write_text(json.dumps(dict_obj, ensure_ascii=False), encoding="utf-8")
        dic = CustomDictionary(dp)
        rep_inputs = [
            "コンフィーUIとコンフィーを使う",
            "スーパーベースを使う",
            "アイデアとアイの話",
            "やまださんとヤマダさん",
        ]
        snip_inputs = [
            "メールアドレス",
            "メールアドレス。",
            "めーるあどれす",
            "メールアドレスを教えて",
            "いつもの挨拶。",
        ]
        stage2 = {
            "kana": [[s, to_katakana(s), to_hiragana(s), is_kana_only(s), kana_variants(s)]
                     for s in ["すーぱーべーす", "スーパーベース", "ComfyUI", "あいうえお"]],
            "fillers": [[s, fr.remove(s)] for s in filler_cases],
            "normalize": [[s, tn.normalize(s)] for s in norm_cases],
            "normalize_disabled": [[s, tn_off.normalize(s)] for s in ["エーアイとピーディーエフとジーピーユー"]],
            "voice_commands": [[s, apply_voice_commands(s)] for s in vc_cases],
            "undo_command": [[s, is_undo_command(s)] for s in undo_cases],
            "strip_trailing_period": [[s, strip_chat_trailing_period(s)] for s in period_cases],
            "dictionary": {
                "data": dict_obj,
                "active_hotwords": dic.active_hotwords(),
                "replacements": [[s, dic.apply_replacements(s)] for s in rep_inputs],
                "snippets": [[s, list(dic.apply_snippets(s))] for s in snip_inputs],
                "normalize": [[s, dic.normalize(s)] for s in ["エーアイとピーディーエフ"]],
            },
        }
    (OUT / "stage2.json").write_text(json.dumps(stage2, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print("書き出しました:", OUT)


if __name__ == "__main__":
    main()
