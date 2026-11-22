"""言いよどみ（フィラー）の除去。

「うん、そういえば、うん。」→「そういえば。」のように、句読点や文頭・文末で区切られた
単独のフィラーだけを消す。「あの人」「まあまあ」のように語の一部になっているものは残す。
「えーと」「あのー」など伸ばし・促音を含む形は、文頭なら区切りがなくても消す
（「えーとそういえば」→「そういえば」）。
"""
from __future__ import annotations

import re
from typing import Iterable, List

DEFAULT_FILLERS: List[str] = [
    "えーと", "えっと", "えーっと", "ええと", "えと", "えー", "えぇ",
    "あのー", "あの", "そのー", "その",
    "うーん", "うんうん", "うん", "んー", "ん",
    "あー", "まあ", "まー", "なんか",
]

# 伸ばし棒・小書き・波ダッシュの繰り返し（「えーーと」「あのぉ」「うーん〜」を吸収）
_STRETCH = "[ーぁぃぅぇぉっ〜~]*"
_DELIM = "、。，．,.！？!?\\s「」『』（）()"
_PUNCT_AFTER = "、，,\\s"


def _token(word: str) -> str:
    # 各文字のあとに伸ばしを許す: 「えーと」→ え[ー…]*ー[ー…]*と[ー…]*
    return "".join(re.escape(ch) + _STRETCH for ch in word)


class FillerRemover:
    def __init__(self, fillers: Iterable[str] | None = None) -> None:
        self.set_fillers(DEFAULT_FILLERS if fillers is None else fillers)

    def set_fillers(self, fillers: Iterable[str]) -> None:
        words = sorted({str(w).strip() for w in fillers if str(w).strip()}, key=len, reverse=True)
        self.fillers = words
        if not words:
            self._delimited = self._leading = None
            return
        alt = "|".join(_token(w) for w in words)
        # 区切られた単独のフィラー（後ろの読点・空白も一緒に消す）
        self._delimited = re.compile(
            f"(^|[{_DELIM}])(?:{alt})(?=[{_DELIM}]|$)[{_PUNCT_AFTER}]*"
        )
        stretched = [w for w in words if re.search("[ーっ]", w)]
        self._leading = (
            re.compile(f"(^|[。．.！？!?\\s])(?:{'|'.join(_token(w) for w in stretched)})[{_PUNCT_AFTER}]*")
            if stretched
            else None
        )

    def remove(self, text: str) -> str:
        if not text or self._delimited is None:
            return text
        result = text
        for _ in range(10):  # 「うん、うん、うん」のような連続にも対応
            before = result
            result = self._delimited.sub(lambda m: m.group(1), result)
            if self._leading is not None:
                result = self._leading.sub(lambda m: m.group(1), result)
            if result == before:
                break
        return _tidy_punctuation(result)


def _tidy_punctuation(text: str) -> str:
    t = re.sub(r"[、，,]+([。．.！？!?])", r"\1", text)   # 「そういえば、。」→「そういえば。」
    t = re.sub(r"([。．.！？!?])[、，,]+", r"\1", t)       # 「。、」→「。」
    t = re.sub(r"、{2,}", "、", t)
    t = re.sub(r"。{2,}", "。", t)
    t = re.sub(r"^[\s、，,。．.]+", "", t)                 # 文頭に残った句読点
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()
