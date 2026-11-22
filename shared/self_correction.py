"""話し言葉の「言い直し」（いやいやそうじゃなくて／じゃなくて／訂正 等）を検知し、
最新の意図だけを残す処理（LLM 連携のトリガー判定 ＋ LLM 未起動時の軽量ルールフォールバック）。
"""
from __future__ import annotations

import re

# 言い直しを示す明確なフレーズ（普通の会話の「いや、やっぱり〜」等は除外して暴発を防ぐ）
_SELF_CORRECTION_TRIGGER_RE = re.compile(
    r"(?:"
    r"いやいや[、，,\s]*(?:そうじゃなくて|じゃなくて|違う|ちがう|待って)?"
    r"|いや[、，,\s]+(?:そうじゃなくて|じゃなくて|待って|ちがう|違う)"
    r"|そうじゃなくて"
    r"|(?:あ|あっ|あー)[、，,\s]*(?:じゃなくて|違う|ちがう|違った|訂正|待って)"
    r"|じゃなくて"
    r"|違う違う|ちがうちがう"
    r"|訂正[、，。．,\s]+"
    r"|間違えた[、，。．,\s]+|まちがえた[、，。．,\s]+"
    r"|言い直し[、，。．,\s]+"
    r")"
)

# ルールベースで前の句・文をバッサリ切っても安全な強い言い直し区切り
_STRONG_CUT_RE = re.compile(
    r"(?:"
    r"いやいや[、，,\s]*(?:そうじゃなくて|じゃなくて)?[、，,\s]*"
    r"|いや[、，,\s]+(?:そうじゃなくて|じゃなくて|待って|違う|ちがう)[、，,\s]*"
    r"|そうじゃなくて[、，,\s]*"
    r"|(?:あ|あっ|あー)[、，,\s]*(?:じゃなくて|違う|ちがう|訂正|待って)[、，,\s]*"
    r"|違う違う[、，,\s]*|ちがうちがう[、，,\s]*"
    r"|訂正[、，。．,\s]+"
    r")"
)


def has_self_correction(text: str) -> bool:
    """テキスト内に「後ろに新しい言い直し内容が続く」明確な言い直し表現があるか判定する。"""
    if not text or len(text) < 6:
        return False
    for m in _SELF_CORRECTION_TRIGGER_RE.finditer(text):
        after = text[m.end() :].strip(" 、，。．.,!！?？")
        # 文末が「〜そうじゃなくて。」等で終わっている場合は、後ろに言い直し先がないので対象外
        if len(after) >= 3:
            return True
    return False


def is_over_truncated(original: str, refined: str) -> bool:
    """LLM が元の文を極端に削りすぎてしまった誤動作を検知する。"""
    orig = original.strip()
    ref = refined.strip()
    if not ref:
        return True
    # 元の文が20文字以上なのに30%未満かつ10文字以下まで激減していたら削りすぎ
    if len(orig) >= 20 and len(ref) <= 10 and (len(ref) / len(orig)) < 0.35:
        return True
    return False


def rule_based_self_correction(text: str) -> str:
    """LLM が使えないときの軽量フォールバック: 強い言い直しフレーズの直前の句/文を削り新しい方だけ残す。"""
    if not text:
        return text
    cur = text
    for _ in range(3):
        m = _STRONG_CUT_RE.search(cur)
        if not m:
            break
        before = cur[: m.start()]
        after = cur[m.end() :].lstrip(" 、，。．,")
        if len(after.strip(" 、，。．.,!！?？")) < 3:
            break
        before_trimmed = before.rstrip(" 、，。．？！\n")
        cut_pos = max(
            before_trimmed.rfind("。"),
            before_trimmed.rfind("？"),
            before_trimmed.rfind("！"),
            before_trimmed.rfind("\n"),
        )
        if cut_pos != -1:
            kept_prefix = before_trimmed[: cut_pos + 1]
            cur = kept_prefix + after
        else:
            cur = after
    return cur.strip()
