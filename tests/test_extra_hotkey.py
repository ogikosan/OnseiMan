"""もう 1 つの録音キー（キーボードのキーでも、マウスのボタンでもよい）のテスト。"""
from __future__ import annotations

import ctypes
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import client.input_hook as ih  # noqa: E402


def _kb(vk: int, msg: int):
    st = ih.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=0, time=0, dwExtraInfo=0)
    return st, ctypes.addressof(st), msg


def _mouse(msg: int, xbutton: int = 0):
    st = ih.MSLLHOOKSTRUCT(pt=ih.POINT(0, 0), mouseData=(xbutton << 16), flags=0, time=0, dwExtraInfo=0)
    return st, ctypes.addressof(st), msg


class TestExtraHotkey(unittest.TestCase):
    def _hook(self, extra):
        hook = ih.GlobalInputHook(
            hotkey="ralt", cancel_key="esc", suppress_hotkey=True,
            on_press=lambda: None, on_release=lambda: None, on_cancel=lambda: None,
            is_recording_fn=lambda: False, extra_hotkeys=extra,
        )
        hook._stopped = True
        p = mock.patch.object(ih, "send_menu_mask")
        p.start()
        self.addCleanup(p.stop)
        return hook

    def _drain(self, hook):
        out = []
        while not hook._event_queue.empty():
            out.append(hook._event_queue.get_nowait()[0])
        return out

    def test_extra_keyboard_key_records_like_main_key(self) -> None:
        hook = self._hook(["f8"])
        for msg in (ih.WM_KEYDOWN, ih.WM_KEYUP):
            _st, ptr, m = _kb(0x77, msg)  # F8
            self.assertEqual(hook._keyboard_proc(0, m, ptr), 1, "アプリには渡さない")
        self.assertEqual(self._drain(hook), ["press", "release"])
        # いままでの録音キー（右Alt）も、そのまま使える
        for msg in (ih.WM_SYSKEYDOWN, ih.WM_KEYUP):
            _st, ptr, m = _kb(0xA5, msg)
            hook._keyboard_proc(0, m, ptr)
        self.assertEqual(self._drain(hook), ["press", "release"])
        self.assertEqual(hook.typed_key_count, 0, "録音キーは文字を打った回数に数えない")

    def test_extra_mouse_button(self) -> None:
        hook = self._hook(["mouse_x1"])
        self.assertTrue(hook._wants_mouse_hook(), "マウスのボタンなら、マウスの見張りを付ける")
        for msg in (ih.WM_XBUTTONDOWN, ih.WM_XBUTTONUP):
            _st, ptr, m = _mouse(msg, ih.XBUTTON1)
            self.assertEqual(hook._mouse_proc(0, m, ptr), 1)
        self.assertEqual(self._drain(hook), ["press", "release"])
        # 進むボタンは録音キーではない
        _st, ptr, m = _mouse(ih.WM_XBUTTONDOWN, ih.XBUTTON2)
        self.assertNotEqual(hook._mouse_proc(0, m, ptr), 1)
        self.assertEqual(self._drain(hook), [])

    def test_no_mouse_hook_without_mouse_keys(self) -> None:
        hook = self._hook(["f8"])
        self.assertFalse(hook._wants_mouse_hook(), "キーボードだけなら、マウスの見張りは付けない（マウスが重くならない）")

    def test_change_and_clear(self) -> None:
        hook = self._hook([])
        hook.update_bindings("ralt", "esc", True, extra_hotkeys=["mouse_x2"])
        self.assertEqual(hook.extra_bindings, [("mouse", "x2")])
        # ほかの設定を変えても（extra_hotkeys を渡さなければ）、もう 1 つの録音キーは残る
        hook.update_bindings("f2", "esc", True)
        self.assertEqual(hook.extra_bindings, [("mouse", "x2")])
        hook.update_bindings("ralt", "esc", True, extra_hotkeys=[])
        self.assertEqual(hook.extra_bindings, [])
        self.assertFalse(hook._wants_mouse_hook())


class TestAppSide(unittest.TestCase):
    def test_bad_name_is_ignored(self) -> None:
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = mock.MagicMock(hotkey_extra="そんなキーはない")
        self.assertEqual(app._extra_hotkeys(), [])
        app.config = mock.MagicMock(hotkey_extra="mouse_x1")
        self.assertEqual(app._extra_hotkeys(), ["mouse_x1"])
        app.config = mock.MagicMock(hotkey_extra="")
        self.assertEqual(app._extra_hotkeys(), [])


if __name__ == "__main__":
    unittest.main()
