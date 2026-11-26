from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

from shared.fillers import DEFAULT_FILLERS, FillerRemover
from shared.kana import is_kana_only, to_katakana
from shared.normalize import TextNormalizer

import re

# カタカナ語の切れ目: 前後がカタカナ・長音でないこと（「アイ」が「アイデア」の中で置き換わらないように）
_KATA_CHARS = "ァ-ヺー"

logger = logging.getLogger("onseiman.dictionary")


def _is_kana(s: str) -> bool:
    """ひらがな・カタカナ（と長音・中黒）だけでできているか。"""
    return all("\u3041" <= c <= "\u30ff" or c in "ー・" for c in s)


class CustomDictionary:
    """Loads hotwords and post-ASR replacement rules from dict.json with auto hot-reload."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._mtime: float = -1.0
        self._hotwords: List[str] = []
        self._replacements: List[Tuple[str, str]] = []
        self._snippets: Dict[str, str] = {}
        self._filler_remover = FillerRemover(DEFAULT_FILLERS)
        self._normalizer = TextNormalizer()
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        if not self.path.exists():
            if force:
                self._hotwords = []
                self._replacements = []
                self._snippets = {}
                self._mtime = -1.0
            return False

        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False

        if not force and mtime == self._mtime:
            return False

        try:
            raw = self.path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("辞書ファイル %s の読み込みに失敗しました（前回の辞書を維持します）: %s", self.path, exc)
            return False

        hotwords_raw = data.get("hotwords", [])
        if isinstance(hotwords_raw, list):
            self._hotwords = [str(w).strip() for w in hotwords_raw if str(w).strip()]
        else:
            self._hotwords = []

        replacements_raw: Dict[str, str] = data.get("replacements", {})
        if isinstance(replacements_raw, dict):
            # Longer keys first to avoid partial shadowing
            pairs = [
                (str(k), str(v))
                for k, v in replacements_raw.items()
                if str(k) != ""
            ]
            pairs.sort(key=lambda item: len(item[0]), reverse=True)
            self._replacements = pairs
        else:
            self._replacements = []
        self._compile_rules()

        off = data.get("builtin_off")
        self._normalizer.set_disabled(off if isinstance(off, list) else [])

        fillers_raw = data.get("fillers")
        if isinstance(fillers_raw, list):
            self._filler_remover.set_fillers(fillers_raw)
        else:
            self._filler_remover.set_fillers(DEFAULT_FILLERS)

        snippets_raw = data.get("snippets")
        if isinstance(snippets_raw, dict):
            self._snippets = {
                str(k).strip(): str(v)
                for k, v in snippets_raw.items()
                if str(k).strip() and str(v) != ""
            }
        else:
            self._snippets = {}

        self._mtime = mtime
        logger.info(
            "辞書を読み込みました: hotwords=%d件, replacements=%d件, snippets=%d件, fillers=%d件 (%s)",
            len(self._hotwords),
            len(self._replacements),
            len(self._snippets),
            len(self._filler_remover.fillers),
            self.path,
        )
        return True

    @property
    def hotwords(self) -> List[str]:
        self.reload()
        return list(self._hotwords)

    # 音声認識に一度に渡す優先語の上限。優先語はモデルの「一度に扱える量」を使うので、
    # 多すぎると長い発話の後半が消える（実測: 18 秒の発話は 28 語で欠け、36 語でほぼ空）。
    MAX_HOTWORDS = 40

    def active_hotwords(self) -> List[str]:
        """実際に音声認識へ渡す優先語。正しい言葉（置き換え後）も含め、新しく登録したものから上限まで。"""
        self.reload()
        words: List[str] = []
        for w in list(self._hotwords) + [dst for _src, dst in self._replacements]:
            w = str(w).strip()
            # ひらがなだけ・1 文字の言葉はふつうの言葉なので、枠を使わない（置き換えは効く）
            if len(w) < 2 or all("\u3041" <= c <= "\u309f" for c in w):
                continue
            if w not in words:
                words.append(w)
        return words[-self.MAX_HOTWORDS:]

    @property
    def hotwords_csv(self) -> str:
        return ",".join(self.active_hotwords())

    MAX_READINGS = 5

    def keyword_readings(self) -> dict:
        """優先語の「読み」: 聞き間違えた言い方のうち、カナだけで書いたもの（例: GitHub ← ギットハブ）。

        Haas-ja（speech.cpp・NeMo）は、言葉の書き方と読みを渡すと、聞き取る段階でその言葉にしやすくなる。
        """
        active = set(self.active_hotwords())
        out: dict = {}
        for src, dst in self._replacements:
            src, dst = str(src).strip(), str(dst).strip()
            if dst not in active or src == dst or len(src) < 2 or not _is_kana(src):
                continue
            lst = out.setdefault(dst, [])
            if src not in lst and len(lst) < self.MAX_READINGS:
                lst.append(src)
        return out

    def _compile_rules(self) -> None:
        """置き換えルールを作る。

        - かなだけで書いた言葉（読み）は、カタカナにして「カタカナの一続きの語」として現れたときに置き換える。
          音声認識は知らない固有名詞をカタカナで出すので、読みで登録すれば効く。
        - それ以外はそのまま文字列で置き換える。
        """
        rules = []
        for src, dst in self._replacements:
            # ひらがな登録の扱い:
            # - カタカナ語境界で一致させる（「すーぱーべーす」→「スーパーベース」を置換、「アイ」が「アイデア」で化けない）
            # - 3文字以上のひらがな（例: 「どうふさん」）は、ひらがなのまま認識されたときも置換する
            #   （1〜2文字の短いひらがな「あい」等は「ぐあい」誤爆を防ぐためカタカナのみ）
            has_hiragana = any("\u3041" <= c <= "\u3096" for c in src)
            if is_kana_only(src) and has_hiragana:
                kata = to_katakana(src)
                rules.append((re.compile(f"(?<![{_KATA_CHARS}]){re.escape(kata)}(?![{_KATA_CHARS}])"), dst, len(kata)))
                if len(src) >= 3:
                    rules.append((src, dst, len(src)))
            else:
                rules.append((src, dst, len(src)))
        rules.sort(key=lambda r: r[2], reverse=True)
        self._rules = [(a, b) for a, b, _ in rules]

    def normalize(self, text: str) -> str:
        """標準の変換（漢数字→数字、カタカナ略語→英字、全角英数→半角）。dict.json の builtin_off で個別に止められる。"""
        self.reload()
        return self._normalizer.normalize(text)

    def remove_fillers(self, text: str) -> str:
        """「うん、」「えーと」などの言いよどみを取り除く（dict.json の fillers で編集可能）。"""
        self.reload()
        return self._filler_remover.remove(text)

    def apply_replacements(self, text: str) -> str:
        self.reload()
        if not text:
            return text
        result = text
        for src, dst in getattr(self, "_rules", []):
            if isinstance(src, str):
                result = result.replace(src, dst)
            else:
                result = src.sub(lambda _m, d=dst: d, result)
        return result

    def apply_snippets(self, text: str) -> Tuple[str, bool]:
        """発話全体が定型文の呼び出し語と一致したとき、登録された定型文に展開する。

        文の途中の単語（「メールアドレスを教えて」等）で誤爆しないよう、
        前後の空白・句読点を除いた発話全体が一致したときだけ展開する。
        """
        self.reload()
        if not text or not self._snippets:
            return text, False
        key = text.strip().strip("。、.,！？!?「」『』 ")
        if not key:
            return text, False
        if key in self._snippets:
            return self._snippets[key], True
        key_lower = key.lower()
        key_kata = to_katakana(key) if is_kana_only(key) else ""
        for trig, body in self._snippets.items():
            t = trig.strip().strip("。、.,！？!? ")
            if not t:
                continue
            if t == key or t.lower() == key_lower:
                return body, True
            if key_kata and is_kana_only(t) and to_katakana(t) == key_kata:
                return body, True
        return text, False

    def add_replacement(self, src: str, dst: str, *, also_hotword: bool = False) -> None:
        """履歴画面などから 1 クリックで置き換え（および任意で優先語）を dict.json へ追記し即反映する。"""
        src = src.strip()
        dst = dst.strip()
        if not src:
            raise ValueError("置き換え前の言葉が空です")
        data: dict = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except Exception:
                data = {}
        reps = dict(data.get("replacements") or {})
        # 大文字・小文字だけ違う「正しい言葉」がすでにあれば、そちらにまとめる（Github と GitHub など）
        for known in [str(w) for w in (data.get("hotwords") or [])] + [str(v) for v in reps.values()]:
            if known != dst and known.lower() == dst.lower():
                dst = known
                break
        reps[src] = dst
        data["replacements"] = reps
        if also_hotword and dst:
            hot = [str(w) for w in (data.get("hotwords") or []) if str(w).strip()]
            if dst not in hot:
                hot.append(dst)
            data["hotwords"] = hot
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        self.reload(force=True)
