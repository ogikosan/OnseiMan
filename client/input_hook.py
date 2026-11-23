"""Windows Low-Level Keyboard & Mouse Hook (FIFO serialized, crash-safe)."""
from __future__ import annotations

import atexit
import ctypes
import logging
import queue
import threading
from ctypes import wintypes
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger("onseiman.hook")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

WM_QUIT = 0x0012
WM_TIMER = 0x0113
WM_APP_SYNC_HOOKS = 0x8001  # re-evaluate whether the mouse hook is needed
HEARTBEAT_INTERVAL_MS = 250

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

# Modifier VKs must NEVER be suppressed, otherwise Windows can get stuck with Alt/Ctrl held down.
MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}

# Alt / Win tapped alone make Windows open the menu bar / Start menu on release, which steals
# focus from the text field so the following Ctrl+V lands in the menu instead of at the caret.
# Same trick as AutoHotkey's "menu mask": tap an unassigned VK while the key is held.
MENU_MASK_TRIGGER_VKS = {0x12, 0xA4, 0xA5, 0x5B, 0x5C}
MENU_MASK_VK = 0xE8  # unassigned virtual-key code, ignored by applications
# 打鍵とみなさないキー（Shift/Ctrl/Alt/Win、マスク用キー）
_NOT_TYPING_VKS = frozenset({0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, 0xE8})

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send_menu_mask() -> None:
    """Tap an unassigned key so a lone Alt/Win press does not activate the menu bar / Start menu."""
    inputs = (_INPUT * 2)()
    inputs[0].type = INPUT_KEYBOARD
    inputs[0].ki = _KEYBDINPUT(wVk=MENU_MASK_VK, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
    inputs[1].type = INPUT_KEYBOARD
    inputs[1].ki = _KEYBDINPUT(wVk=MENU_MASK_VK, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)
    user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(_INPUT))

KEY_NAME_TO_VK: dict[str, int] = {
    "esc": 0x1B,
    "escape": 0x1B,
    "ralt": 0xA5,
    "right_alt": 0xA5,
    "lalt": 0xA4,
    "left_alt": 0xA4,
    "rctrl": 0xA3,
    "right_ctrl": 0xA3,
    "lctrl": 0xA2,
    "left_ctrl": 0xA2,
    "rshift": 0xA1,
    "right_shift": 0xA1,
    "lshift": 0xA0,
    "left_shift": 0xA0,
    "muhenkan": 0x1D,
    "henkan": 0x1C,
    "kana": 0x15,
    "capslock": 0x14,
    "pause": 0x13,
    "scroll_lock": 0x91,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "page_up": 0x21,
    "page_down": 0x22,
    "space": 0x20,
    "tab": 0x09,
    "apps": 0x5D,
    "menu": 0x5D,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}

VK_TO_KEY_NAME: dict[int, str] = {
    0x1B: "esc",
    0xA5: "ralt",
    0xA4: "lalt",
    0xA3: "rctrl",
    0xA2: "lctrl",
    0xA1: "rshift",
    0xA0: "lshift",
    0x1D: "muhenkan",
    0x1C: "henkan",
    0x15: "kana",
    0x14: "capslock",
    0x13: "pause",
    0x91: "scroll_lock",
    0x2D: "insert",
    0x24: "home",
    0x23: "end",
    0x21: "page_up",
    0x22: "page_down",
    0x20: "space",
    0x09: "tab",
    0x5D: "apps",
    **{0x6F + i: f"f{i}" for i in range(1, 25)},
}

MOUSE_BUTTONS = {
    "mouse_x1": ("mouse", "x1"),
    "mouse_side1": ("mouse", "x1"),
    "mouse_back": ("mouse", "x1"),
    "mouse_x2": ("mouse", "x2"),
    "mouse_side2": ("mouse", "x2"),
    "mouse_forward": ("mouse", "x2"),
    "mouse_middle": ("mouse", "middle"),
}


def parse_binding(name: str) -> Tuple[str, int | str]:
    """Parse hotkey string into ('mouse', 'x1'|'x2'|'middle') or ('key', vk_code)."""
    norm = name.strip().lower().replace("-", "_").replace(" ", "_")
    if norm in MOUSE_BUTTONS:
        return MOUSE_BUTTONS[norm]
    if norm in KEY_NAME_TO_VK:
        return ("key", KEY_NAME_TO_VK[norm])
    if norm.startswith("vk:"):
        return ("key", int(norm[3:], 0))
    if len(norm) == 1 and norm.isalnum():
        return ("key", ord(norm.upper()))
    raise ValueError(f"未対応のホットキー指定です: {name!r}")


def format_vk_name(vk: int) -> str:
    if vk in VK_TO_KEY_NAME:
        return VK_TO_KEY_NAME[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return f"vk:0x{vk:02X}"


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL


_KEEP_ALIVE: list = []


class GlobalInputHook:
    """Listens for push-to-talk hotkey (keyboard or mouse side button) and cancel key.

    All callbacks are executed strictly in FIFO order on a single worker thread so rapid
    press/release bursts can never reorder or race against PortAudio.
    """

    def __init__(
        self,
        hotkey: str,
        cancel_key: str,
        suppress_hotkey: bool,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None],
        is_recording_fn: Callable[[], bool],
        heartbeat=None,
        secondary_hotkey: str = "",
        on_secondary_press: Optional[Callable[[], None]] = None,
        on_secondary_release: Optional[Callable[[], None]] = None,
        extra_hotkeys: Optional[List[str]] = None,
    ) -> None:
        self._lock = threading.Lock()
        self.hotkey_name = hotkey
        self.hotkey_binding = parse_binding(hotkey)
        # もう 1 つの録音キー（いまの録音キーとまったく同じ動き。キーボードでもマウスのボタンでもよい）
        self.extra_hotkey_names: List[str] = [h.strip() for h in (extra_hotkeys or []) if h and h.strip()]
        self.extra_bindings: List[Tuple[str, int | str]] = [parse_binding(h) for h in self.extra_hotkey_names]
        self.secondary_hotkey_name = (secondary_hotkey or "").strip()
        self.secondary_binding: Optional[Tuple[str, int | str]] = (
            parse_binding(self.secondary_hotkey_name) if self.secondary_hotkey_name else None
        )
        self.cancel_binding = parse_binding(cancel_key)
        self.suppress_hotkey = suppress_hotkey

        self.on_press = on_press
        self.on_release = on_release
        self.on_secondary_press = on_secondary_press
        self.on_secondary_release = on_secondary_release
        self.on_cancel = on_cancel
        self.is_recording_fn = is_recording_fn

        self._pressed = False
        self._secondary_pressed = False
        self._capture_callback: Optional[Callable[[str], None]] = None
        self._event_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._hook_thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._kb_hook = None
        self._ms_hook = None
        self._kb_proc = None
        self._ms_proc = None
        self._started_event = threading.Event()
        self._stopped = False
        # Shared-memory heartbeat watched by client/watchdog.py (written only from the hook thread,
        # so it stops exactly when the hook thread can no longer answer Windows).
        self.heartbeat = heartbeat
        # VKs whose key-down we swallowed. Their key-up MUST be swallowed too (and only then), so
        # Windows never sees half a keystroke -> no "Alt stuck down" even if bindings change mid-press.
        self._suppressed_down: set[int] = set()
        # Tests only: treat injected (SendInput) events like physical ones.
        self.accept_injected = False
        # 実際に押された文字キーなどの回数（修飾キー・録音キー・キャンセルキーは数えない）。
        # 「今のなし」で取り消すとき、貼り付け後にユーザーが打鍵していないか確かめるのに使う。
        self.typed_key_count = 0

        atexit.register(self.stop)

    def update_bindings(
        self,
        hotkey: str,
        cancel_key: str,
        suppress_hotkey: bool,
        secondary_hotkey: Optional[str] = None,
        extra_hotkeys: Optional[List[str]] = None,
    ) -> None:
        new_hotkey = parse_binding(hotkey)
        new_extra_names = (
            [h.strip() for h in extra_hotkeys if h and h.strip()] if extra_hotkeys is not None else None
        )
        new_extra = [parse_binding(h) for h in new_extra_names] if new_extra_names is not None else None
        new_cancel = parse_binding(cancel_key)
        new_secondary = (
            (parse_binding(secondary_hotkey.strip()) if secondary_hotkey.strip() else None)
            if secondary_hotkey is not None
            else self.secondary_binding
        )
        with self._lock:
            self.hotkey_name = hotkey
            self.hotkey_binding = new_hotkey
            if secondary_hotkey is not None:
                self.secondary_hotkey_name = secondary_hotkey.strip()
                self.secondary_binding = new_secondary
            if new_extra is not None:
                self.extra_hotkey_names = new_extra_names
                self.extra_bindings = new_extra
            self.cancel_binding = new_cancel
            self.suppress_hotkey = suppress_hotkey
            self._pressed = False
            self._secondary_pressed = False
        self._request_hook_sync()
        logger.info(
            "ホットキーを更新しました: hotkey=%s, もう1つ=%s, ai_hotkey=%s, cancel=%s",
            hotkey,
            ",".join(self.extra_hotkey_names) or "(なし)",
            self.secondary_hotkey_name or "(なし)",
            cancel_key,
        )

    def _primary_bindings(self) -> List[Tuple[str, int | str]]:
        """録音キー（いまの録音キーと、もう 1 つの録音キー）。呼ぶ側で self._lock を持つこと。"""
        return [self.hotkey_binding] + list(self.extra_bindings)

    def start_capture_next_input(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._capture_callback = callback
        self._request_hook_sync()

    def _wants_mouse_hook(self) -> bool:
        """The mouse hook slows every mouse event system-wide, so install it only when needed."""
        with self._lock:
            sec_is_mouse = self.secondary_binding is not None and self.secondary_binding[0] == "mouse"
            primary_is_mouse = any(b[0] == "mouse" for b in self._primary_bindings())
            return primary_is_mouse or sec_is_mouse or self._capture_callback is not None

    def _request_hook_sync(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_APP_SYNC_HOOKS, 0, 0)

    def _sync_mouse_hook(self) -> None:
        """Runs on the hook thread only."""
        want = self._wants_mouse_hook()
        if want and not self._ms_hook:
            hmod = kernel32.GetModuleHandleW(None)
            self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
            if self._ms_hook:
                logger.info("マウス監視を開始しました")
            else:
                logger.error("マウス監視の登録に失敗しました (err=%d)", ctypes.get_last_error())
        elif not want and self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None
            logger.info("マウス監視を停止しました（キーボードのホットキーでは不要）")

    def start(self) -> None:
        if self._hook_thread is not None and self._hook_thread.is_alive():
            return
        self._stopped = False
        self._started_event.clear()

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="InputEventWorker", daemon=True
        )
        self._worker_thread.start()

        self._hook_thread = threading.Thread(
            target=self._run_loop, name="InputHookThread", daemon=True
        )
        self._hook_thread.start()
        self._started_event.wait(timeout=3.0)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._event_queue.put(("quit", None))
        if self._thread_id:
            try:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._hook_thread is not None and self._hook_thread.is_alive():
            self._hook_thread.join(timeout=2.0)

    def run_on_worker(self, fn: Callable[[], None]) -> None:
        """Run fn on the same serialized worker as press/release/cancel (e.g. a timer firing)."""
        self._enqueue("call", fn)

    def _enqueue(self, kind: str, payload=None) -> None:
        self._event_queue.put_nowait((kind, payload))

    def _worker_loop(self) -> None:
        while True:
            kind, payload = self._event_queue.get()
            if kind == "quit":
                return
            try:
                if kind == "press":
                    self.on_press()
                elif kind == "release":
                    self.on_release()
                elif kind == "secondary_press" and self.on_secondary_press is not None:
                    self.on_secondary_press()
                elif kind == "secondary_release" and self.on_secondary_release is not None:
                    self.on_secondary_release()
                elif kind == "cancel":
                    self.on_cancel()
                elif kind == "call" and payload is not None:
                    payload()
                elif kind == "capture" and payload is not None:
                    cb, detected = payload
                    cb(detected)
            except Exception as exc:
                logger.exception("入力イベント処理中にエラーが発生しました (%s): %s", kind, exc)

    def _handle_capture(self, detected_name: str) -> bool:
        cb = None
        with self._lock:
            if self._capture_callback is not None:
                cb = self._capture_callback
                self._capture_callback = None
        if cb is not None:
            self._enqueue("capture", (cb, detected_name))
            self._request_hook_sync()
            return True
        return False

    def _keyboard_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        try:
            if nCode >= 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)

                # Ignore injected synthetic events (e.g. our own Ctrl+V paste)
                if (kb.flags & 0x10) and not self.accept_injected:
                    return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

                if is_down and vk not in _NOT_TYPING_VKS and ("key", vk) not in (
                    self.hotkey_binding,
                    self.secondary_binding,
                    self.cancel_binding,
                    *self.extra_bindings,
                ):
                    self.typed_key_count += 1

                # Key-up of a key whose key-down we swallowed: swallow it too, whatever happened since.
                if is_up and vk in self._suppressed_down:
                    self._suppressed_down.discard(vk)
                    release_event = None
                    with self._lock:
                        if self._pressed and ("key", vk) in self._primary_bindings():
                            self._pressed = False
                            release_event = "release"
                        elif self._secondary_pressed and self.secondary_binding == ("key", vk):
                            self._secondary_pressed = False
                            release_event = "secondary_release"
                    if release_event:
                        self._enqueue(release_event)
                    return 1

                # Capture mode for interactive hotkey assignment
                if is_down and self._capture_callback is not None:
                    if vk == 0x1B:
                        self._handle_capture("")
                    else:
                        self._handle_capture(format_vk_name(vk))
                    # Never suppress modifiers even during capture
                    if vk in MODIFIER_VKS:
                        if vk in MENU_MASK_TRIGGER_VKS:
                            send_menu_mask()
                        return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)
                    return 1

                with self._lock:
                    primaries = self._primary_bindings()
                    sec_binding = self.secondary_binding
                    ck_kind, ck_val = self.cancel_binding
                    suppress = self.suppress_hotkey

                # Cancel key while recording
                if is_down and ck_kind == "key" and vk == ck_val and self.is_recording_fn():
                    with self._lock:
                        self._pressed = False
                        self._secondary_pressed = False
                    self._enqueue("cancel")
                    return 1

                # Hotkey (keyboard) — primary or secondary (AI refine)
                matched_role = None
                if ("key", vk) in primaries:
                    matched_role = "primary"
                elif sec_binding is not None and sec_binding == ("key", vk):
                    matched_role = "secondary"

                if matched_role is not None:
                    if is_down:
                        should_trigger = False
                        with self._lock:
                            if matched_role == "primary" and not self._pressed:
                                self._pressed = True
                                should_trigger = True
                            elif matched_role == "secondary" and not self._secondary_pressed:
                                self._secondary_pressed = True
                                should_trigger = True
                        if should_trigger:
                            self._enqueue("press" if matched_role == "primary" else "secondary_press")
                        if suppress and (should_trigger or vk in self._suppressed_down):
                            self._suppressed_down.add(vk)
                            return 1
                        if should_trigger and vk in MENU_MASK_TRIGGER_VKS:
                            send_menu_mask()
                    elif is_up:
                        should_release = False
                        with self._lock:
                            if matched_role == "primary" and self._pressed:
                                self._pressed = False
                                should_release = True
                            elif matched_role == "secondary" and self._secondary_pressed:
                                self._secondary_pressed = False
                                should_release = True
                        if should_release:
                            self._enqueue("release" if matched_role == "primary" else "secondary_release")
        except Exception:
            # Never let an exception break the Windows hook chain
            pass

        return user32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

    def _mouse_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        try:
            if nCode >= 0:
                ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                btn: Optional[str] = None
                is_down = False
                is_up = False

                if wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    xbtn = (ms.mouseData >> 16) & 0xFFFF
                    if xbtn == XBUTTON1:
                        btn = "x1"
                    elif xbtn == XBUTTON2:
                        btn = "x2"
                    is_down = wParam == WM_XBUTTONDOWN
                    is_up = wParam == WM_XBUTTONUP
                elif wParam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                    btn = "middle"
                    is_down = wParam == WM_MBUTTONDOWN
                    is_up = wParam == WM_MBUTTONUP

                if btn is not None:
                    if is_down and self._capture_callback is not None:
                        self._handle_capture(f"mouse_{btn}")
                        return 1

                    with self._lock:
                        primaries = self._primary_bindings()
                        sec_binding = self.secondary_binding
                        suppress = self.suppress_hotkey

                    matched_role = None
                    if ("mouse", btn) in primaries:
                        matched_role = "primary"
                    elif sec_binding is not None and sec_binding == ("mouse", btn):
                        matched_role = "secondary"

                    if matched_role is not None:
                        if is_down:
                            should_trigger = False
                            with self._lock:
                                if matched_role == "primary" and not self._pressed:
                                    self._pressed = True
                                    should_trigger = True
                                elif matched_role == "secondary" and not self._secondary_pressed:
                                    self._secondary_pressed = True
                                    should_trigger = True
                            if should_trigger:
                                self._enqueue("press" if matched_role == "primary" else "secondary_press")
                            if suppress:
                                return 1
                        elif is_up:
                            should_release = False
                            with self._lock:
                                if matched_role == "primary" and self._pressed:
                                    self._pressed = False
                                    should_release = True
                                elif matched_role == "secondary" and self._secondary_pressed:
                                    self._secondary_pressed = False
                                    should_release = True
                            if should_release:
                                self._enqueue("release" if matched_role == "primary" else "secondary_release")
                            if suppress:
                                return 1
        except Exception:
            pass

        return user32.CallNextHookEx(self._ms_hook, nCode, wParam, lParam)

    def _run_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        # Keep the ctypes callback objects alive for the whole process lifetime: if a HOOKPROC
        # thunk were freed while still registered, Windows would jump into freed memory.
        self._kb_proc = HOOKPROC(self._keyboard_proc)
        self._ms_proc = HOOKPROC(self._mouse_proc)
        _KEEP_ALIVE.extend([self._kb_proc, self._ms_proc])

        hmod = kernel32.GetModuleHandleW(None)
        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        if not self._kb_hook:
            logger.error("Windows キーボードフックの登録に失敗しました (err=%d)", ctypes.get_last_error())
        else:
            logger.info("グローバルフック開始: hotkey=%s, cancel=%s", self.hotkey_name, self.cancel_binding)
        self._sync_mouse_hook()

        timer_id = user32.SetTimer(None, 0, HEARTBEAT_INTERVAL_MS, None)
        if self.heartbeat is not None:
            self.heartbeat.beat()
        self._started_event.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if not msg.hWnd:
                if msg.message == WM_TIMER:
                    if self.heartbeat is not None:
                        try:
                            self.heartbeat.beat()
                        except Exception:
                            pass
                    continue
                if msg.message == WM_APP_SYNC_HOOKS:
                    self._sync_mouse_hook()
                    continue
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if timer_id:
            user32.KillTimer(None, timer_id)

        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None
