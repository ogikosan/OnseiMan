"""いま文字を入力している場所（文字カーソル）の画面上の位置を調べる。

アプリによって取れる方法が違うので、順に試す:
  1. GetGUIThreadInfo の文字カーソル（メモ帳など普通の Windows の入力欄）
  2. アクセシビリティ（MSAA の OBJID_CARET）… Chrome / Edge / Discord / VS Code などの Chromium 系
  3. UI Automation で、入力中の欄そのものの位置（文字カーソルは取れないが欄の近くには出せる）
  4. マウスの位置
どれも取れなければ None。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional, Tuple

logger = logging.getLogger("onseiman.caret")

Rect = Tuple[int, int, int, int]  # left, top, right, bottom（画面座標）

user32 = ctypes.WinDLL("user32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
oleacc = ctypes.WinDLL("oleacc")

OBJID_CARET = -8
VT_I4 = 3


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


# IAccessible {618736E0-3C3D-11CF-810C-00AA00389B71}
IID_IAccessible = GUID(0x618736E0, 0x3C3D, 0x11CF, (ctypes.c_ubyte * 8)(0x81, 0x0C, 0x00, 0xAA, 0x00, 0x38, 0x9B, 0x71))


class VARIANT(ctypes.Structure):
    # 64 ビットでは 24 バイト（vt と予約 3 つ + 16 バイトの値）
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("val", ctypes.c_longlong), ("val2", ctypes.c_longlong)]


_ACC_LOCATION = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long),
    ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_long),
    VARIANT,
)
_RELEASE = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)


def _valid(r: Rect) -> bool:
    left, top, right, bottom = r
    return (right - left) < 4000 and (bottom - top) < 4000 and not (left == top == right == bottom == 0)


def _from_gui_thread() -> Optional[Rect]:
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    tid = user32.GetWindowThreadProcessId(fg, None)
    info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(tid, ctypes.byref(info)) or not info.hwndCaret:
        return None
    rc = info.rcCaret
    if rc.right == rc.left and rc.bottom == rc.top:
        return None
    pt1 = wintypes.POINT(rc.left, rc.top)
    pt2 = wintypes.POINT(rc.right, rc.bottom)
    user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt1))
    user32.ClientToScreen(info.hwndCaret, ctypes.byref(pt2))
    r = (pt1.x, pt1.y, max(pt2.x, pt1.x + 1), pt2.y)
    return r if _valid(r) else None


def _from_msaa() -> Optional[Rect]:
    fg = user32.GetForegroundWindow()
    if not fg:
        return None
    ole32.CoInitialize(None)  # 既に初期化済みでも問題ない
    acc = ctypes.c_void_p()
    hr = oleacc.AccessibleObjectFromWindow(wintypes.HWND(fg), wintypes.DWORD(OBJID_CARET & 0xFFFFFFFF),
                                           ctypes.byref(IID_IAccessible), ctypes.byref(acc))
    if hr != 0 or not acc.value:
        return None
    try:
        vtbl = ctypes.cast(ctypes.cast(acc, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        acc_location = _ACC_LOCATION(vtbl[22])  # IUnknown(3) + IDispatch(4) + IAccessible の 16 番目
        left, top, w, h = ctypes.c_long(), ctypes.c_long(), ctypes.c_long(), ctypes.c_long()
        child = VARIANT(vt=VT_I4)
        hr = acc_location(acc, ctypes.byref(left), ctypes.byref(top), ctypes.byref(w), ctypes.byref(h), child)
        if hr != 0 or (w.value == 0 and h.value == 0 and left.value == 0 and top.value == 0):
            return None
        r = (left.value, top.value, left.value + max(w.value, 1), top.value + max(h.value, 1))
        return r if _valid(r) else None
    finally:
        _RELEASE(vtbl[2])(acc)


def _from_uia_focus() -> Optional[Rect]:
    try:
        import uiautomation as auto

        ctrl = auto.GetFocusedControl()
        if ctrl is None:
            return None
        b = ctrl.BoundingRectangle
        r = (int(b.left), int(b.top), int(b.right), int(b.bottom))
        if r[2] - r[0] <= 0 or r[3] - r[1] <= 0 or not _valid(r):
            return None
        return r
    except Exception:
        return None


def _mouse() -> Optional[Rect]:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        return None
    return (pt.x, pt.y, pt.x + 1, pt.y + 20)


_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def get_caret_rect() -> Tuple[Optional[Rect], str]:
    """（位置, どの方法で取れたか）を返す。位置は、このプロセスの画面座標（Tk の座標）で返す。

    画面の拡大率が 100% 以外のとき、Chrome/Discord などは実際の画素で位置を返す一方、
    拡大率に対応していないプロセス（OnseiMan の Tk）は縮めた座標で動く。そのため調べる間だけ
    スレッドを「実際の画素」で動かして全部の方法をそろえ、最後にこのプロセスの座標に直す。
    """
    old = None
    try:
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        old = user32.SetThreadDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    except Exception:
        old = None
    found: Optional[Rect] = None
    how = ""
    fg = user32.GetForegroundWindow()
    pid = wintypes.DWORD(0)
    if fg:
        user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    own = pid.value == ctypes.windll.kernel32.GetCurrentProcessId()
    try:
        real_dpi = user32.GetDpiForSystem() if old else 96
        for name, fn in (("caret", _from_gui_thread), ("msaa", _from_msaa), ("field", _from_uia_focus), ("mouse", _mouse)):
            if own and name != "mouse":
                continue  # 前面が OnseiMan 自身（設定画面など）なら、入力先のカーソルではない
            try:
                r = fn()
                if r and not (user32.MonitorFromPoint(wintypes.POINT(r[0], r[1]), 0)):
                    r = None  # 画面の外（隠してある窓など）は使わない
            except Exception as exc:
                logger.debug("カーソル位置の取得に失敗 (%s): %s", name, exc)
                r = None
            if r:
                found, how = r, name
                break
    finally:
        if old:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old))
    if found is None:
        return None, ""
    proc_dpi = user32.GetDpiForSystem() if old else real_dpi
    if old and real_dpi and proc_dpi != real_dpi:
        f = proc_dpi / real_dpi
        found = tuple(int(round(v * f)) for v in found)  # type: ignore[assignment]
    return found, how


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def work_area_at(x: int, y: int) -> Rect:
    """その点がある画面の、タスクバーを除いた範囲。"""
    mon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        w = mi.rcWork
        return (w.left, w.top, w.right, w.bottom)
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def place_near(rect: Rect, w: int, h: int, how: str, gap: int = 10) -> Tuple[int, int]:
    """幅 w・高さ h の小窓を、rect（文字カーソル）の左上に置く座標。

    真上は書いている文章と重なりやすいので、カーソルより左へずらした斜め上に出す。
    入力欄全体の位置しか取れなかったとき（how == "field"）は、欄の左上の外側に出す。
    上に入らなければカーソルの下へ、左に入らなければ画面の端にそろえる。
    """
    left, top, right, bottom = rect
    wl, wt, wr, wb = work_area_at(left, top)
    x = left if how == "field" else left - w - gap
    y = top - h - gap
    if y < wt:  # 上に入らない → 下へ
        y = bottom + gap
    if y + h > wb:
        y = max(wt, wb - h)
    x = max(wl + 4, min(x, wr - w - 4))
    return int(x), int(y)


def point_on_screen(x: int, y: int) -> bool:
    """その点がどこかの画面の上にあるか（外したモニターの位置を覚えていた場合に使う）。"""
    return bool(user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 0))  # MONITOR_DEFAULTTONULL
