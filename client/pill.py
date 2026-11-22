"""OnseiMan の状態表示カプセル。録音中・認識中・入力しました・注意を、ひとつのカプセルで表す。

デザインの考え方
- 録音中: 左の青い丸（マイク）が「録音ボタン」。声の大きさで外側の輪が広がる（聞こえていることが分かる）。
  マウスを乗せると「■（止める）」に変わり、押すと確定。右の小さな ✕ でキャンセル。
  真ん中は経過時間。話しながら認識できた文があれば、それを薄く表示する（OnseiMan の区切り認識を見せる）。
- 認識中: 左が回る輪になり「文字にしています」。
- 入力した: 左が緑の ✓、入力した文の頭を表示。右に「取り消す」（数秒だけ）。
- 注意・エラー: 黄・赤の丸と理由。
幅は中身に合わせて伸び縮みし、中心の位置は動かない。

描き方
Tk では角がギザギザになり、拡大率 150% などでは引き伸ばされてぼやけるため、Windows の
レイヤードウィンドウに Pillow で描いた半透明の絵を直接出す。絵は実際の画素の大きさで、
丸や線は 3 倍で描いてから縮めて縁をなめらかにする。専用スレッドが窓と描画（20 回/秒）を受け持つ。
フォーカスは奪わない（クリックしても入力先のカーソルはそのまま）。ドラッグで動かせる。
"""
from __future__ import annotations

import ctypes
import logging
import math
import os
import queue
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger("onseiman.pill")

# ---------------------------------------------------------------- 寸法（拡大率 100% のとき）
H_BODY = 40
MARGIN = 14              # 影のための余白
ICON_R = 13              # 左の丸の半径
PAD_L = 7                # 本体の左端から丸まで
GAP = 10                 # 丸と文字の間
MIN_W, MAX_W = 168, 440
NOMINAL_W = 240          # 呼び出し側が場所を決めるときに使う大きさ
N_LEVELS = 8

# ---------------------------------------------------------------- 色（OnseiMan の画面と同じすりガラス）
THEMES = {
    "light": {
        "body": (255, 255, 255, 232), "edge": (108, 128, 164, 70), "shadow": (58, 88, 140, 90),
        "text": (29, 40, 66, 255), "muted": (107, 118, 144, 255),
        "btn": (47, 107, 242, 22), "btn_hover": (47, 107, 242, 48), "btn_fg": (71, 85, 120, 255),
        "ring_track": (47, 107, 242, 40),
    },
    "dark": {
        "body": (30, 39, 60, 240), "edge": (255, 255, 255, 30), "shadow": (0, 0, 0, 130),
        "text": (228, 233, 244, 255), "muted": (149, 160, 184, 255),
        "btn": (255, 255, 255, 22), "btn_hover": (255, 255, 255, 48), "btn_fg": (203, 213, 225, 255),
        "ring_track": (255, 255, 255, 40),
    },
}
BLUE_A, BLUE_B = (59, 130, 246), (99, 102, 241)   # アプリのアイコンと同じ青→藍
RED_A, RED_B = (248, 113, 113), (220, 38, 38)
AMBER = (245, 158, 11)
GREEN = (34, 197, 94)
SLATE = (100, 116, 139)


def windows_is_dark() -> bool:
    """Windows のアプリの色（ライト／ダーク）。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def _diag_gradient(size: int, a, b, lighten: float = 0.0) -> Image.Image:
    """左上→右下のグラデーション（アプリのアイコンと同じ向き）。"""
    t = (np.add.outer(np.arange(size), np.arange(size)) / max(1, 2 * (size - 1)))[..., None]
    a_ = np.array(a, dtype=np.float32)
    b_ = np.array(b, dtype=np.float32)
    rgb = a_ + (b_ - a_) * t
    if lighten:
        rgb = rgb + (255 - rgb) * lighten
    alpha = np.full((size, size, 1), 255, dtype=np.float32)
    return Image.fromarray(np.concatenate([rgb, alpha], axis=2).astype(np.uint8), "RGBA")

KIND_COLOR = {"done": GREEN, "warn": AMBER, "error": RED_B, "info": SLATE, "busy": BLUE_A}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    names = ("YuGothB.ttc", "meiryob.ttc", "seguisb.ttf") if bold else ("YuGothM.ttc", "meiryo.ttc", "segoeui.ttf")
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), size)
        except Exception:
            continue
    return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def kind_from_status(text: str) -> Tuple[str, str]:
    """main からの文（先頭の記号つき）を、カプセルの種類と本文に分ける。"""
    t = (text or "").strip()
    table = (("✓", "done"), ("✕", "error"), ("⚠", "warn"), ("◌", "busy"), ("●", "info"), ("✦", "busy"), ("↓", "busy"), ("↶", "done"))
    for mark, kind in table:
        if t.startswith(mark):
            return kind, t[len(mark):].strip()
    return "info", t


class PillRenderer:
    """拡大率 scale で絵を作る。Win32 に依存しないので、絵だけ作って確かめられる。"""

    SS = 3

    def __init__(self, scale: float, dark: bool = False) -> None:
        self.s = scale
        self.dark = dark
        self.c = THEMES["dark" if dark else "light"]
        self.f_main = _font(max(9, round(13 * scale)), bold=True)
        self.f_sub = _font(max(8, round(12 * scale)))
        self.f_btn = _font(max(8, round(12 * scale)), bold=True)
        self._base_cache: Dict[int, Image.Image] = {}

    def px(self, v: float) -> int:
        return int(round(v * self.s))

    # ------------------------------------------------------------ 大きさ
    def _text_w(self, txt: str, font) -> float:
        return font.getlength(txt) / self.s if txt else 0.0

    @staticmethod
    def _fit(txt: str, font, max_w_px: float, keep_tail: bool) -> str:
        if font.getlength(txt) <= max_w_px:
            return txt
        while txt and font.getlength(("…" + txt) if keep_tail else (txt + "…")) > max_w_px:
            txt = txt[1:] if keep_tail else txt[:-1]
        return ("…" + txt) if keep_tail else (txt + "…")

    def layout(self, st: dict) -> dict:
        """各部分の位置（論理座標、本体の左上が 0,0）と本体の幅を決める。"""
        kind = st["kind"]
        right_w = 0.0
        if kind == "rec":
            right_w = 30.0          # ✕
        elif kind == "done" and st.get("undo"):
            right_w = 8 + self._text_w("取り消す", self.f_btn) + 20
        main, sub = st.get("main", ""), st.get("sub", "")
        x_text = PAD_L + ICON_R * 2 + GAP
        max_text = MAX_W - x_text - right_w - 14
        main_w = min(self._text_w(main, self.f_main), max_text)
        sub_room = max(0.0, max_text - main_w - (8 if main else 0))
        sub_fit = self._fit(sub, self.f_sub, sub_room * self.s, keep_tail=(kind == "rec")) if sub and sub_room > 24 else ""
        sub_w = self._text_w(sub_fit, self.f_sub)
        w = x_text + main_w + ((8 + sub_w) if sub_fit else 0) + right_w + 14
        w = max(MIN_W if kind == "rec" else 0, min(MAX_W, w))
        w = max(w, x_text + 60)
        return {"w": w, "x_text": x_text, "main_w": main_w, "sub": sub_fit, "right_w": right_w}

    # ------------------------------------------------------------ 描画
    def _base(self, w_px: int) -> Image.Image:
        """影・本体・縁（幅ごとに作って使い回す）。"""
        if w_px in self._base_cache:
            return self._base_cache[w_px]
        S = self.SS
        sc = self.s * S
        m, h = MARGIN * sc, H_BODY * sc
        bw = w_px * S
        W, Hh = round(bw + 2 * m), round(h + 2 * m)
        img = Image.new("RGBA", (W, Hh), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((m, m + 4 * sc, m + bw, m + h + 4 * sc), h / 2, fill=self.c["shadow"])
        img = img.filter(ImageFilter.GaussianBlur(8 * sc))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((m, m, m + bw, m + h), h / 2, fill=self.c["body"], outline=self.c["edge"],
                            width=max(1, round(sc)))
        out = img.resize((round(W / S), round(Hh / S)), Image.LANCZOS)
        if len(self._base_cache) > 24:
            self._base_cache.clear()
        self._base_cache[w_px] = out
        return out

    def _icon(self, st: dict, hover: Optional[str]) -> Image.Image:
        """左の丸（3 倍で描いて縮める）。"""
        S = self.SS
        sc = self.s * S
        size = round((ICON_R * 2 + 34) * sc)  # 光が切れないよう広めに
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        c = size / 2
        r = ICON_R * sc
        kind = st["kind"]
        lw = max(1, round(2 * sc))
        if kind == "rec":
            mic_warn = st.get("mic_warn")
            a, b = (RED_A, RED_B) if mic_warn else (BLUE_A, BLUE_B)
            lvl = st.get("level", 0.0)
            rad = r * 0.62  # アプリのアイコンと同じ、角の丸い四角
            # 声に合わせてふわっと広がる光
            glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
            g = (1.5 + 7.5 * lvl) * sc
            ImageDraw.Draw(glow).rounded_rectangle((c - r - g, c - r - g, c + r + g, c + r + g), rad + g,
                                                   fill=b + (int(45 + 95 * lvl),))
            glow = glow.filter(ImageFilter.GaussianBlur(3.0 * sc))
            img = Image.alpha_composite(img, glow)
            grad = _diag_gradient(round(2 * r), a, b, 0.14 if hover == "icon" else 0.0)
            mask = Image.new("L", grad.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, grad.width - 1, grad.height - 1), rad, fill=255)
            img.paste(grad, (round(c - r), round(c - r)), mask)
            d = ImageDraw.Draw(img)
            if hover == "icon":
                q = 4.2 * sc  # ■ 止める
                d.rounded_rectangle((c - q, c - q, c + q, c + q), 1.5 * sc, fill=(255, 255, 255, 255))
            else:
                # マイク
                mw, mh = 3.4 * sc, 5.2 * sc
                d.rounded_rectangle((c - mw, c - mh - 1.6 * sc, c + mw, c + mh - 1.6 * sc), mw, fill=(255, 255, 255, 255))
                d.arc((c - 6.2 * sc, c - 6.5 * sc, c + 6.2 * sc, c + 5.6 * sc), 20, 160, fill=(255, 255, 255, 255), width=lw)
                d.line((c, c + 5.6 * sc, c, c + 8.2 * sc), fill=(255, 255, 255, 255), width=lw)
        elif kind == "busy":
            d.ellipse((c - r, c - r, c + r, c + r), outline=self.c["ring_track"], width=round(2.6 * sc))
            start = (st.get("phase", 0.0) * 360) % 360
            d.arc((c - r, c - r, c + r, c + r), start, start + 100, fill=BLUE_A + (255,), width=round(2.6 * sc))
        else:
            col = KIND_COLOR.get(kind, SLATE)
            d.ellipse((c - r, c - r, c + r, c + r), fill=col + (255,))
            white = (255, 255, 255, 255)
            if kind == "done":
                d.line([(c - 5.4 * sc, c + 0.2 * sc), (c - 1.6 * sc, c + 4 * sc), (c + 5.8 * sc, c - 4.4 * sc)],
                       fill=white, width=round(2.4 * sc), joint="curve")
            elif kind == "error":
                k = 4.6 * sc
                d.line((c - k, c - k, c + k, c + k), fill=white, width=round(2.4 * sc))
                d.line((c - k, c + k, c + k, c - k), fill=white, width=round(2.4 * sc))
            elif kind == "warn":
                d.line((c, c - 5.5 * sc, c, c + 1.5 * sc), fill=white, width=round(2.6 * sc))
                d.ellipse((c - 1.6 * sc, c + 3.6 * sc, c + 1.6 * sc, c + 6.8 * sc), fill=white)
            else:
                d.ellipse((c - 2.6 * sc, c - 2.6 * sc, c + 2.6 * sc, c + 2.6 * sc), fill=white)
        return img.resize((round(size / S), round(size / S)), Image.LANCZOS)

    def _x_button(self, hover: bool) -> Image.Image:
        S = self.SS
        sc = self.s * S
        size = round(24 * sc)
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((0, 0, size - 1, size - 1), fill=self.c["btn_hover"] if hover else self.c["btn"])
        c, k = size / 2, 4 * sc
        col = self.c["text"] if hover else self.c["btn_fg"]
        d.line((c - k, c - k, c + k, c + k), fill=col, width=round(1.8 * sc))
        d.line((c - k, c + k, c + k, c - k), fill=col, width=round(1.8 * sc))
        return img.resize((round(size / S), round(size / S)), Image.LANCZOS)

    def render(self, st: dict, hover: Optional[str] = None) -> Tuple[Image.Image, dict]:
        """絵と、当たり判定（絵の中の実画素座標）を返す。"""
        lay = self.layout(st)
        w = lay["w"]
        img = self._base(self.px(w)).copy()
        m = self.px(MARGIN)
        cy = m + self.px(H_BODY / 2)
        hits: Dict[str, tuple] = {"body": (m, m, m + self.px(w), m + self.px(H_BODY))}
        # 左の丸
        ic = self._icon(st, hover)
        icx = m + self.px(PAD_L + ICON_R)
        img.alpha_composite(ic, (round(icx - ic.width / 2), round(cy - ic.height / 2)))
        if st["kind"] == "rec":
            hits["icon"] = (icx, cy, self.px(ICON_R + 3))
        # 文字
        d = ImageDraw.Draw(img)
        x = m + self.px(lay["x_text"])
        if st.get("main"):
            d.text((x, cy), st["main"], font=self.f_main, fill=self.c["text"], anchor="lm")
            x += self.px(lay["main_w"] + 8)
        if lay["sub"]:
            d.text((x, cy), lay["sub"], font=self.f_sub, fill=self.c["muted"], anchor="lm")
        # 右のボタン
        right = m + self.px(w)
        if st["kind"] == "rec":
            xb = self._x_button(hover == "cancel")
            bx = right - self.px(8) - xb.width
            img.alpha_composite(xb, (bx, round(cy - xb.height / 2)))
            hits["cancel"] = (bx + xb.width / 2, cy, xb.width / 2 + self.px(3))
        elif st["kind"] == "done" and st.get("undo"):
            label = "取り消す"
            tw = self.f_btn.getlength(label)
            bw, bh = tw + self.px(20), self.px(26)
            bx1 = right - self.px(8)
            bx0 = bx1 - bw
            S = self.SS
            btn = Image.new("RGBA", (round(bw * S), round(bh * S)), (0, 0, 0, 0))
            ImageDraw.Draw(btn).rounded_rectangle((0, 0, btn.width - 1, btn.height - 1), 8 * self.s * S,
                                                  fill=self.c["btn_hover"] if hover == "undo" else self.c["btn"])
            btn = btn.resize((round(bw), round(bh)), Image.LANCZOS)
            img.alpha_composite(btn, (round(bx0), round(cy - bh / 2)))
            d = ImageDraw.Draw(img)
            d.text((bx0 + bw / 2, cy), label, font=self.f_btn, fill=self.c["text"], anchor="mm")
            hits["undo"] = ("rect", bx0, cy - bh / 2, bx1, cy + bh / 2)
        return img, hits


# ====================================================================== Win32 の窓
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00080000, 0x08, 0x80, 0x08000000
CS_DBLCLKS = 0x0008
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
WM_TIMER, WM_SETCURSOR, WM_MOUSEACTIVATE = 0x0113, 0x0020, 0x0021
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_APP_CMD = 0x8001
MA_NOACTIVATE = 3
ULW_ALPHA = 2
SWP_NOSIZE, SWP_NOACTIVATE = 0x0001, 0x0010
HWND_TOPMOST = -1
IDC_ARROW, IDC_HAND, IDC_SIZEALL = 32512, 32649, 32646

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.LoadCursorW.restype = wintypes.HANDLE
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class _BLEND(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetCapture.argtypes = [wintypes.HWND]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetCursor.argtypes = [wintypes.HANDLE]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class StatusPill:
    """外から使う口。どのスレッドから呼んでもよい（中で専用スレッドに頼む）。"""

    def __init__(
        self,
        on_finish: Callable[[], None],
        on_cancel: Callable[[], None],
        on_undo: Callable[[], None],
        on_moved: Callable[[Optional[Tuple[int, int]]], None],
    ) -> None:
        self.on_finish, self.on_cancel, self.on_undo, self.on_moved = on_finish, on_cancel, on_undo, on_moved
        self.visible = False
        self._lock = threading.Lock()
        self._state: dict = {"kind": "info", "main": ""}
        self._levels: List[float] = [0.0] * N_LEVELS
        self._shown_level = 0.0
        self._t0 = time.monotonic()
        self._hide_at = 0.0
        self._hover: Optional[str] = None
        self._hits: dict = {}
        self._size = (0, 0)
        self._anchor = (-10000.0, -10000.0)   # 本体の下端の中央（実際の画素）
        self._pos = (-10000, -10000)
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._hwnd = None
        self._ready = threading.Event()
        self._proc = WNDPROC(self._wndproc)  # 参照を保持
        self._press: Optional[Tuple[int, int, float, float]] = None
        self._dragging = False
        self._thread = threading.Thread(target=self._run, name="onseiman-pill", daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        if not self._hwnd:
            raise RuntimeError("状態表示を作れませんでした")

    # ------------------------------------------------------------ 外から
    def nominal_size(self) -> Tuple[int, int]:
        """呼び出し側の座標で、場所を決めるときに使う大きさ（影の余白込み）。"""
        k = self._proc_scale
        return round((NOMINAL_W + 2 * MARGIN) * k), round((H_BODY + 2 * MARGIN) * k)

    def show_recording(self, x: int, y: int, hint: str) -> None:
        with self._lock:
            self._levels = [0.0] * N_LEVELS
            self._t0 = time.monotonic()
            self._state = {"kind": "rec", "main": "0:00", "sub": hint, "hint": hint, "live": "", "mic_warn": False}
            self._hide_at = 0.0
        self._show(x, y)

    def set_live_text(self, text: str) -> None:
        with self._lock:
            if self._state.get("kind") == "rec":
                self._state["live"] = text

    def mic_warn(self) -> None:
        with self._lock:
            if self._state.get("kind") == "rec":
                self._state["mic_warn"] = True
                self._state["hint"] = "マイクの音が来ていません"

    def push_level(self, level: float) -> None:
        with self._lock:
            self._levels = self._levels[1:] + [max(0.0, min(1.0, float(level)))]

    def show_message(self, x: int, y: int, kind: str, main: str, sub: str = "", auto_hide_ms: int = 0,
                     undo: bool = False) -> None:
        with self._lock:
            self._state = {"kind": kind, "main": main, "sub": sub, "undo": undo}
            self._hide_at = time.monotonic() + auto_hide_ms / 1000.0 if auto_hide_ms > 0 else 0.0
        self._show(x, y)

    def hide(self) -> None:
        self.visible = False
        self._post("hide", None)

    def move_to(self, x: int, y: int) -> None:
        """表示したまま場所だけ変える（ダブルクリックで元の場所に戻すとき）。"""
        self._show(x, y)

    def _show(self, x: int, y: int) -> None:
        self.visible = True
        self._post("show", (x, y))

    def _post(self, name: str, arg) -> None:
        self._cmds.put((name, arg))
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_APP_CMD, 0, 0)

    def nominal_position(self) -> Tuple[int, int]:
        """いまの場所を、呼び出し側の座標の「左上（nominal_size のとき）」で返す。"""
        f = self._to_phys
        nw, nh = self.nominal_size()
        return round(self._anchor[0] / f - nw / 2), round(self._anchor[1] / f - nh + MARGIN * self._proc_scale)

    # ------------------------------------------------------------ 窓のスレッド
    def _run(self) -> None:
        proc_dpi = user32.GetDpiForSystem() or 96
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # このスレッドだけ実際の画素で
        real_dpi = user32.GetDpiForSystem() or 96
        self._to_phys = real_dpi / proc_dpi
        self._proc_scale = proc_dpi / 96.0
        self._scale = real_dpi / 96.0
        self._r = PillRenderer(self._scale, windows_is_dark())
        hinst = kernel32.GetModuleHandleW(None)
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.style = CS_DBLCLKS
        cls.lpfnWndProc = self._proc
        cls.hInstance = hinst
        cls.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        cls.lpszClassName = f"OnseiManStatusPill{id(self)}"
        user32.RegisterClassExW(ctypes.byref(cls))
        self._hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            cls.lpszClassName, "OnseiMan", WS_POPUP, -10000, -10000, 10, 10, None, None, hinst, None,
        )
        self._ready.set()
        if not self._hwnd:
            return
        user32.SetTimer(self._hwnd, 1, 50, None)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _handle_cmds(self) -> None:
        while True:
            try:
                name, arg = self._cmds.get_nowait()
            except queue.Empty:
                return
            if name == "show":
                dark = windows_is_dark()
                if dark != self._r.dark:
                    self._r = PillRenderer(self._scale, dark)
                if self._press is None:  # ドラッグ中は場所を変えない
                    x, y = arg
                    f = self._to_phys
                    nw, nh = self.nominal_size()
                    self._anchor = ((x + nw / 2) * f, (y + nh - MARGIN * self._proc_scale) * f)
                self._paint()
                user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            elif name == "hide":
                user32.ShowWindow(self._hwnd, SW_HIDE)
                self._press, self._dragging, self._hover = None, False, None

    def _current_state(self) -> dict:
        with self._lock:
            st = dict(self._state)
            levels = list(self._levels)
        if st.get("kind") == "rec":
            target = max(levels[-3:]) if levels else 0.0
            self._shown_level += (target - self._shown_level) * (0.6 if target > self._shown_level else 0.2)
            st["level"] = self._shown_level
            sec = time.monotonic() - self._t0
            st["main"] = f"{int(sec // 60)}:{int(sec % 60):02d}"
            st["sub"] = st.get("live") or ("マイクの音が来ていません" if st.get("mic_warn") else "")
        elif st.get("kind") == "busy":
            st["phase"] = (time.monotonic() * 1.4) % 1.0
        return st

    def _paint(self) -> None:
        img, hits = self._r.render(self._current_state(), self._hover)
        w, h = img.size
        m = self._r.px(MARGIN)
        x = round(self._anchor[0] - w / 2)
        y = round(self._anchor[1] - (h - m))
        self._pos, self._size, self._hits = (x, y), (w, h), hits
        a = np.asarray(img, dtype=np.uint16)
        al = a[..., 3]
        bgra = np.empty(a.shape, dtype=np.uint8)
        bgra[..., 0] = a[..., 2] * al // 255
        bgra[..., 1] = a[..., 1] * al // 255
        bgra[..., 2] = a[..., 0] * al // 255
        bgra[..., 3] = al
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(screen)
        hbmp = gdi32.CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, bgra.tobytes(), w * h * 4)
            old = gdi32.SelectObject(mem, hbmp)
            blend = BLENDFUNCTION(0, 0, 255, 1)
            user32.UpdateLayeredWindow(self._hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                       ctypes.byref(wintypes.SIZE(w, h)), mem, ctypes.byref(wintypes.POINT(0, 0)),
                                       0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mem, old)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(None, screen)

    def _local(self) -> Tuple[int, int]:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x - self._pos[0], pt.y - self._pos[1]

    def _hit(self, x: int, y: int) -> str:
        for name in ("icon", "cancel", "undo"):
            h = self._hits.get(name)
            if not h:
                continue
            if h[0] == "rect":
                _, l, t, r, b = h
                if l <= x <= r and t <= y <= b:
                    return name
            else:
                cx, cy, r = h
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    return name
        l, t, r, b = self._hits.get("body", (0, 0, 0, 0))
        return "body" if l <= x <= r and t <= y <= b else "none"

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_APP_CMD:
                self._handle_cmds()
                return 0
            if msg == WM_TIMER:
                if self.visible:
                    if self._hide_at and time.monotonic() >= self._hide_at and self._press is None:
                        self.visible = False
                        user32.ShowWindow(hwnd, SW_HIDE)
                        return 0
                    if self._hover and self._press is None and self._hit(*self._local()) != self._hover:
                        self._hover = None
                    self._paint()
                return 0
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_SETCURSOR:
                which = self._hit(*self._local())
                cur = IDC_HAND if which in ("icon", "cancel", "undo") else IDC_SIZEALL
                user32.SetCursor(user32.LoadCursorW(None, ctypes.c_void_p(cur)))
                return 1
            if msg == WM_MOUSEMOVE:
                if self._press is not None:
                    pt = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    px, py, ax, ay = self._press
                    dx, dy = pt.x - px, pt.y - py
                    if self._dragging or abs(dx) + abs(dy) >= 6:
                        self._dragging = True
                        self._anchor = (ax + dx, ay + dy)
                        self._paint()
                else:
                    which = self._hit(*self._local())
                    hv = which if which in ("icon", "cancel", "undo") else None
                    if hv != self._hover:
                        self._hover = hv
                        self._paint()
                return 0
            if msg == WM_LBUTTONDOWN:
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._press = (pt.x, pt.y, self._anchor[0], self._anchor[1])
                self._dragging = False
                user32.SetCapture(hwnd)
                return 0
            if msg == WM_LBUTTONUP:
                user32.ReleaseCapture()
                if self._press is None:
                    return 0
                self._press = None
                if self._dragging:
                    self._dragging = False
                    self._safe(self.on_moved, self.nominal_position())
                    return 0
                which = self._hit(*self._local())
                if which == "icon":
                    self._safe(self.on_finish)
                elif which == "cancel":
                    self._safe(self.on_cancel)
                elif which == "undo":
                    self._safe(self.on_undo)
                return 0
            if msg == WM_LBUTTONDBLCLK:
                if self._hit(*self._local()) == "body":
                    self._safe(self.on_moved, None)
                return 0
        except Exception:
            logger.exception("状態表示の処理で問題がありました")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("状態表示の操作に失敗しました")
