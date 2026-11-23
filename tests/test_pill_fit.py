"""録音中などの小さな表示（カプセル）に、長い文がはみ出さないことのテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from client.pill import MAX_W, PillRenderer, kind_from_status
    HAVE = True
except Exception:  # Windows 以外・描画の部品がない環境
    HAVE = False


@unittest.skipUnless(HAVE, "描画の部品がありません")
class TestPillFit(unittest.TestCase):
    def _check(self, text: str, sub: str = "") -> dict:
        r = PillRenderer(1.5, False)
        kind, main = kind_from_status(text)
        st = {"kind": kind, "main": main, "sub": sub}
        lay = r.layout(st)
        end = lay["x_text"] + r._text_w(lay["main"], r.f_main)
        self.assertLessEqual(lay["w"], MAX_W)
        self.assertLessEqual(end, lay["w"], f"本文がはみ出しています: {lay['main']}")
        r.render(st, None)  # 描けること
        return lay

    def test_long_text_is_cut_with_ellipsis(self) -> None:
        lay = self._check("新しい版があります（1.2.8 → 1.2.9）。画面の上のお知らせから更新できます。" * 2)
        self.assertTrue(lay["main"].endswith("…"))

    def test_short_text_is_kept(self) -> None:
        lay = self._check("✓ 準備できました（Mac で認識）")
        self.assertFalse(lay["main"].endswith("…"))

    def test_update_notice_fits_without_cut(self) -> None:
        lay = self._check("新しい版 1.2.10 があります（右上の版のボタンから更新）")
        self.assertFalse(lay["main"].endswith("…"))


if __name__ == "__main__":
    unittest.main()
