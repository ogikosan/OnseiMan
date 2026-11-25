"""日常会話の認識結果を、書き言葉として読みやすい形に整える（標準の変換）。

- 漢数字 → 算用数字: 五千九十→5090、二十歳→20歳、五万三千円→5万3000円、三点五パーセント→3.5%
  ただし言い回しとして定着しているもの（一人・一度・十分(じゅうぶん)・八百屋・五十嵐・千葉・万が一、
  「二三日」のような並び）は変えない。一桁だけの漢数字（三人・一つ）も変えない。
- 全角英数字 → 半角
- よく出るカタカナ略語 → 英字（エーアイ→AI、ピーディーエフ→PDF など）。
  カタカナの一続きの語として出てきたときだけ置き換える（「アイ」が「アイデア」の中で化けない）。
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

# ---------------------------------------------------------------- カタカナ略語
BUILTIN_TERMS: Dict[str, str] = {
    "エーアイ": "AI",
    "ピーシー": "PC",
    "シーピーユー": "CPU",
    "ジーピーユー": "GPU",
    "アールティーエックス": "RTX",
    "ブイラム": "VRAM",
    "エスエスディー": "SSD",
    "エイチディーディー": "HDD",
    "ユーエスビー": "USB",
    "エイチディーエムアイ": "HDMI",
    "ブルートゥース": "Bluetooth",
    "ワイファイ": "Wi-Fi",
    "ピーディーエフ": "PDF",
    "ユーアールエル": "URL",
    "エーピーアイ": "API",
    "エスキューエル": "SQL",
    "ユーアイ": "UI",
    "ユーエックス": "UX",
    "アイディー": "ID",
    "ディーエム": "DM",
    "エスエヌエス": "SNS",
    "エルエルエム": "LLM",
    "ブイアール": "VR",
    "オーケー": "OK",
    "エヌジー": "NG",
    "ディーブイディー": "DVD",
    "アイフォン": "iPhone",
    "アイフォーン": "iPhone",
    "アイパッド": "iPad",
    "ウィンドウズ": "Windows",
    "ユーチューブ": "YouTube",
    "ディスコード": "Discord",
    "スラック": "Slack",
    "ノーション": "Notion",
    "グーグル": "Google",
    "ジーメール": "Gmail",
    "アマゾン": "Amazon",
    "ギットハブ": "GitHub",
    "ティックトック": "TikTok",
    "エクセル": "Excel",
    "パイソン": "Python",
    "ジャバスクリプト": "JavaScript",
    "チャットジーピーティー": "ChatGPT",
    "ジーピーティー": "GPT",
    "ジェミニ": "Gemini",
    "クロード": "Claude",
    "コンフィーユーアイ": "ComfyUI",
}

_KATA = "\\u30A1-\\u30FA\\u30FC"

# ---------------------------------------------------------------- 漢数字
_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_SMALL = {"十": 10, "百": 100, "千": 1000}
_BIG = {"万": 10**4, "億": 10**8, "兆": 10**12}
_NUM_CHARS = "".join(_DIGITS) + "".join(_SMALL) + "".join(_BIG)
_RUN_RE = re.compile(f"[{_NUM_CHARS}]+")

# 数字の後ろに来たら「数として使っている」とみなす語（一字の十・百・千を変換してよいか判定）
_COUNTERS = (
    "円", "人", "個", "回", "年", "歳", "才", "枚", "本", "件", "度", "時", "分", "秒", "日", "月", "週",
    "点", "位", "台", "冊", "匹", "頭", "杯", "階", "号", "倍", "割", "%", "％",
    "パーセント", "キロ", "メートル", "グラム", "センチ", "ミリ", "ドル", "ページ", "万", "億",
)
# 慣用表現・固有名詞（この中の漢数字は変えない）
_KEEP_WORDS = (
    "八百屋", "八百長", "嘘八百", "五十嵐", "十八番", "百人一首", "千葉", "千代田", "百貨店", "百科",
    "万引き", "万が一", "万一", "千差万別", "一石二鳥", "十人十色", "百聞", "百発百中", "九十九里",
    "三十路", "四十肩", "五十肩", "十字", "十二支", "一千一秒", "一点一点",
    "一か八か", "九九", "一から十まで", "一も二もなく",
)
# 漢字（前後がこれなら、漢数字は言葉の一部: 一緒・一番・十分・二人・第五 など）
_KANJI = "\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF々〆"
# 1 文字の漢数字のあとにこれが来たら、数え方の言葉なのでそのまま（一つ・五つ）
_NATIVE_AFTER = ("つ",)
_NUM_BEFORE = f"[{_NUM_CHARS}0-9]"


def _kanji_to_int(run: str) -> int | None:
    if not any(c in _SMALL or c in _BIG for c in run):
        # 位取りなし（二〇二六）: 各桁を並べる
        return int("".join(str(_DIGITS[c]) for c in run))
    total, section, cur = 0, 0, None
    for c in run:
        if c in _DIGITS:
            if cur is not None:
                return None  # 「二五十」のような並びは数として扱わない
            cur = _DIGITS[c]
        elif c in _SMALL:
            section += (1 if cur is None else cur) * _SMALL[c]
            cur = None
        else:  # 万・億・兆
            section += 0 if cur is None else cur
            if section == 0:
                return None
            total += section * _BIG[c]
            section, cur = 0, None
    return total + section + (cur or 0)


def _format_japanese(n: int) -> str:
    """5090 / 5万3000 / 1億2000万 のように、万以上は日本語の単位を残して読みやすくする。"""
    if n < 10**4:
        return str(n)
    parts = []
    for unit, name in ((10**12, "兆"), (10**8, "億"), (10**4, "万")):
        q, n = divmod(n, unit)
        if q:
            parts.append(f"{q}{name}")
    if n:
        parts.append(str(n))
    return "".join(parts)


def _should_convert(run: str, before: str, after: str) -> bool:
    if run[0] in _BIG:
        return False  # 「万一」など
    has_unit = any(c in _SMALL or c in _BIG for c in run)
    has_zero = any(c in "〇零" for c in run)
    if not has_unit and not has_zero:
        # 1 文字の数（五）: 前後が漢字でなければ数として使っている（「五を押して」「バージョン五」「五です」）。
        # 前後が漢字なら言葉の一部（一緒・一番・二人・第五）、「一つ・五つ」は数え方なのでそのまま
        if len(run) == 1:
            prev_kanji = bool(before) and re.match(f"[{_KANJI}]", before[-1]) is not None
            next_kanji = bool(after) and re.match(f"[{_KANJI}]", after[0]) is not None
            return not prev_kanji and not next_kanji and not after.startswith(_NATIVE_AFTER)
        return False  # 二三日・四五人 などはそのまま
    if not has_unit and len(run) < 2:
        return False
    if len(run) == 1:
        # 一字の十・百・千: 後ろが単位のときだけ（十時→10時、百円→100円。十分(じゅうぶん)は除く）
        if run == "十" and after.startswith("分") and not re.match(_NUM_BEFORE, before[-1:] or " "):
            return False
        return after.startswith(_COUNTERS)
    return True


def convert_kanji_numbers(text: str) -> str:
    if not text:
        return text
    # 慣用表現は一時的に記号へ置き換えて、どの変換にも触らせない
    kept: List[str] = []

    def _mask(m: re.Match) -> str:
        kept.append(m.group())
        return chr(0xE000 + len(kept) - 1)

    text = re.sub("|".join(re.escape(w) for w in sorted(_KEEP_WORDS, key=len, reverse=True)), _mask, text)

    out, last = [], 0
    for m in _RUN_RE.finditer(text):
        s, e = m.span()
        run = m.group()
        if not _should_convert(run, text[max(0, s - 1):s], text[e:e + 5]):
            continue
        value = _kanji_to_int(run)
        if value is None:
            continue
        out.append(text[last:s])
        out.append(_format_japanese(value))
        last = e
    out.append(text[last:])
    result = "".join(out)

    # 小数: 3点5 → 3.5（「三点五」は上で 3点5 にはならないので、ここで両側を数字にする）
    result = re.sub(
        f"([0-9]+)点([{''.join(_DIGITS)}]+)",
        lambda m2: f"{m2.group(1)}.{''.join(str(_DIGITS[c]) for c in m2.group(2))}",
        result,
    )
    result = re.sub(
        f"(?<![{_NUM_CHARS}])([{''.join(_DIGITS)}])点([{''.join(_DIGITS)}]+)",
        lambda m2: f"{_DIGITS[m2.group(1)]}.{''.join(str(_DIGITS[c]) for c in m2.group(2))}",
        result,
    )
    # 点の両側が数字なら小数（辞書の「五→5」が先に効いて「5点5」になっていても直す。「4点八」「四点8」も）
    result = re.sub(r"(?<![0-9.])([0-9]+)点([0-9]+)(?![0-9])", r"\1.\2", result)
    result = re.sub(
        f"([0-9]+)点([{''.join(_DIGITS)}]+)",
        lambda m2: f"{m2.group(1)}.{''.join(str(_DIGITS[c]) for c in m2.group(2))}",
        result,
    )
    result = re.sub(
        f"(?<![{_NUM_CHARS}])([{''.join(_DIGITS)}]+)点([0-9]+)",
        lambda m2: f"{''.join(str(_DIGITS[c]) for c in m2.group(1))}.{m2.group(2)}",
        result,
    )
    # 3パーセント → 3%
    result = re.sub(r"([0-9](?:[0-9.]*[0-9])?)\s*パーセント", r"\1%", result)
    for i, w in enumerate(kept):
        result = result.replace(chr(0xE000 + i), w)
    return result


def to_halfwidth_alnum(text: str) -> str:
    def conv(c: str) -> str:
        o = ord(c)
        if 0xFF10 <= o <= 0xFF19 or 0xFF21 <= o <= 0xFF3A or 0xFF41 <= o <= 0xFF5A:
            return chr(o - 0xFEE0)
        if c == "％":
            return "%"
        return c

    return "".join(conv(c) for c in text)


class TextNormalizer:
    def __init__(self, disabled_terms: Iterable[str] = ()) -> None:
        self.set_disabled(disabled_terms)

    def set_disabled(self, disabled_terms: Iterable[str]) -> None:
        self.disabled = {str(t) for t in disabled_terms}
        terms = [(k, v) for k, v in BUILTIN_TERMS.items() if k not in self.disabled]
        terms.sort(key=lambda kv: len(kv[0]), reverse=True)
        self._term_rules = [
            (re.compile(f"(?<![{_KATA}]){re.escape(k)}(?![{_KATA}])"), v) for k, v in terms
        ]

    def normalize(self, text: str) -> str:
        if not text:
            return text
        t = to_halfwidth_alnum(text)
        for pattern, dst in self._term_rules:
            t = pattern.sub(lambda _m, d=dst: d, t)
        t = convert_kanji_numbers(t)
        return t
