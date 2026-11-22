"""macOS 風のクリーンなライトテーマ（Tkinter / ttk 共通スタイル）。"""
from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import ttk

# Apple macOS Sonoma 風カラーパレット
BG_APP = "#F5F5F7"
BG_CARD = "#FFFFFF"
BG_SUBTLE = "#EFEFF4"
BORDER = "#E5E5EA"
BORDER_STRONG = "#D1D1D6"
TEXT_PRIMARY = "#1D1D1F"
TEXT_SECONDARY = "#6E6E73"
ACCENT = "#007AFF"
ACCENT_HOVER = "#0063D1"
SUCCESS = "#28A745"
SUCCESS_BG = "#EAF8EE"
DANGER = "#FF3B30"
ROW_ALT = "#FAFAFC"
ROW_SELECT = "#E8F2FF"

FONT_FAMILY = "Yu Gothic UI"
FONT_TITLE = (FONT_FAMILY, 11, "bold")
FONT_BODY = (FONT_FAMILY, 10)
FONT_BODY_BOLD = (FONT_FAMILY, 10, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_CAPTION = (FONT_FAMILY, 8)


def apply_macos_window_chrome(win: tk.Misc) -> None:
    """Windows 11 のタイトルバー色を背景 (#F5F5F7) と一体化させる（非対応環境では無視）。"""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        # DWMWA_CAPTION_COLOR = 35, COLORREF is 0x00BBGGRR for #F5F5F7 -> 0x00F7F5F5
        color = ctypes.c_int(0x00F7F5F5)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
        # DWMWA_TEXT_COLOR = 36, #1D1D1F -> 0x001F1D1D
        tcolor = ctypes.c_int(0x001F1D1D)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(tcolor), ctypes.sizeof(tcolor))
    except Exception:
        pass


def configure_macos_theme(root: tk.Misc) -> None:
    """Tk アプリ全体に macOS 風の ttk スタイルを適用する。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(".", background=BG_APP, foreground=TEXT_PRIMARY, font=FONT_BODY)
    style.configure("TFrame", background=BG_APP)
    style.configure("Card.TFrame", background=BG_CARD)
    style.configure("TLabel", background=BG_APP, foreground=TEXT_PRIMARY, font=FONT_BODY)
    style.configure("Card.TLabel", background=BG_CARD, foreground=TEXT_PRIMARY, font=FONT_BODY)
    style.configure("Muted.TLabel", background=BG_APP, foreground=TEXT_SECONDARY, font=FONT_SMALL)

    # LabelFrame をカード風に
    style.configure(
        "TLabelframe",
        background=BG_CARD,
        bordercolor=BORDER,
        borderwidth=1,
        relief="solid",
    )
    style.configure(
        "TLabelframe.Label",
        background=BG_CARD,
        foreground=TEXT_PRIMARY,
        font=FONT_BODY_BOLD,
    )

    # Notebook (セグメント風すっきりタブ)
    style.configure("TNotebook", background=BG_APP, borderwidth=0, tabmargins=[0, 4, 0, 0])
    style.configure(
        "TNotebook.Tab",
        background=BG_SUBTLE,
        foreground=TEXT_SECONDARY,
        padding=[16, 7],
        borderwidth=0,
        font=FONT_SMALL,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", BG_CARD), ("active", "#E5E5EA")],
        foreground=[("selected", TEXT_PRIMARY)],
        font=[("selected", (FONT_FAMILY, 9, "bold"))],
    )

    # Secondary button (白ベース＋繊細な枠)
    style.configure(
        "TButton",
        background=BG_CARD,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER_STRONG,
        lightcolor=BG_CARD,
        darkcolor=BG_CARD,
        borderwidth=1,
        relief="solid",
        padding=[12, 5],
        font=FONT_SMALL,
    )
    style.map(
        "TButton",
        background=[("active", "#EFEFF4"), ("pressed", "#E5E5EA")],
        bordercolor=[("active", "#B8B8C0")],
    )

    # Primary button (macOS System Blue)
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground="#FFFFFF",
        bordercolor=ACCENT,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        borderwidth=1,
        relief="flat",
        padding=[14, 5],
        font=(FONT_FAMILY, 9, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_HOVER), ("pressed", "#0051B3")],
        foreground=[("active", "#FFFFFF")],
    )

    # Checkbutton / Radiobutton
    style.configure("TCheckbutton", background=BG_CARD, foreground=TEXT_PRIMARY, font=FONT_BODY)
    style.map("TCheckbutton", background=[("active", BG_CARD)])
    style.configure("TRadiobutton", background=BG_CARD, foreground=TEXT_PRIMARY, font=FONT_BODY)
    style.map("TRadiobutton", background=[("active", BG_CARD)])

    # Entry / Combobox / Spinbox
    style.configure(
        "TEntry",
        fieldbackground=BG_CARD,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        padding=5,
    )
    style.configure(
        "TCombobox",
        fieldbackground=BG_CARD,
        background=BG_CARD,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER_STRONG,
        padding=4,
    )

    # Treeview (ゆったりした行高＋控えめなヘッダー)
    style.configure(
        "Treeview",
        background=BG_CARD,
        fieldbackground=BG_CARD,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        borderwidth=1,
        rowheight=32,
        font=FONT_BODY,
    )
    style.configure(
        "Treeview.Heading",
        background=BG_SUBTLE,
        foreground=TEXT_SECONDARY,
        bordercolor=BORDER,
        relief="flat",
        padding=[10, 7],
        font=(FONT_FAMILY, 9, "bold"),
    )
    style.map(
        "Treeview",
        background=[("selected", ROW_SELECT)],
        foreground=[("selected", TEXT_PRIMARY)],
    )
    style.map("Treeview.Heading", background=[("active", "#E5E5EA")])

    # Scrollbar (スリム)
    style.configure(
        "Vertical.TScrollbar",
        background="#D1D1D6",
        troughcolor=BG_CARD,
        bordercolor=BG_CARD,
        arrowcolor=TEXT_SECONDARY,
        relief="flat",
        width=10,
    )


def style_text_widget(widget: tk.Text, *, subtle: bool = False) -> None:
    """tk.Text を macOS カード風の枠と余白に整える。"""
    bg = BG_SUBTLE if subtle else BG_CARD
    fg = TEXT_SECONDARY if subtle else TEXT_PRIMARY
    widget.configure(
        bg=bg,
        fg=fg,
        insertbackground=ACCENT,
        selectbackground="#CCE4FF",
        selectforeground=TEXT_PRIMARY,
        relief="flat",
        highlightthickness=1,
        highlightbackground=BORDER,
        highlightcolor=ACCENT,
        padx=10,
        pady=7,
        font=FONT_SMALL if subtle else FONT_BODY,
    )
