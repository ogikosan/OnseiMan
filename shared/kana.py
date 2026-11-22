"""ひらがな・カタカナの相互変換と、IME ユーザー辞書（テキスト書き出し）の読み込み。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

_HIRA_START, _HIRA_END = 0x3041, 0x3096
_KATA_OFFSET = 0x60
_KANA_ONLY_RE = re.compile(r"^[ぁ-ゖァ-ヺー・ゝゞヽヾ]+$")


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + _KATA_OFFSET) if _HIRA_START <= ord(c) <= _HIRA_END else c for c in text)


def to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(c) - _KATA_OFFSET) if _HIRA_START + _KATA_OFFSET <= ord(c) <= _HIRA_END + _KATA_OFFSET else c
        for c in text
    )


def is_kana_only(text: str) -> bool:
    return bool(_KANA_ONLY_RE.match(text))


def kana_variants(text: str) -> List[str]:
    """かなだけの言葉なら、ひらがな・カタカナ両方の書き方を返す（読みで登録しても両方に効く）。"""
    if not is_kana_only(text):
        return [text]
    out: List[str] = []
    for v in (text, to_hiragana(text), to_katakana(text)):
        if v not in out:
            out.append(v)
    return out


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    # BOM なし UTF-16LE（Microsoft IME の書き出しで見られる）
    if len(raw) > 4 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le")
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_ime_user_dictionary(path: str | Path) -> List[Tuple[str, str]]:
    """Microsoft IME / Google 日本語入力 / ATOK の「テキストに書き出し」ファイルを読む。

    各行「読み<TAB>単語<TAB>品詞[<TAB>コメント]」。「!」「#」で始まる行や空行は無視。
    戻り値は (読み, 単語) のリスト（重複・読みと単語が同じものは除く）。
    """
    text = _decode(Path(path).read_bytes())
    pairs: List[Tuple[str, str]] = []
    seen = set()
    for line in text.splitlines():
        line = line.strip("﻿").rstrip("\r")
        if not line.strip() or line.lstrip().startswith(("!", "#")):
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        reading, word = cols[0].strip(), cols[1].strip()
        if not reading or not word or reading == word:
            continue
        if (reading, word) in seen:
            continue
        seen.add((reading, word))
        pairs.append((reading, word))
    return pairs
