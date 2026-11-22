"""Single-threaded Tkinter UI manager: floating status overlay + copyable history log window."""
from __future__ import annotations

import ctypes
import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from client.logger import HistoryEntry
from client.paste import paste_text, set_clipboard_text
from client.theme import (
    ACCENT,
    BG_APP,
    BG_CARD,
    BG_SUBTLE,
    BORDER,
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_CAPTION,
    FONT_SMALL,
    FONT_TITLE,
    SUCCESS,
    SUCCESS_BG,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    apply_macos_window_chrome,
    configure_macos_theme,
    style_text_widget,
)

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020


logger_ui = __import__("logging").getLogger("onseiman.ui")


class UIController:
    """Runs Tk in its own dedicated thread and accepts thread-safe commands via a queue."""

    def __init__(
        self,
        show_overlay: bool,
        get_history_fn: Callable[[], List[HistoryEntry]],
        on_change_hotkey: Optional[Callable[[], None]] = None,
        on_open_dict: Optional[Callable[[], None]] = None,
        on_open_config: Optional[Callable[[], None]] = None,
        get_status_info: Optional[Callable[[], tuple[str, str]]] = None,
        on_quick_add_dict: Optional[Callable[[str, str, bool], None]] = None,
        on_retranscribe_last: Optional[Callable[[], None]] = None,
        get_learning_candidates_fn: Optional[Callable[[], list]] = None,
        on_approve_candidate: Optional[Callable[[str, str], None]] = None,
        on_reject_candidate: Optional[Callable[[str, str], None]] = None,
        on_learn_from_manual_edit: Optional[Callable[[str, str], int]] = None,
    ) -> None:
        self.show_overlay_enabled = show_overlay
        self.get_history_fn = get_history_fn
        self.on_change_hotkey = on_change_hotkey
        self.on_open_dict = on_open_dict
        self.on_open_config = on_open_config
        self.get_status_info = get_status_info
        self.on_quick_add_dict = on_quick_add_dict
        self.on_retranscribe_last = on_retranscribe_last
        self.get_learning_candidates_fn = get_learning_candidates_fn
        self.on_approve_candidate = on_approve_candidate
        self.on_reject_candidate = on_reject_candidate
        self.on_learn_from_manual_edit = on_learn_from_manual_edit
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

        self._root: Optional[tk.Tk] = None
        self._overlay: Optional[tk.Toplevel] = None
        self._overlay_label: Optional[tk.Label] = None
        self._level_canvas: Optional[tk.Canvas] = None
        self._level_bar = None
        self._hide_after_id: Optional[str] = None

        self._history_win: Optional[tk.Toplevel] = None
        self._history_nb: Optional[ttk.Notebook] = None
        self._history_header_lbl: Optional[tk.Label] = None
        self._history_tree: Optional[ttk.Treeview] = None
        self._history_preview: Optional[tk.Text] = None
        self._history_status: Optional[tk.Label] = None
        self._history_cache: List[HistoryEntry] = []
        self._cand_tree: Optional[ttk.Treeview] = None
        self._cand_tab: Optional[ttk.Frame] = None
        self._cand_cache: list = []
        self._toast_win: Optional[tk.Toplevel] = None
        self._toast_after_id: Optional[str] = None
        # 録音中などの表示を出す基準（入力中の文字カーソルの位置）。None なら画面下の中央
        self._overlay_anchor: Optional[tuple] = None
        # ユーザーがドラッグして決めた表示位置（None ならカーソルの近く）。動かしたら on_overlay_moved で知らせる
        self._overlay_pinned: Optional[tuple] = None
        self.on_overlay_moved: Optional[Callable[[Optional[tuple]], None]] = None
        # 表示の基本の場所: "bottom"（画面下の中央・Typeless と同じ）/ "caret"（カーソルの左上）
        self.overlay_place = "bottom"
        # 録音中の小さなバー（× ・ 音量の点 ・ ✓）
        self._pill = None
        self.on_pill_cancel: Optional[Callable[[], None]] = None
        self.on_pill_finish: Optional[Callable[[], None]] = None
        self.on_pill_undo: Optional[Callable[[], None]] = None
        self._drag: Optional[tuple] = None

        self._editor_args: dict = {}
        self._dict_editor = None
        self._settings_editor = None

    def configure_editors(self, **kwargs) -> None:
        """辞書・設定画面に必要な情報（パス、保存時の処理など）を渡す。"""
        self._editor_args = kwargs

    def open_dict_editor(self) -> None:
        self._queue.put(("dict_edit",))

    def open_settings(self) -> None:
        self._queue.put(("settings_edit",))

    def _open_dict_editor(self) -> None:
        from client.settings_ui import DictionaryEditor, _focus_window

        if self._dict_editor is not None and self._dict_editor.win.winfo_exists():
            _focus_window(self._dict_editor.win)
            return
        a = self._editor_args
        self._dict_editor = DictionaryEditor(self._root, a["dict_path"], on_saved=a.get("on_dict_saved"))

    def _open_settings(self) -> None:
        from client.settings_ui import SettingsEditor, _focus_window

        if self._settings_editor is not None and self._settings_editor.win.winfo_exists():
            _focus_window(self._settings_editor.win)
            return
        a = self._editor_args
        self._settings_editor = SettingsEditor(
            self._root,
            a["config_path"],
            on_saved=a.get("on_config_saved"),
            list_microphones=a.get("list_microphones"),
            get_hotkey=a.get("get_hotkey"),
            on_change_hotkey=self.on_change_hotkey,
            get_ai_hotkey=a.get("get_ai_hotkey"),
            on_change_ai_hotkey=a.get("on_change_ai_hotkey"),
            on_clear_ai_hotkey=a.get("on_clear_ai_hotkey"),
            is_startup_enabled=a.get("is_startup_enabled"),
            set_startup_enabled=a.get("set_startup_enabled"),
        )

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_tk, name="TkUIThread", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def run_mainloop(self, on_ready: Optional[Callable[[], None]] = None) -> None:
        """Run Tk on the caller's thread (recommended: main thread on Windows)."""
        self._root = tk.Tk()
        self._root.withdraw()
        configure_macos_theme(self._root)
        self._create_overlay()
        self._create_pill()
        self._ready.set()
        if on_ready is not None:
            self._root.after(10, on_ready)
        self._poll_queue()
        self._root.mainloop()

    def stop(self) -> None:
        self._queue.put(("quit",))

    def show_status(self, text: str, bg: str = "#1f2937", auto_hide_ms: int = 0) -> None:
        if not self.show_overlay_enabled:
            return
        self._queue.put(("overlay_show", text, bg, auto_hide_ms))

    def update_level(self, level: float) -> None:
        if not self.show_overlay_enabled:
            return
        self._queue.put(("overlay_level", level))

    def set_overlay_anchor(self, rect: Optional[tuple], how: str = "") -> None:
        """録音中などの表示を、この位置（入力中のカーソル）の近くに出す。None なら画面下の中央。"""
        self._queue.put(("overlay_anchor", rect, how))

    def show_pill(self) -> None:
        """録音中の小さなバーを出す。"""
        if self.show_overlay_enabled:
            self._queue.put(("pill_show",))

    def hide_pill(self) -> None:
        self._queue.put(("pill_hide",))

    def pill_warn(self) -> None:
        self._queue.put(("pill_warn",))

    def pill_live_text(self, text: str) -> None:
        """録音中に、話しながら認識できた文をカプセルに薄く出す。"""
        self._queue.put(("pill_live", text))

    def show_done(self, text: str, undo: bool = True) -> None:
        """入力が終わったことを、入力した文と「取り消す」ボタンつきで知らせる。"""
        if self.show_overlay_enabled:
            self._queue.put(("pill_done", text, undo))

    def set_overlay_place(self, place: str) -> None:
        self._queue.put(("overlay_place", "caret" if place == "caret" else "bottom"))

    def set_overlay_pinned(self, pos: Optional[tuple]) -> None:
        """表示をこの位置（x, y）に固定する。None ならカーソルの近くに出す。"""
        self._queue.put(("overlay_pin", tuple(pos) if pos else None))

    def hide_overlay(self) -> None:
        self._queue.put(("overlay_hide",))

    def open_history_window(self) -> None:
        self._queue.put(("history_open",))

    def refresh_history_if_open(self) -> None:
        self._queue.put(("history_refresh",))

    def show_learning_toast(self, src: str, dst: str) -> None:
        """入力中のカーソルの近くに、フォーカスを奪わない「辞書に登録しますか？」カードを出す。
        位置はここ（呼び出し側のスレッド）で調べる。取れなければ画面右下。"""
        try:
            from client.caret import get_caret_rect

            rect, how = get_caret_rect()
        except Exception:
            rect, how = None, ""
        self._queue.put(("learning_toast", src, dst, rect, how))

    def _run_tk(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()
        configure_macos_theme(self._root)
        self._create_overlay()
        self._create_pill()
        self._ready.set()
        self._poll_queue()
        self._root.mainloop()

    def _create_overlay(self) -> None:
        assert self._root is not None
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92)
        win.configure(bg="#1f2937")

        frame = tk.Frame(win, bg="#1f2937", padx=14, pady=8)
        frame.pack(fill="both", expand=True)

        lbl = tk.Label(
            frame,
            text="",
            fg="#f9fafb",
            bg="#1f2937",
            font=("Meiryo UI", 10, "bold"),
        )
        lbl.pack(side="top", anchor="w")

        canvas = tk.Canvas(frame, width=220, height=5, bg="#374151", highlightthickness=0)
        canvas.pack(side="top", fill="x", pady=(5, 0))
        bar = canvas.create_rectangle(0, 0, 0, 5, fill="#38bdf8", width=0)

        win.update_idletasks()
        # Make overlay non-activating and click-through on Windows
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            if hwnd:
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd,
                    GWL_EXSTYLE,
                    style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                )
        except Exception:
            pass

        # ドラッグで動かせる（動かした場所を覚えて、次からそこに出す）。ダブルクリックでカーソルの近くに戻す
        for wdg in (win, frame, lbl, canvas):
            wdg.bind("<ButtonPress-1>", self._overlay_drag_start)
            wdg.bind("<B1-Motion>", self._overlay_drag_move)
            wdg.bind("<ButtonRelease-1>", self._overlay_drag_end)
            wdg.bind("<Double-Button-1>", self._overlay_unpin)
            wdg.configure(cursor="fleur")

        # Show it once now and never withdraw/deiconify again: re-mapping a Tk window activates the
        # Tk app and can bring the OnseiMan main window to the front, stealing the user's caret.
        # Hiding is done by making it fully transparent and moving it off-screen instead.
        win.attributes("-alpha", 0.0)
        win.geometry("+-10000+-10000")
        self._overlay = win
        self._overlay_label = lbl
        self._level_canvas = canvas
        self._level_bar = bar

    def _create_pill(self) -> None:
        """状態表示のカプセル（client/pill.py）。作れなければ従来の文字の表示を使う。"""
        from client.pill import StatusPill

        def call(name: str):
            def fn() -> None:
                cb = getattr(self, name, None)
                if cb:
                    cb()
            return fn

        def moved(pos) -> None:  # カプセルのスレッドから来るので、Tk のスレッドで処理する
            self._queue.put(("pill_moved", pos))

        try:
            self._pill = StatusPill(call("on_pill_finish"), call("on_pill_cancel"), call("on_pill_undo"), moved)
        except Exception:
            logger_ui.exception("状態表示のカプセルを作れませんでした（文字の表示を使います）")
            self._pill = None

    def _pill_xy(self) -> tuple:
        w, h = self._pill.nominal_size()
        return self._home_xy(w, h)

    def _home_xy(self, w: int, h: int) -> tuple:
        """表示を出す場所（左上の座標）。ドラッグで決めた場所 → カーソルの左上（設定時）→ 画面下の中央。"""
        assert self._root is not None
        if self._overlay_pinned is not None:
            px, py = self._overlay_pinned
            try:
                from client.caret import point_on_screen

                if point_on_screen(px, py) and point_on_screen(px + w - 1, py):
                    return int(px), int(py)
            except Exception:
                pass
        anchor = self._overlay_anchor
        if self.overlay_place == "caret" and anchor is not None:
            try:
                from client.caret import place_near

                rect, how = anchor
                return place_near(rect, w, h, how, gap=12)
            except Exception:
                pass
        # 画面下の中央（入力中のカーソルがある画面の、タスクバーのすぐ上）
        try:
            from client.caret import work_area_at

            if anchor is not None:
                ax, ay = anchor[0][0], anchor[0][1]
            else:
                ax, ay = self._root.winfo_pointerx(), self._root.winfo_pointery()
            wl, wt, wr, wb = work_area_at(ax, ay)
            return int((wl + wr - w) // 2), int(wb - h - 14)
        except Exception:
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            return (sw - w) // 2, sh - h - 76

    def _overlay_drag_start(self, event) -> None:
        if not self._overlay or not self._root:
            return
        if self._hide_after_id:  # 動かしている間は消さない
            self._root.after_cancel(self._hide_after_id)
            self._hide_after_id = None
        self._drag = (event.x_root - self._overlay.winfo_x(), event.y_root - self._overlay.winfo_y(), False)

    def _overlay_drag_move(self, event) -> None:
        if not self._overlay or self._drag is None:
            return
        dx, dy, _ = self._drag
        self._drag = (dx, dy, True)
        self._overlay.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _overlay_drag_end(self, _event) -> None:
        if not self._overlay or not self._root or self._drag is None:
            return
        moved = self._drag[2]
        self._drag = None
        if moved:
            pos = (int(self._overlay.winfo_x()), int(self._overlay.winfo_y()))
            self._overlay_pinned = pos
            if self.on_overlay_moved:
                try:
                    self.on_overlay_moved(pos)
                except Exception:
                    pass
        if not self._hide_after_id:
            self._hide_after_id = self._root.after(1500, self._hide_overlay_now)

    def _overlay_unpin(self, _event=None) -> None:
        self._overlay_pinned = None
        if self.on_overlay_moved:
            try:
                self.on_overlay_moved(None)
            except Exception:
                pass
        self._position_overlay()

    def _hide_overlay_now(self) -> None:
        """Hide without unmapping (see _create_overlay): transparent + off-screen, never activates."""
        self._hide_after_id = None
        if self._overlay is not None:
            self._overlay.attributes("-alpha", 0.0)
            self._overlay.geometry("+-10000+-10000")

    def _position_overlay(self) -> None:
        if not self._overlay or not self._root:
            return
        self._overlay.update_idletasks()
        w = self._overlay.winfo_reqwidth()
        h = self._overlay.winfo_reqheight()
        x, y = self._home_xy(w, h)
        # バーと同じ場所に出す（バーより横長でも、中心をそろえる）
        if self._pill is not None:
            pw, ph = self._pill.nominal_size()
            bx, by = self._home_xy(pw, ph)
            x = bx + (pw - w) // 2
            y = by + ph - h
        self._overlay.geometry(f"+{x}+{y}")

    def _open_history(self) -> None:
        assert self._root is not None
        if self._history_win is not None and self._history_win.winfo_exists():
            self._populate_history()
            self._history_win.deiconify()
            self._history_win.lift()
            self._history_win.focus_force()
            return

        win = tk.Toplevel(self._root)
        win.title("OnseiMan")
        win.geometry("840x580")
        win.minsize(620, 420)
        win.configure(bg=BG_APP)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        apply_macos_window_chrome(win)

        # macOS 統合ツールバー風ヘッダー
        header = tk.Frame(win, bg=BG_APP, padx=18, pady=12)
        header.pack(fill="x")

        title_row = tk.Frame(header, bg=BG_APP)
        title_row.pack(side="left", fill="y")

        tk.Label(
            title_row,
            text="OnseiMan",
            bg=BG_APP,
            fg=TEXT_PRIMARY,
            font=FONT_TITLE,
        ).pack(side="left", padx=(0, 10))

        header_lbl = tk.Label(
            title_row,
            text="",
            bg=SUCCESS_BG,
            fg="#1E7E34",
            font=FONT_CAPTION,
            padx=10,
            pady=3,
        )
        header_lbl.pack(side="left")

        ttk.Button(header, text="設定", command=self._open_settings).pack(side="right", padx=(6, 0))
        ttk.Button(header, text="辞書", command=self._open_dict_editor).pack(side="right", padx=(6, 0))

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=16, pady=(0, 14))

        hist_tab = tk.Frame(nb, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
        nb.add(hist_tab, text="  認識履歴  ")

        top = tk.Frame(hist_tab, bg=BG_CARD, padx=12, pady=12)
        top.pack(fill="both", expand=True)

        columns = ("time", "backend", "text")
        tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="browse")
        tree.heading("time", text="日時")
        tree.heading("backend", text="処理")
        tree.heading("text", text="認識テキスト")
        tree.column("time", width=135, stretch=False)
        tree.column("backend", width=125, stretch=False)
        tree.column("text", width=500, stretch=True)

        sb = ttk.Scrollbar(top, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bottom = tk.Frame(hist_tab, bg=BG_CARD, padx=12, pady=10)
        bottom.pack(fill="x")

        raw_lbl = tk.Label(
            bottom,
            text="元の認識結果（誤認識した語をなぞって「辞書に登録」できます）",
            bg=BG_CARD,
            fg=TEXT_SECONDARY,
            font=FONT_CAPTION,
            anchor="w",
        )
        raw_lbl.pack(fill="x")
        raw_preview = tk.Text(bottom, height=2, wrap="word")
        style_text_widget(raw_preview, subtle=True)
        raw_preview.pack(fill="x", pady=(3, 8))

        out_lbl = tk.Label(
            bottom,
            text="入力された文字（辞書・変換後）",
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            font=FONT_SMALL,
            anchor="w",
        )
        out_lbl.pack(fill="x")
        preview = tk.Text(bottom, height=3, wrap="word")
        style_text_widget(preview, subtle=False)
        preview.pack(fill="x", pady=(3, 10))

        btn_row = tk.Frame(bottom, bg=BG_CARD)
        btn_row.pack(fill="x")

        status_lbl = tk.Label(btn_row, text="行をダブルクリックでコピー", bg=BG_CARD, fg=TEXT_SECONDARY, font=FONT_SMALL)
        status_lbl.pack(side="left")

        def get_selected_entry() -> Optional[HistoryEntry]:
            sel = tree.selection()
            if not sel:
                return None
            idx = int(sel[0])
            if 0 <= idx < len(self._history_cache):
                return self._history_cache[idx]
            return None

        def get_selected_text() -> str:
            entry = get_selected_entry()
            return entry.text if entry is not None else ""

        def on_select(_event=None) -> None:
            entry = get_selected_entry()
            raw_txt = entry.raw_text if entry is not None else ""
            out_txt = entry.text if entry is not None else ""
            raw_preview.delete("1.0", "end")
            raw_preview.insert("1.0", raw_txt)
            preview.delete("1.0", "end")
            preview.insert("1.0", out_txt)
            if entry is not None and raw_txt != out_txt:
                out_lbl.config(text="入力された文字（辞書・変換後 ★変換あり）:", fg="#047857")
            else:
                out_lbl.config(text="入力された文字（辞書・変換後）:", fg="#111827")

        def copy_selected(_event=None) -> None:
            txt = preview.get("1.0", "end-1c").strip() or get_selected_text()
            if not txt:
                return
            set_clipboard_text(txt)
            status_lbl.config(text="クリップボードにコピーしました！", fg="#059669")

        def refresh_clicked() -> None:
            self._populate_history()
            status_lbl.config(text="更新しました", fg="#4b5563")

        def quick_add_dict_clicked() -> None:
            if not self.on_quick_add_dict:
                return
            initial_src = ""
            for box in (raw_preview, preview):
                try:
                    picked = box.get("sel.first", "sel.last").strip()
                    if picked:
                        initial_src = picked
                        break
                except tk.TclError:
                    pass
            if not initial_src:
                entry = get_selected_entry()
                initial_src = (entry.raw_text if entry is not None else "").strip()
            self._open_quick_dict_dialog(win, initial_src, status_lbl)

        def retranscribe_clicked() -> None:
            if not self.on_retranscribe_last:
                return
            status_lbl.config(text="直前の音声を最新辞書で再認識しています...", fg="#4c1d95")
            threading.Thread(target=self.on_retranscribe_last, daemon=True).start()

        def learn_edit_clicked() -> None:
            if not self.on_learn_from_manual_edit:
                return
            entry = get_selected_entry()
            if entry is None:
                return
            edited = preview.get("1.0", "end-1c").strip()
            count = self.on_learn_from_manual_edit(entry.text, edited)
            if count > 0:
                status_lbl.config(text=f"修正から {count} 件の学習候補を追加しました（「学習候補」タブで確認）", fg="#059669")
                self._populate_history()
            else:
                status_lbl.config(text="学習できる差分が見つかりませんでした（下の欄を書き換えてから押してください）", fg="#4b5563")

        tree.bind("<<TreeviewSelect>>", on_select)
        tree.bind("<Double-1>", copy_selected)

        ttk.Button(btn_row, text="コピー", style="Accent.TButton", command=copy_selected).pack(side="right", padx=(6, 0))
        if self.on_quick_add_dict:
            ttk.Button(btn_row, text="辞書に登録", command=quick_add_dict_clicked).pack(side="right", padx=(6, 0))
        if self.on_learn_from_manual_edit:
            ttk.Button(btn_row, text="修正を学習", command=learn_edit_clicked).pack(side="right", padx=(6, 0))
        if self.on_retranscribe_last:
            ttk.Button(btn_row, text="再認識", command=retranscribe_clicked).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="更新", command=refresh_clicked).pack(side="right")

        # ==================== タブ2: 学習候補（半自動） ====================
        cand_tab = tk.Frame(nb, bg=BG_CARD, highlightbackground=BORDER, highlightthickness=1)
        nb.add(cand_tab, text="  学習候補  ")
        tk.Label(
            cand_tab,
            text="貼り付け後に手で直した言葉を自動で見つけて並べます。ダブルクリックまたは「辞書に登録」で即反映します。",
            bg=BG_CARD,
            fg=TEXT_SECONDARY,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", padx=14, pady=(12, 6))

        c_body = tk.Frame(cand_tab, bg=BG_CARD, padx=12, pady=4)
        c_body.pack(fill="both", expand=True)
        c_tree = ttk.Treeview(c_body, columns=("src", "dst", "count", "example"), show="headings", selectmode="extended")
        c_tree.heading("src", text="元の言葉")
        c_tree.heading("dst", text="修正後（正しい表記）")
        c_tree.heading("count", text="回数")
        c_tree.heading("example", text="修正例")
        c_tree.column("src", width=150, stretch=False)
        c_tree.column("dst", width=160, stretch=False)
        c_tree.column("count", width=55, anchor="center", stretch=False)
        c_tree.column("example", width=380, stretch=True)
        c_sb = ttk.Scrollbar(c_body, orient="vertical", command=c_tree.yview)
        c_tree.configure(yscrollcommand=c_sb.set)
        c_tree.pack(side="left", fill="both", expand=True)
        c_sb.pack(side="right", fill="y")

        c_btns = tk.Frame(cand_tab, bg=BG_CARD, padx=12, pady=12)
        c_btns.pack(fill="x")
        c_status = tk.Label(c_btns, text="", bg=BG_CARD, fg=TEXT_SECONDARY, font=FONT_SMALL)
        c_status.pack(side="left")

        def approve_selected(_event=None) -> None:
            if not self.on_approve_candidate:
                return
            sel = c_tree.selection()
            if not sel:
                return
            done = 0
            for iid in sel:
                src, dst, _cnt, _ex = c_tree.item(iid, "values")
                self.on_approve_candidate(str(src), str(dst))
                done += 1
            c_status.config(text=f"{done} 件を辞書に登録しました（即反映されます）", fg="#059669")
            self._populate_history()

        def reject_selected() -> None:
            if not self.on_reject_candidate:
                return
            sel = c_tree.selection()
            if not sel:
                return
            done = 0
            for iid in sel:
                src, dst, _cnt, _ex = c_tree.item(iid, "values")
                self.on_reject_candidate(str(src), str(dst))
                done += 1
            c_status.config(text=f"{done} 件を却下しました（今後提案しません）", fg="#4b5563")
            self._populate_history()

        c_tree.bind("<Double-1>", approve_selected)
        ttk.Button(c_btns, text="辞書に登録（優先語にも追加）", style="Accent.TButton", command=approve_selected).pack(side="right", padx=(6, 0))
        ttk.Button(c_btns, text="却下（今後出さない）", command=reject_selected).pack(side="right")

        self._history_win = win
        self._history_nb = nb
        self._history_header_lbl = header_lbl
        self._history_tree = tree
        self._history_preview = preview
        self._history_status = status_lbl
        self._cand_tree = c_tree
        self._cand_tab = cand_tab
        self._populate_history()
        win.lift()
        win.attributes("-topmost", True)
        self._root.after(150, lambda: win.winfo_exists() and win.attributes("-topmost", False))

    def _open_quick_dict_dialog(self, parent: tk.Toplevel, initial_src: str, status_lbl: tk.Label) -> None:
        dlg = tk.Toplevel(parent)
        dlg.title("辞書に1クリック登録")
        dlg.geometry("460x210")
        dlg.resizable(False, False)
        dlg.transient(parent)
        dlg.grab_set()

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="誤認識された言葉（プレビューで選択すると自動入力されます）:").pack(anchor="w")
        src_entry = ttk.Entry(body, font=("Meiryo UI", 10))
        src_entry.pack(fill="x", pady=(2, 8))
        src_entry.insert(0, initial_src)

        ttk.Label(body, text="正しい表記（置き換え後）:").pack(anchor="w")
        dst_entry = ttk.Entry(body, font=("Meiryo UI", 10))
        dst_entry.pack(fill="x", pady=(2, 8))

        hot_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(body, text="優先して認識する言葉（hotwords）にも追加", variable=hot_var).pack(anchor="w")

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))

        def do_save(_event=None) -> None:
            src = src_entry.get().strip()
            dst = dst_entry.get().strip()
            if not src or not dst:
                return
            try:
                assert self.on_quick_add_dict is not None
                self.on_quick_add_dict(src, dst, bool(hot_var.get()))
                status_lbl.config(text=f"辞書に登録しました: {src} → {dst}", fg="#059669")
                dlg.destroy()
            except Exception as exc:
                status_lbl.config(text=f"登録失敗: {exc}", fg="#dc2626")

        ttk.Button(btns, text="キャンセル", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="登録", command=do_save).pack(side="right", padx=(0, 6))
        dst_entry.bind("<Return>", do_save)
        (dst_entry if initial_src else src_entry).focus_set()

    def _populate_history(self) -> None:
        if self._history_header_lbl is not None and self.get_status_info is not None:
            backend, hotkey = self.get_status_info()
            self._history_header_lbl.config(text=f"● {backend}   ·   キー: {hotkey}")
        if self._history_tree is None or not self._history_tree.winfo_exists():
            return
        for item in self._history_tree.get_children():
            self._history_tree.delete(item)
        self._history_cache = self.get_history_fn()
        for idx, entry in enumerate(self._history_cache):
            one_line = entry.text.replace("\r", " ").replace("\n", " ")
            self._history_tree.insert(
                "",
                "end",
                iid=str(idx),
                values=(entry.timestamp, f"{entry.backend} ({entry.elapsed_ms:.0f}ms)", one_line),
            )

        if self._cand_tree is not None and self._cand_tree.winfo_exists() and self.get_learning_candidates_fn is not None:
            for item in self._cand_tree.get_children():
                self._cand_tree.delete(item)
            self._cand_cache = self.get_learning_candidates_fn()
            for idx, c in enumerate(self._cand_cache):
                ex = ""
                if c.example_before and c.example_after:
                    ex = f"{c.example_before[:24]} → {c.example_after[:24]}"
                self._cand_tree.insert("", "end", iid=str(idx), values=(c.src, c.dst, c.count, ex))
            if self._history_nb is not None and self._cand_tab is not None:
                n = len(self._cand_cache)
                self._history_nb.tab(self._cand_tab, text=f"学習候補 ({n})" if n > 0 else "学習候補")

    def _poll_queue(self) -> None:
        assert self._root is not None
        try:
            while True:
                item = self._queue.get_nowait()
                cmd = item[0]
                if cmd == "quit":
                    self._root.quit()
                    return
                elif cmd == "overlay_show" and self._pill is not None:
                    from client.pill import kind_from_status

                    _, text, bg, auto_hide_ms = item
                    first, _, rest = str(text).partition("\n")
                    kind, main = kind_from_status(first)
                    self._pill.show_message(*self._pill_xy(), kind, main, rest.strip(), auto_hide_ms)
                elif cmd == "overlay_show" and self._overlay and self._overlay_label:
                    _, text, bg, auto_hide_ms = item
                    if self._hide_after_id:
                        self._root.after_cancel(self._hide_after_id)
                        self._hide_after_id = None
                    self._overlay.configure(bg=bg)
                    self._overlay_label.master.configure(bg=bg)
                    self._overlay_label.configure(text=text, bg=bg)
                    if self._level_canvas and self._level_bar:
                        self._level_canvas.coords(self._level_bar, 0, 0, 0, 5)
                    self._position_overlay()
                    self._overlay.attributes("-alpha", 0.92)
                    if auto_hide_ms > 0:
                        self._hide_after_id = self._root.after(auto_hide_ms, self._hide_overlay_now)
                elif cmd == "overlay_level" and self._pill is not None:
                    self._pill.push_level(float(item[1]))
                elif cmd == "overlay_level" and self._level_canvas and self._level_bar:
                    level = max(0.0, min(1.0, float(item[1])))
                    self._level_canvas.coords(self._level_bar, 0, 0, int(220 * level), 5)
                elif cmd == "overlay_hide" and self._overlay:
                    self._hide_overlay_now()
                    if self._pill is not None:
                        self._pill.hide()
                elif cmd == "dict_edit":
                    self._open_dict_editor()
                elif cmd == "settings_edit":
                    self._open_settings()
                elif cmd == "history_open":
                    self._open_history()
                elif cmd == "history_refresh":
                    if self._history_win is not None and self._history_win.winfo_exists():
                        self._populate_history()
                elif cmd == "learning_toast":
                    _, src, dst, rect, how = item
                    self._show_learning_toast_now(str(src), str(dst), rect, how)
                elif cmd == "pill_show" and self._pill is not None:
                    self._hide_overlay_now()
                    self._pill.show_recording(*self._pill_xy(), "")
                elif cmd == "pill_hide" and self._pill is not None:
                    self._pill.hide()
                elif cmd == "pill_warn" and self._pill is not None:
                    self._pill.mic_warn()
                elif cmd == "pill_live" and self._pill is not None:
                    self._pill.set_live_text(item[1])
                elif cmd == "pill_done" and self._pill is not None:
                    _, text, undo = item
                    self._pill.show_message(*self._pill_xy(), "done", text, "", 3200, undo=undo)
                elif cmd == "pill_moved":
                    pos = item[1]
                    self._overlay_pinned = pos
                    if self.on_overlay_moved:
                        try:
                            self.on_overlay_moved(pos)
                        except Exception:
                            pass
                    if pos is None and self._pill is not None and self._pill.visible:
                        self._pill.move_to(*self._pill_xy())
                elif cmd == "overlay_place":
                    self.overlay_place = item[1]
                elif cmd == "overlay_pin":
                    self._overlay_pinned = item[1]
                elif cmd == "overlay_anchor":
                    _, rect, how = item
                    self._overlay_anchor = (rect, how) if rect else None
        except queue.Empty:
            pass

        self._root.after(30, self._poll_queue)

    def _dismiss_learning_toast(self) -> None:
        if self._toast_after_id and self._root is not None:
            try:
                self._root.after_cancel(self._toast_after_id)
            except Exception:
                pass
            self._toast_after_id = None
        if self._toast_win is not None:
            try:
                self._toast_win.destroy()
            except Exception:
                pass
            self._toast_win = None

    def _show_learning_toast_now(self, src: str, dst: str, rect: Optional[tuple] = None, how: str = "") -> None:
        assert self._root is not None
        self._dismiss_learning_toast()

        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.96)
        win.configure(bg="#0f172a")

        frame = tk.Frame(win, bg="#0f172a", padx=14, pady=10, highlightbackground="#38bdf8", highlightthickness=1)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="💡 辞書に登録しますか？",
            fg="#38bdf8",
            bg="#0f172a",
            font=("Meiryo UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x")

        tk.Label(
            frame,
            text=f"{src}  →  {dst}",
            fg="#f8fafc",
            bg="#0f172a",
            font=("Meiryo UI", 10, "bold"),
            anchor="w",
            wraplength=340,
        ).pack(fill="x", pady=(4, 8))

        btns = tk.Frame(frame, bg="#0f172a")
        btns.pack(fill="x")

        def on_yes() -> None:
            if self.on_approve_candidate:
                self.on_approve_candidate(src, dst)
            self._dismiss_learning_toast()
            if self._history_win is not None and self._history_win.winfo_exists():
                self._populate_history()

        def on_no() -> None:
            if self.on_reject_candidate:
                self.on_reject_candidate(src, dst)
            self._dismiss_learning_toast()
            if self._history_win is not None and self._history_win.winfo_exists():
                self._populate_history()

        tk.Button(
            btns,
            text="登録する",
            command=on_yes,
            bg="#059669",
            fg="white",
            activebackground="#047857",
            activeforeground="white",
            relief="flat",
            padx=10,
            pady=3,
            font=("Meiryo UI", 9, "bold"),
            cursor="hand2",
        ).pack(side="left")

        tk.Button(
            btns,
            text="あとで",
            command=self._dismiss_learning_toast,  # 消すのはこの場だけ。同じ直しを見つけたらまた聞く
            bg="#334155",
            fg="#e2e8f0",
            activebackground="#475569",
            activeforeground="white",
            relief="flat",
            padx=8,
            pady=3,
            font=("Meiryo UI", 9),
            cursor="hand2",
        ).pack(side="left", padx=(6, 0))

        tk.Button(
            btns,
            text="×",
            command=self._dismiss_learning_toast,
            bg="#0f172a",
            fg="#94a3b8",
            activebackground="#1e293b",
            activeforeground="white",
            relief="flat",
            padx=6,
            pady=2,
            font=("Meiryo UI", 9),
            cursor="hand2",
        ).pack(side="right")

        win.update_idletasks()
        w = win.winfo_reqwidth()
        h = win.winfo_reqheight()
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x, y = sw - w - 20, sh - h - 68
        if rect:
            try:
                from client.caret import place_near

                x, y = place_near(rect, w, h, how)
            except Exception:
                pass
        win.geometry(f"+{x}+{y}")

        # フォーカスを奪わない（クリックは可能にするため WS_EX_TRANSPARENT は付けない）
        try:
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            if hwnd:
                style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    hwnd,
                    GWL_EXSTYLE,
                    style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                )
        except Exception:
            pass

        self._toast_win = win
        self._toast_after_id = self._root.after(9000, self._dismiss_learning_toast)
