"""画面の拡大率の扱いが、途中で変わらないことのテスト。

手直しの見張りが使う uiautomation は、読み込んだときにアプリ全体の拡大率の扱いを切り替える。
切り替わると録音中の表示の座標がずれ、画面の外に出て「最初の 1 回しか出ない」ようになっていた（2026-11-23）。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECK = r"""
import ctypes, sys
sys.path.insert(0, sys.argv[1])
u = ctypes.windll.user32
u.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
def aw():
    return u.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(u.GetThreadDpiAwarenessContext()))
import client.main  # 起動と同じく、最初に読み込む
before = aw()
import uiautomation  # 手直しの見張りが、あとから読み込む部品
print(before, aw())
"""


@unittest.skipUnless(sys.platform == "win32", "Windows だけ")
class TestDpiLock(unittest.TestCase):
    def test_uiautomation_does_not_change_dpi_awareness(self) -> None:
        try:
            import uiautomation  # noqa: F401
        except Exception:
            self.skipTest("uiautomation が入っていません")
        r = subprocess.run([sys.executable, "-c", CHECK, str(ROOT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        before, after = r.stdout.split()[-2:]
        self.assertEqual((before, after), ("0", "0"), "拡大率の扱いが変わっています（0 = 拡大率を考えない）")


if __name__ == "__main__":
    unittest.main()
