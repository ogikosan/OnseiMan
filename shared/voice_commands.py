"""音声コマンド（「かいぎょう」「改行」→ 改行文字）を安全に展開する。

「改行コード」「改行文字」「改行する」「自動改行」など通常の日本語名詞・動詞は壊さず、
独立した音声指示として発話された「改行」「かいぎょう」だけを実際の改行 (\n) に変える。
"""
from __future__ import annotations

import re

# 後ろにこれらが続いていたら「改行」は通常の語句（コマンドではない）
_NOUN_VERB_SUFFIX = (
    r"(?:コード|文字|記号|位置|処理|設定|なし|あり|付き|つき|幅|数|分|前|後|先|元"
    r"|する|して|した|し(?:ます|ない|た|て|よう)|でき|され|せず|せ(?:よ|ず)"
    r"|を|が|は|の|に|で|と|へ|から|まで|も|や|だけ|など|って)"
)
# 前にこれらが付いていたら複合名詞（コマンドではない）
_COMPOUND_PREFIXES = ("自動", "強制", "手動", "連続", "不要な", "余分な", "末尾の")

_NEWLINE_CMD_RE = re.compile(
    r"(?P<lead>[、，,\s]*)"
    r"(?P<word>改行|かいぎょう|カイギョウ)"
    rf"(?!{_NOUN_VERB_SUFFIX})"
    r"(?P<trail>[、，。．.,\s]*)"
)


def apply_voice_commands(text: str) -> str:
    """音声コマンド（改行）を実際の制御文字へ変換する。"""
    if not text:
        return text

    stripped = text.strip(" 、，。．.,")
    if stripped in ("改行", "かいぎょう", "カイギョウ"):
        return "\n"

    def _replace(m: re.Match) -> str:
        word_start = m.start("word")
        before = text[:word_start]
        if before.endswith(_COMPOUND_PREFIXES):
            return m.group(0)
        return "\n"

    result = _NEWLINE_CMD_RE.sub(_replace, text)
    result = re.sub(r"\n[ \t、，]+", "\n", result)
    result = apply_symbol_commands(result)
    return result.strip(" \t")


# ---------------------------------------------------------------- 記号の音声コマンド
# 「かぎかっこ」「かっこ開き」「はてなマーク」など、記号の名前として言ったときだけ記号に変える。
# 普通の言葉と紛れやすいもの（「かっこ」単体＝格好、「びっくり」単体）は対象にしない。
_KAKKO = r"(?:かっこ|カッコ|括弧)"
_OPEN = r"(?:開き|ひらき|ヒラキ)"
_CLOSE = r"(?:閉じ|とじ|トジ)"
_KAGI = r"(?:かぎ|カギ|鍵)"
# 後ろにこれが続くなら記号の話をしているだけ（「はてなマークを付ける」）なので変えない
_SYMBOL_NOT_CMD = r"(?!(?:を|が|は|の|も|って|付き|つき|する|して|した))"
_P = r"[、，。．,.\s]*"

_SYMBOL_RULES = [
    # (パターン, 記号)。前後の句読点はパターンに含めて一緒に消す（「かぎかっことじ」の前の「、」など）
    (re.compile(_P + _KAGI + _KAKKO + _CLOSE + _SYMBOL_NOT_CMD + _P), "」"),
    (re.compile(_KAGI + _KAKKO + _OPEN + "?" + _SYMBOL_NOT_CMD + _P), "「"),
    (re.compile(_P + _KAKKO + _CLOSE + _SYMBOL_NOT_CMD + _P), "）"),
    (re.compile(_KAKKO + _OPEN + _SYMBOL_NOT_CMD + _P), "（"),
    (
        re.compile(_P + r"(?:(?:はてな|ハテナ|クエスチョン)マーク|疑問符)" + _SYMBOL_NOT_CMD + _P),
        "？",
    ),
    (
        re.compile(_P + r"(?:(?:びっくり|ビックリ|エクスクラメーション)マーク|感嘆符)" + _SYMBOL_NOT_CMD + _P),
        "！",
    ),
]


def apply_symbol_commands(text: str) -> str:
    if not text:
        return text
    for pattern, symbol in _SYMBOL_RULES:
        text = pattern.sub(symbol, text)
    return text


# ---------------------------------------------------------------- 取り消しの音声コマンド
_UNDO_RE = re.compile(
    r"^(?:(?:今の|いまの)(?:は|を)?(?:なし|無し|ナシ|取り消し(?:て)?|とりけし|消して|けして|削除(?:して)?)"
    r"|取り消し(?:て)?|とりけし)$"
)


def is_undo_command(text: str) -> bool:
    """発話全体が「今のなし」「取り消し」などの取り消し指示なら True。"""
    if not text:
        return False
    core = re.sub(r"[\s、，。．,.!！?？]", "", text)
    return bool(_UNDO_RE.match(core))
