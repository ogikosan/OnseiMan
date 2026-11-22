"""ペースト後のユーザー修正から辞書学習候補を安全に抽出・管理する。"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("onseiman.learner")

# 助詞や句読点だけの置換は誤認識の修正ではなく文章の手直しなので除外する
_PARTICLES = {
    "は", "が", "を", "に", "で", "と", "へ", "も", "や", "の", "か", "ね", "よ", "わ", "さ",
    "て", "た", "だ", "な", "し", "ば", "ど", "ん", "から", "まで", "より", "けど", "のに",
}
_PUNCT_SPACE_RE = re.compile(r"^[\s、。，．,.!！?？・ー\-―〜~「」『』()（）\[\]【】\"']+$")
_HONORIFICS = ("さん", "くん", "ちゃん", "様", "氏", "殿")


# 外してよい助詞（「さん」の「ん」などを削らないよう、言葉の一部になりやすい字は入れない）
_TRIM_PARTICLES = ("から", "まで", "より", "けど", "のに", "は", "が", "を", "に", "で", "と", "へ", "も", "や", "の")


def _trim_shared_particles(src: str, dst: str) -> tuple:
    """両方の後ろ（前）に同じ助詞がついているだけなら外す。

    「山駄さんも → 山田さんも」を「山駄さん → 山田さん」として覚え、「山田さんも」「山田さんと」… と
    助詞ごとに別々の登録ができるのを防ぐ。
    """
    for p in _TRIM_PARTICLES:
        if src.endswith(p) and dst.endswith(p) and len(src) > len(p) and len(dst) > len(p):
            src, dst = src[: -len(p)], dst[: -len(p)]
            break
    for p in _TRIM_PARTICLES:
        if src.startswith(p) and dst.startswith(p) and len(src) > len(p) and len(dst) > len(p):
            src, dst = src[len(p):], dst[len(p):]
            break
    return src, dst


def _is_hiragana_only(s: str) -> bool:
    return bool(s) and all("\u3041" <= c <= "\u3096" or c == "ー" for c in s)


def _char_script(c: str) -> str:
    if "\u4e00" <= c <= "\u9fff" or c == "々":
        return "kanji"
    if "\u30a1" <= c <= "\u30fa" or c == "ー":
        return "kata"
    if c.isascii() and c.isalnum():
        return "ascii"
    if "\u3041" <= c <= "\u3096":
        return "hira"
    return "other"


def _strip_edge_punct(s: str) -> str:
    return s.strip(" \t\r\n、。，．,.!！?？「」『』()（）\"'")


def _locate_edited_span(pasted: str, edited_full: str, baseline_full: Optional[str]) -> Optional[str]:
    """入力欄全体 (edited_full) の中から、今回ペーストした文に対応する範囲を切り出す。"""
    if not pasted or not edited_full:
        return None

    # ペースト直後の全体スナップショットがある場合、前後の共通接頭・接尾を削ってペースト範囲を取り出す
    if baseline_full and pasted in baseline_full:
        pos = baseline_full.find(pasted)
        prefix = baseline_full[:pos]
        suffix = baseline_full[pos + len(pasted) :]

        start = 0
        if prefix:
            if edited_full.startswith(prefix):
                start = len(prefix)
            else:
                anchor = prefix[-16:]
                idx = edited_full.find(anchor)
                if idx != -1:
                    start = idx + len(anchor)

        end = len(edited_full)
        if suffix:
            if edited_full.endswith(suffix):
                end = len(edited_full) - len(suffix)
            else:
                anchor = suffix[:16]
                idx = edited_full.find(anchor, start)
                if idx != -1:
                    end = idx

        if 0 <= start < end <= len(edited_full):
            return edited_full[start:end]

    # ベースラインが無い場合（履歴画面の手動編集など）はそのまま比較
    return edited_full


def extract_corrections(
    pasted: str,
    edited_full: str,
    baseline_full: Optional[str] = None,
) -> List[Tuple[str, str]]:
    """ペーストした文とユーザー修正後の文から、(誤認識語, 正しい語) のペアを安全に抽出する。"""
    pasted_clean = pasted.strip()
    if not pasted_clean or not edited_full:
        return []

    span = _locate_edited_span(pasted_clean, edited_full.strip(), baseline_full)
    if not span or span == pasted_clean:
        return []

    # 短い1フレーズ丸ごとの書き直し（例: ゾングリコロコロダンブリ → どんぐりころころどんぶりこ、
    # スーパーベース → Supabase）は共通文字が0で ratio() が低くなるため、直接ペアとして扱う
    sm = difflib.SequenceMatcher(None, pasted_clean, span, autojunk=False)
    if sm.ratio() < 0.45:
        src_w = _strip_edge_punct(pasted_clean)
        dst_w = _strip_edge_punct(span)
        if (
            2 <= len(src_w) <= 14
            and 1 <= len(dst_w) <= 18
            and 0.35 <= (len(dst_w) / len(src_w)) <= 2.2
            and "\n" not in src_w
            and "\n" not in dst_w
            and "。" not in src_w
            and "。" not in dst_w
            and not _PUNCT_SPACE_RE.match(src_w)
            and not _PUNCT_SPACE_RE.match(dst_w)
            and src_w not in _PARTICLES
            and dst_w not in _PARTICLES
        ):
            return [(src_w, dst_w)]
        return []

    pairs: List[Tuple[str, str]] = []
    opcodes = sm.get_opcodes()

    for idx, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag != "replace":
            continue

        src = pasted_clean[i1:i2]
        dst = span[j1:j2]

        # 1文字だけの差分（例: 実力→出力 の「実→出」、再起動→再始動 の「起→始」）は
        # 単文字で辞書登録すると誤爆するため、前後に繋がる同一文字種（漢字・カタカナ・英数字）まで
        # 単語単位に広げる
        if len(src) < 2 or len(dst) < 2:
            # 対象スクリプト（漢字・カタカナ・英数字のみ拡張）
            scripts = {_char_script(c) for c in (src + dst)} & {"kanji", "kata", "ascii"}
            if len(scripts) == 1:
                target_script = next(iter(scripts))
                # 左側へ拡張（両側で同じ文字種が続く限り）
                li1, lj1 = i1, j1
                while (
                    li1 > 0
                    and lj1 > 0
                    and pasted_clean[li1 - 1] == span[lj1 - 1]
                    and _char_script(pasted_clean[li1 - 1]) == target_script
                ):
                    li1 -= 1
                    lj1 -= 1
                # 右側へ拡張
                li2, lj2 = i2, j2
                while (
                    li2 < len(pasted_clean)
                    and lj2 < len(span)
                    and pasted_clean[li2] == span[lj2]
                    and _char_script(pasted_clean[li2]) == target_script
                ):
                    li2 += 1
                    lj2 += 1
                src = pasted_clean[li1:li2]
                dst = span[lj1:lj2]
                i2, j2 = li2, lj2

        # 直後が「さん」「くん」「ちゃん」等の敬称なら、誤爆防止のため敬称込みで覚える
        after_src = pasted_clean[i2:]
        after_dst = span[j2:]
        for hon in _HONORIFICS:
            if after_src.startswith(hon) and after_dst.startswith(hon):
                src = src + hon
                dst = dst + hon
                break

        src = _strip_edge_punct(src)
        dst = _strip_edge_punct(dst)
        src, dst = _trim_shared_particles(src, dst)

        if not src or not dst or src == dst:
            continue
        if _PUNCT_SPACE_RE.match(src) or _PUNCT_SPACE_RE.match(dst):
            continue
        # 単語拡張しても1文字しか残らない置換（例: 私→俺、実→出）は誤爆の元なので除外
        if len(src) < 2:
            continue
        # 長すぎる書き直しは文章編集なので除外
        if len(src) > 12 or len(dst) > 16:
            continue
        # 1〜2文字のひらがな助詞・言い換えは誤爆しやすいので除外
        if src in _PARTICLES or dst in _PARTICLES:
            continue
        if len(src) <= 2 and _is_hiragana_only(src) and _is_hiragana_only(dst):
            continue
        # 改行をまたぐ編集は除外
        if "\n" in src or "\n" in dst:
            continue

        pair = (src, dst)
        if pair not in pairs:
            pairs.append(pair)

    return pairs


@dataclass
class LearningCandidate:
    src: str
    dst: str
    count: int = 1
    example_before: str = ""
    example_after: str = ""
    updated_at: str = ""


class CorrectionCandidateStore:
    """半自動学習の候補を JSON で管理する。

    - ×で消した候補（dismissed）: 同じ直しをまたしたら候補に出すが、自動では辞書に登録しない。
      画面の「消した候補」から、いつでも戻せる（回数・例文もそのまま）。
    - 無視（ignored）: 候補にも出さない。画面からは使わない（前の版の「今後出さない」は、読み込み時に dismissed に移す）。
    """

    MAX_DISMISSED = 300

    def __init__(self, path: Path) -> None:
        self.path = path
        self._candidates: Dict[Tuple[str, str], LearningCandidate] = {}
        self._ignored: set[Tuple[str, str]] = set()
        self._dismissed: Dict[Tuple[str, str], LearningCandidate] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.warning("学習候補ファイル %s の読み込みに失敗: %s", self.path, exc)
            return
        self._candidates = {}
        for item in data.get("candidates") or []:
            try:
                c = LearningCandidate(**item)
                if c.src and c.dst:
                    self._candidates[(c.src, c.dst)] = c
            except Exception:
                continue
        self._ignored = set()
        self._dismissed = {}
        for item in data.get("dismissed") or []:
            try:
                c = LearningCandidate(**item) if isinstance(item, dict) else LearningCandidate(str(item[0]), str(item[1]))
                if c.src and c.dst:
                    self._dismissed[(c.src, c.dst)] = c
            except Exception:
                continue
        # 前の版の「今後出さない」は、二度と出ないように見えて困るので、×で消したものと同じ扱いにする（戻せる）
        for pair in data.get("ignored") or []:
            if isinstance(pair, list) and len(pair) == 2 and str(pair[0]) and str(pair[1]):
                key = (str(pair[0]), str(pair[1]))
                self._dismissed.setdefault(key, LearningCandidate(key[0], key[1]))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "candidates": [asdict(c) for c in self.list_candidates()],
            "ignored": sorted([list(p) for p in self._ignored]),
            "dismissed": [asdict(c) for c in self.list_dismissed()],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def list_candidates(self) -> List[LearningCandidate]:
        return sorted(
            self._candidates.values(),
            key=lambda c: (c.count, c.updated_at),
            reverse=True,
        )

    def record(
        self,
        src: str,
        dst: str,
        *,
        example_before: str = "",
        example_after: str = "",
        existing_replacements: Optional[Iterable[str]] = None,
    ) -> Optional[LearningCandidate]:
        """候補を記録する。既に無視リストまたは辞書登録済みなら None を返す。"""
        key = (src.strip(), dst.strip())
        if not key[0] or not key[1] or key[0] == key[1]:
            return None
        if key in self._ignored:
            return None
        if existing_replacements is not None and key[0] in set(existing_replacements):
            return None

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if key in self._candidates:
            c = self._candidates[key]
            c.count += 1
            c.updated_at = now
            if example_before:
                c.example_before = example_before
            if example_after:
                c.example_after = example_after
        else:
            c = LearningCandidate(
                src=key[0],
                dst=key[1],
                count=1,
                example_before=example_before,
                example_after=example_after,
                updated_at=now,
            )
            self._candidates[key] = c
        self.save()
        return c

    def dismiss(self, src: str, dst: str) -> None:
        """×: 候補から消す。同じ直しをまたしたら候補に出すが、自動では登録しない。「消した候補」から戻せる。"""
        key = (src, dst)
        c = self._candidates.pop(key, None) or self._dismissed.get(key) or LearningCandidate(src, dst)
        c.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._dismissed[key] = c
        if len(self._dismissed) > self.MAX_DISMISSED:
            for old in self.list_dismissed()[self.MAX_DISMISSED:]:
                self._dismissed.pop((old.src, old.dst), None)
        self.save()

    def list_dismissed(self) -> List[LearningCandidate]:
        """×で消した候補（新しい順）。いま候補に出ているものは除く。"""
        items = [c for k, c in self._dismissed.items() if k not in self._candidates]
        return sorted(items, key=lambda c: c.updated_at, reverse=True)

    def restore(self, c: LearningCandidate) -> None:
        """×の取り消し: 候補を元どおり（回数・例文も）戻す。"""
        key = (c.src, c.dst)
        if not c.src or not c.dst:
            return
        self._dismissed.pop(key, None)
        self._ignored.discard(key)
        old = self._candidates.get(key)
        if old is not None:
            old.count = max(old.count, c.count)
        else:
            self._candidates[key] = c
        self.save()

    def is_dismissed(self, src: str, dst: str) -> bool:
        return (src, dst) in self._dismissed

    def remove(self, src: str, dst: str, *, ignore_future: bool = False) -> None:
        key = (src, dst)
        self._candidates.pop(key, None)
        if ignore_future:
            self._ignored.add(key)
        self.save()
