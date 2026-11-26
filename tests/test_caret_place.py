"""表示をカーソルの近くに置く規則のテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import caret  # noqa: E402

WORK = (0, 0, 1920, 1040)


class TestPlaceNear(unittest.TestCase):
    def setUp(self):
        p = mock.patch("client.caret.work_area_at", return_value=WORK)
        p.start()
        self.addCleanup(p.stop)

    def test_upper_left_of_caret(self):
        # カーソル(500,300)-(501,320) の左上: 右端がカーソルの 10px 左、下端がカーソルの 10px 上
        self.assertEqual(caret.place_near((500, 300, 501, 320), 200, 40, "caret"), (290, 250))

    def test_below_when_no_room_above(self):
        x, y = caret.place_near((500, 20, 501, 40), 200, 40, "msaa")
        self.assertEqual(y, 50)

    def test_left_edge_is_clamped(self):
        x, _ = caret.place_near((50, 300, 51, 320), 200, 40, "caret")
        self.assertEqual(x, 4)

    def test_field_goes_above_field_left(self):
        self.assertEqual(caret.place_near((300, 900, 1500, 960), 200, 40, "field"), (300, 850))

    def test_returns_something_on_real_desktop(self):
        if caret._mouse() is None:
            self.skipTest("画面ロック中または非対話セッションのためカーソル位置を取得できません")
        rect, how = caret.get_caret_rect()  # 何かしら（少なくともマウス位置）は取れる
        self.assertIsNotNone(rect)
        self.assertIn(how, ("caret", "msaa", "field", "mouse"))


if __name__ == "__main__":
    unittest.main()
