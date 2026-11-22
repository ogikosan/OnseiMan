"""Windows clipboard & Ctrl+V auto-paste helper (pure ctypes, Unicode-safe)."""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("onseiman.paste")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_C = 0x43
VK_V = 0x56

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _open_clipboard_retry(retries: int = 12, delay_sec: float = 0.015) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay_sec)
    return False


def get_clipboard_text() -> Optional[str]:
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard_retry():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not _open_clipboard_retry():
        logger.warning("クリップボードを開けませんでした")
        return False
    try:
        user32.EmptyClipboard()
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not hmem:
            return False
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return False
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()


VK_LWIN = 0x5B
VK_RWIN = 0x5C
MENU_MASK_VK = 0xE8

TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TokenElevation
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
advapi32.GetTokenInformation.restype = wintypes.BOOL


@dataclass
class PasteResult:
    ok: bool                 # Ctrl+V was delivered to the foreground window
    reason: str              # "pasted" | "elevated_window" | "no_window" | "sendinput_failed" | "clipboard_failed" | "empty"
    clipboard_kept: bool     # recognized text was left in the clipboard for manual Ctrl+V
    window_title: str = ""
    hwnd: int = 0            # 貼り付けたウィンドウ（取り消しで同じウィンドウか確かめる）


def _process_is_elevated(process_handle) -> Optional[bool]:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevated = wintypes.DWORD(0)
        size = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
            token, TOKEN_ELEVATION_CLASS, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)
        ):
            return None
        return bool(elevated.value)
    finally:
        kernel32.CloseHandle(token)


def foreground_window_info() -> tuple[int, str, Optional[bool]]:
    """Return (hwnd, title, is_elevated_and_we_are_not).

    Windows silently drops SendInput into a higher-integrity (admin) window (UIPI) and gives no error,
    so this must be checked beforehand. None means unknown.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, "", None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    title = buf.value

    if _process_is_elevated(kernel32.GetCurrentProcess()):
        return hwnd, title, False  # we are admin too, nothing is blocked

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return hwnd, title, None
    try:
        elevated = _process_is_elevated(hproc)
        if elevated is None:
            # A non-admin process cannot read an admin process token -> treat as elevated
            return hwnd, title, ctypes.get_last_error() == 5
        return hwnd, title, elevated
    finally:
        kernel32.CloseHandle(hproc)


def is_own_window(hwnd) -> bool:
    if not hwnd:
        return False
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == kernel32.GetCurrentProcessId()


def capture_paste_target() -> int:
    """録音開始時の入力先ウィンドウを覚える（OnseiMan 自身なら 0）。"""
    hwnd = user32.GetForegroundWindow()
    if not hwnd or is_own_window(hwnd):
        return 0
    return int(hwnd)


def _restore_foreground(target: int) -> bool:
    """前面を元の入力先へ戻す。Windows は他アプリの前面化を制限するので、
    無害なキーを1回送って「直前に入力したプロセス」になってから再試行する。"""
    if user32.SetForegroundWindow(target):
        pass
    else:
        tap = (INPUT * 2)(_key(MENU_MASK_VK), _key(MENU_MASK_VK, up=True))
        user32.SendInput(2, tap, ctypes.sizeof(INPUT))
        user32.SetForegroundWindow(target)
    for _ in range(20):
        if user32.GetForegroundWindow() == target:
            return True
        time.sleep(0.01)
    return False


def _key(vk: int, up: bool = False) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
    return inp


def _release_stuck_modifiers() -> None:
    """Release modifier keys still reported as held, so Ctrl+V is not turned into Alt+Ctrl+V etc.

    A menu-mask key is tapped first so that a synthetic Alt/Win key-up does not open the menu bar
    or Start menu.
    """
    held = [
        vk
        for vk in (VK_MENU, VK_LMENU, VK_RMENU, VK_SHIFT, VK_LWIN, VK_RWIN)
        if user32.GetAsyncKeyState(vk) & 0x8000
    ]
    if not held:
        return
    ups = [_key(MENU_MASK_VK), _key(MENU_MASK_VK, up=True)] + [_key(vk, up=True) for vk in held]
    arr = (INPUT * len(ups))(*ups)
    user32.SendInput(len(ups), arr, ctypes.sizeof(INPUT))
    time.sleep(0.02)


def send_ctrl_v() -> bool:
    _release_stuck_modifiers()
    keys = [_key(VK_CONTROL), _key(VK_V), _key(VK_V, up=True), _key(VK_CONTROL, up=True)]
    inputs = (INPUT * 4)(*keys)
    sent = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
    if sent == 4:
        return True
    err = ctypes.get_last_error()
    logger.warning("SendInput(Ctrl+V) が失敗しました sent=%d/4 (err=%d)", sent, err)
    if 0 < sent < 4:
        # Make sure Ctrl is not left held down
        rel = (INPUT * 1)(_key(VK_CONTROL, up=True))
        user32.SendInput(1, rel, ctypes.sizeof(INPUT))
    return False


def paste_text(
    text: str,
    restore_clipboard: bool = True,
    paste_delay_ms: int = 80,
    restore_delay_ms: int = 250,
    target_hwnd: int = 0,
) -> PasteResult:
    """Paste text at the caret of the foreground window via clipboard + Ctrl+V.

    If the paste cannot be delivered (admin window, no window, SendInput failure), the text is left
    in the clipboard so the user can press Ctrl+V manually; the previous clipboard is NOT restored.
    """
    if not text:
        return PasteResult(False, "empty", False)

    # 貼り付けの瞬間に OnseiMan 自身のウィンドウが前面に来ていたら、録音開始時の入力先へ戻す
    fg = user32.GetForegroundWindow()
    if target_hwnd and (not fg or is_own_window(fg)) and user32.IsWindow(target_hwnd):
        if _restore_foreground(target_hwnd):
            logger.info("入力先が OnseiMan に奪われていたため、元のウィンドウに戻しました")
        else:
            logger.warning("元の入力先ウィンドウに戻せませんでした")

    hwnd, title, elevated = foreground_window_info()
    if is_own_window(hwnd):
        # 自分自身へ貼っても意味がないので、クリップボードに残して知らせる
        set_clipboard_text(text)
        return PasteResult(False, "own_window", True, title)
    previous_text = get_clipboard_text() if restore_clipboard else None

    if not set_clipboard_text(text):
        return PasteResult(False, "clipboard_failed", False, title)

    if not hwnd:
        return PasteResult(False, "no_window", True, title)
    if elevated:
        return PasteResult(False, "elevated_window", True, title)

    if paste_delay_ms > 0:
        time.sleep(paste_delay_ms / 1000.0)

    if not send_ctrl_v():
        return PasteResult(False, "sendinput_failed", True, title)

    if restore_clipboard and previous_text is not None:
        time.sleep(max(restore_delay_ms, 150) / 1000.0)
        set_clipboard_text(previous_text)
        return PasteResult(True, "pasted", False, title, int(hwnd))

    return PasteResult(True, "pasted", True, title, int(hwnd))


VK_BACK = 0x08
VK_Z = 0x5A

# Ctrl+Z が「元に戻す」にならない（^Z が入力される）端末類。ここだけは Backspace で消す。
_TERMINAL_PROCESSES = {
    "windowsterminal.exe", "cmd.exe", "conhost.exe", "powershell.exe", "pwsh.exe",
    "openconsole.exe", "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "putty.exe",
}


def _send_keys(keys: list) -> bool:
    arr = (INPUT * len(keys))(*keys)
    sent = user32.SendInput(len(keys), arr, ctypes.sizeof(INPUT))
    if sent != len(keys):
        rel = (INPUT * 1)(_key(VK_CONTROL, up=True))
        user32.SendInput(1, rel, ctypes.sizeof(INPUT))
        return False
    return True


def undo_paste(text: str, target_hwnd: int, process_name: str = "") -> tuple[bool, str]:
    """直前に貼り付けた文を取り消す。戻り値は (成功したか, 理由)。

    通常のアプリは Ctrl+Z（貼り付けは 1 回の操作として戻る）。端末は Ctrl+Z が効かないので、
    貼った文字数ぶん Backspace を送る。前面が貼り付けたウィンドウでなければ何もしない。
    """
    if not text or not target_hwnd:
        return False, "nothing"
    fg = user32.GetForegroundWindow()
    if fg != target_hwnd:
        if fg and not is_own_window(fg):
            return False, "other_window"
        if not user32.IsWindow(target_hwnd) or not _restore_foreground(target_hwnd):
            return False, "other_window"
    _release_stuck_modifiers()
    if process_name.lower() in _TERMINAL_PROCESSES:
        n = len(text.replace(chr(13) + chr(10), chr(10)))
        keys = []
        for _ in range(n):
            keys += [_key(VK_BACK), _key(VK_BACK, up=True)]
        return (_send_keys(keys), "backspace") if keys else (False, "nothing")
    ok = _send_keys([_key(VK_CONTROL), _key(VK_Z), _key(VK_Z, up=True), _key(VK_CONTROL, up=True)])
    return ok, "ctrl_z"


def send_ctrl_c() -> bool:
    _release_stuck_modifiers()
    keys = [_key(VK_CONTROL), _key(VK_C), _key(VK_C, up=True), _key(VK_CONTROL, up=True)]
    inputs = (INPUT * 4)(*keys)
    sent = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
    if sent == 4:
        return True
    if 0 < sent < 4:
        rel = (INPUT * 1)(_key(VK_CONTROL, up=True))
        user32.SendInput(1, rel, ctypes.sizeof(INPUT))
    return False


def capture_selected_text(target_hwnd: int = 0) -> str:
    """前面ウィンドウで選択中のテキストを Ctrl+C で安全に取得する（元のクリップボードは必ず復元）。

    何も選択されていない場合は空文字を返す。
    """
    import uuid

    hwnd, _title, elevated = foreground_window_info()
    if not hwnd or is_own_window(hwnd) or elevated:
        return ""
    if target_hwnd and hwnd != target_hwnd and user32.IsWindow(target_hwnd):
        _restore_foreground(target_hwnd)

    previous_text = get_clipboard_text()
    sentinel = f"__ONSEIMAN_SEL_PROBE_{uuid.uuid4().hex}__"
    if not set_clipboard_text(sentinel):
        return ""
    try:
        if not send_ctrl_c():
            return ""
        selected = ""
        for _ in range(12):
            time.sleep(0.015)
            cur = get_clipboard_text()
            if cur is not None and cur != sentinel:
                selected = cur
                break
        return selected
    finally:
        if previous_text is not None:
            set_clipboard_text(previous_text)
        else:
            # テキスト以外が入っていた・空だった場合、マーカーだけ残らないようにクリア
            if get_clipboard_text() == sentinel:
                set_clipboard_text("")
