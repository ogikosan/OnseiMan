"""アプリ内の「辞書」「設定」編集画面（Tk）。JSON をメモ帳で直接触らせないためのもの。

どちらの画面も Tk スレッド上で開く（UIController のキュー経由）。保存すると dict.json /
config.json を書き換え、on_saved を呼ぶ。辞書は保存時刻で自動再読み込みされる。
"""
from __future__ import annotations

import json
import logging
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, List, Optional

from client.config import ClientConfig
from client.theme import (
    BG_APP,
    BG_CARD,
    BORDER,
    FONT_BODY,
    FONT_SMALL,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    apply_macos_window_chrome,
    configure_macos_theme,
    style_text_widget,
)
from shared.fillers import DEFAULT_FILLERS
from shared.normalize import BUILTIN_TERMS

logger = logging.getLogger("onseiman.settings_ui")

FONT = FONT_BODY
HINT_FG = TEXT_SECONDARY
DEFAULT_MIC_LABEL = "Windows の既定のマイク"
CANCEL_KEY_CHOICES = ["esc", "f1", "f2", "f3", "f4", "f8", "f9", "f10", "f12", "pause", "scroll_lock"]


def _focus_window(win: tk.Toplevel) -> None:
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.after(150, lambda: win.winfo_exists() and win.attributes("-topmost", False))
    win.focus_force()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class _WordListTab:
    """1列の言葉リスト（優先語・言いよどみ用）。"""

    def __init__(self, parent: ttk.Frame, hint: str, words: List[str], placeholder: str) -> None:
        tk.Label(parent, text=hint, font=FONT_SMALL, fg=HINT_FG, justify="left", anchor="w").pack(
            fill="x", padx=10, pady=(10, 6)
        )
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=10)
        self.entry = ttk.Entry(row, font=FONT)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self.add())
        ttk.Button(row, text="追加", command=self.add).pack(side="left", padx=(6, 0))
        self._placeholder = placeholder

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        self.listbox = tk.Listbox(
            body,
            font=FONT,
            activestyle="none",
            selectmode="extended",
            bg=BG_CARD,
            fg=TEXT_PRIMARY,
            selectbackground="#007AFF",
            selectforeground="#FFFFFF",
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor="#007AFF",
            bd=0,
        )
        sb = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.listbox.bind("<Delete>", lambda _e: self.remove())

        btns = ttk.Frame(parent)
        btns.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(btns, text="選んだ言葉を削除", command=self.remove).pack(side="left")
        self.extra_buttons = btns
        self.set_words(words)

    def set_words(self, words: List[str]) -> None:
        self.listbox.delete(0, "end")
        for w in words:
            self.listbox.insert("end", w)

    def words(self) -> List[str]:
        return list(self.listbox.get(0, "end"))

    def add(self) -> None:
        w = self.entry.get().strip()
        if not w:
            return
        if w not in self.words():
            self.listbox.insert("end", w)
            self.listbox.see("end")
        self.entry.delete(0, "end")

    def remove(self) -> None:
        for idx in reversed(self.listbox.curselection()):
            self.listbox.delete(idx)


class DictionaryEditor:
    def __init__(self, root: tk.Tk, dict_path: Path, on_saved: Optional[Callable[[], None]] = None) -> None:
        self.path = dict_path
        self.on_saved = on_saved
        data = _read_json(dict_path)
        self._data = data

        win = tk.Toplevel(root)
        win.title("OnseiMan — 辞書")
        win.geometry("660x560")
        win.minsize(520, 440)
        win.configure(bg=BG_APP)
        configure_macos_theme(root)
        apply_macos_window_chrome(win)
        self.win = win

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        # --- 置き換え
        rep_tab = ttk.Frame(nb)
        nb.add(rep_tab, text="置き換え")
        tk.Label(
            rep_tab,
            text="認識結果の中の言葉を、決まった書き方に置き換えます。例: 「コンフィーUI」→「ComfyUI」\n"
            "左を読み（かな）で書くと、カタカナで認識されたときも置き換わります。例: 「すーぱーべーす」→「Supabase」\n"
            "一覧の行をダブルクリックすると、下の欄で直せます。",
            font=FONT_SMALL, fg=HINT_FG, justify="left", anchor="w",
        ).pack(fill="x", padx=10, pady=(10, 6))
        body = ttk.Frame(rep_tab)
        body.pack(fill="both", expand=True, padx=10)
        tree = ttk.Treeview(body, columns=("src", "dst"), show="headings", selectmode="extended")
        tree.heading("src", text="認識された言葉")
        tree.heading("dst", text="置き換え後")
        tree.column("src", width=260)
        tree.column("dst", width=260)
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for src, dst in (data.get("replacements") or {}).items():
            tree.insert("", "end", values=(src, dst))
        self.tree = tree

        form = ttk.Frame(rep_tab)
        form.pack(fill="x", padx=10, pady=8)
        self.src_entry = ttk.Entry(form, font=FONT, width=22)
        self.dst_entry = ttk.Entry(form, font=FONT, width=22)
        self.src_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(form, text=" → ").pack(side="left")
        self.dst_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(form, text="追加・更新", command=self._add_replacement).pack(side="left", padx=(6, 0))
        self.dst_entry.bind("<Return>", lambda _e: self._add_replacement())
        rbtns = ttk.Frame(rep_tab)
        rbtns.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(rbtns, text="選んだ行を削除", command=self._remove_replacement).pack(side="left")
        tree.bind("<Double-1>", self._load_replacement)
        tree.bind("<Delete>", lambda _e: self._remove_replacement())

        # ためし変換プレビュー（いま登録中の置き換えがどう効くか即確認できる）
        test_box = ttk.LabelFrame(rep_tab, text="ためし変換（ここに文章を入れると、今のルールでどう変わるか確認できます）")
        test_box.pack(fill="x", padx=10, pady=(0, 8))
        trow = ttk.Frame(test_box)
        trow.pack(fill="x", padx=8, pady=6)
        self.test_input = ttk.Entry(trow, font=FONT)
        self.test_input.pack(side="left", fill="x", expand=True)
        ttk.Label(trow, text=" → ").pack(side="left")
        self.test_output = tk.Label(trow, text="（ここに結果が出ます）", font=FONT, fg="#047857", anchor="w")
        self.test_output.pack(side="left", fill="x", expand=True)
        self.test_input.bind("<KeyRelease>", lambda _e: self._update_test_preview())

        # --- 優先して認識する言葉
        hot_tab = ttk.Frame(nb)
        nb.add(hot_tab, text="優先して認識する言葉")
        self.hot = _WordListTab(
            hot_tab,
            "専門用語や固有名詞など、認識されにくい言葉を登録すると、その言葉として認識されやすくなります。",
            [str(w) for w in data.get("hotwords") or []],
            "例: ComfyUI",
        )

        # --- 言いよどみ
        fil_tab = ttk.Frame(nb)
        nb.add(fil_tab, text="言いよどみ")
        fillers = data.get("fillers")
        self.fil = _WordListTab(
            fil_tab,
            "認識結果から取り除く「えーと」「うん」などです。「、」「。」で区切られた単独のものだけ消え、\n"
            "「あの人」のように言葉の一部になっているものは残ります。（設定画面で機能ごと止められます）",
            [str(w) for w in fillers] if isinstance(fillers, list) else list(DEFAULT_FILLERS),
            "例: えーと",
        )
        ttk.Button(
            self.fil.extra_buttons, text="初期の一覧に戻す", command=lambda: self.fil.set_words(list(DEFAULT_FILLERS))
        ).pack(side="left", padx=(6, 0))

        # --- 標準の変換
        std_tab = ttk.Frame(nb)
        nb.add(std_tab, text="標準の変換")
        tk.Label(
            std_tab,
            text="最初から入っている変換です（設定画面でまとめて止められます）。\n"
            "・漢数字を数字に: 五千九十→5090、二十歳→20歳、五万三千円→5万3000円、三点五パーセント→3.5%\n"
            "　（一人・一度・十分(じゅうぶん)・八百屋・千葉・万が一 などはそのまま）\n"
            "・全角の英数字を半角に　・下のカタカナ略語を英字に（人名などで困るものは「使わない」に）",
            font=FONT_SMALL, fg=HINT_FG, justify="left", anchor="w",
        ).pack(fill="x", padx=10, pady=(10, 6))
        sbody = ttk.Frame(std_tab)
        sbody.pack(fill="both", expand=True, padx=10)
        stree = ttk.Treeview(sbody, columns=("src", "dst", "use"), show="headings", selectmode="extended")
        stree.heading("src", text="認識された言葉")
        stree.heading("dst", text="変換後")
        stree.heading("use", text="使う")
        stree.column("src", width=220)
        stree.column("dst", width=200)
        stree.column("use", width=80, anchor="center")
        ssb = ttk.Scrollbar(sbody, orient="vertical", command=stree.yview)
        stree.configure(yscrollcommand=ssb.set)
        stree.pack(side="left", fill="both", expand=True)
        ssb.pack(side="right", fill="y")
        off = set(data.get("builtin_off") or [])
        for src, dst in BUILTIN_TERMS.items():
            stree.insert("", "end", iid=src, values=(src, dst, "使わない" if src in off else "使う"))
        self.std_tree = stree
        sbtns = ttk.Frame(std_tab)
        sbtns.pack(fill="x", padx=10, pady=8)
        ttk.Button(sbtns, text="選んだものを 使う／使わない に切り替え", command=self._toggle_builtin).pack(side="left")
        stree.bind("<Double-1>", lambda _e: self._toggle_builtin())

        # --- 下のボタン
        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=8, pady=8)
        self.status = tk.Label(bottom, text="", font=FONT_SMALL, fg=HINT_FG)
        self.status.pack(side="left")
        ttk.Button(bottom, text="閉じる", command=self._close).pack(side="right")
        ttk.Button(bottom, text="保存", style="Accent.TButton", command=self.save).pack(side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", self._close)
        win.bind("<Control-s>", lambda _e: self.save())
        _focus_window(win)

    def _add_replacement(self) -> None:
        src = self.src_entry.get().strip()
        dst = self.dst_entry.get().strip()
        if not src:
            return
        for item in self.tree.get_children():
            if self.tree.set(item, "src") == src:
                self.tree.item(item, values=(src, dst))
                break
        else:
            self.tree.insert("", "end", values=(src, dst))
        self.src_entry.delete(0, "end")
        self.dst_entry.delete(0, "end")
        self.src_entry.focus_set()
        self._update_test_preview()

    def _update_test_preview(self) -> None:
        if not hasattr(self, "test_input"):
            return
        sample = self.test_input.get()
        if not sample:
            self.test_output.config(text="（ここに結果が出ます）", fg=HINT_FG)
            return
        from shared.kana import is_kana_only, to_katakana
        import re

        pairs = [
            (str(self.tree.set(i, "src")), str(self.tree.set(i, "dst")))
            for i in self.tree.get_children()
            if str(self.tree.set(i, "src")) != ""
        ]
        pairs.sort(key=lambda kv: len(kv[0]), reverse=True)
        result = sample
        for src, dst in pairs:
            has_hiragana = any("\u3041" <= c <= "\u3096" for c in src)
            if is_kana_only(src) and has_hiragana:
                kata = to_katakana(src)
                result = re.sub(rf"(?<![ァ-ヺー]){re.escape(kata)}(?![ァ-ヺー])", lambda _m, d=dst: d, result)
                if len(src) >= 3:
                    result = result.replace(src, dst)
            else:
                result = result.replace(src, dst)
        self.test_output.config(text=result, fg="#047857" if result != sample else "#111827")

    def _load_replacement(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        src, dst = self.tree.item(sel[0], "values")
        self.src_entry.delete(0, "end")
        self.src_entry.insert(0, src)
        self.dst_entry.delete(0, "end")
        self.dst_entry.insert(0, dst)

    def _toggle_builtin(self) -> None:
        for iid in self.std_tree.selection():
            src, dst, use = self.std_tree.item(iid, "values")
            self.std_tree.item(iid, values=(src, dst, "使う" if use == "使わない" else "使わない"))

    def _remove_replacement(self) -> None:
        for item in self.tree.selection():
            self.tree.delete(item)
        self._update_test_preview()

    def collect(self) -> dict:
        data = dict(self._data)
        data["hotwords"] = self.hot.words()
        data["replacements"] = {
            str(self.tree.set(i, "src")): str(self.tree.set(i, "dst")) for i in self.tree.get_children()
        }
        data["fillers"] = self.fil.words()
        data["builtin_off"] = [
            iid for iid in self.std_tree.get_children() if self.std_tree.set(iid, "use") == "使わない"
        ]
        return data

    def save(self) -> bool:
        try:
            data = self.collect()
            _write_json(self.path, data)
            self._data = data
        except Exception as exc:
            logger.exception("辞書の保存に失敗しました")
            messagebox.showerror("OnseiMan", f"辞書を保存できませんでした:\n{exc}", parent=self.win)
            return False
        self.status.config(text="保存しました（すぐに反映されます）", fg="#047857")
        if self.on_saved:
            self.on_saved()
        return True

    def _close(self) -> None:
        if self.collect() != self._data:
            ans = messagebox.askyesnocancel("OnseiMan", "変更を保存しますか？", parent=self.win)
            if ans is None:
                return
            if ans and not self.save():
                return
        self.win.destroy()


class SettingsEditor:
    def __init__(
        self,
        root: tk.Tk,
        config_path: Path,
        on_saved: Optional[Callable[[], None]] = None,
        list_microphones: Optional[Callable[[], List[str]]] = None,
        get_hotkey: Optional[Callable[[], str]] = None,
        on_change_hotkey: Optional[Callable[[], None]] = None,
        get_ai_hotkey: Optional[Callable[[], str]] = None,
        on_change_ai_hotkey: Optional[Callable[[], None]] = None,
        on_clear_ai_hotkey: Optional[Callable[[], None]] = None,
        is_startup_enabled: Optional[Callable[[], bool]] = None,
        set_startup_enabled: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self.path = config_path
        self.on_saved = on_saved
        self.get_hotkey = get_hotkey
        self.get_ai_hotkey = get_ai_hotkey
        self._set_startup_enabled = set_startup_enabled
        cfg = ClientConfig.load(config_path)

        win = tk.Toplevel(root)
        win.title("OnseiMan — 設定")
        win.geometry("640x580")
        win.minsize(520, 500)
        win.configure(bg=BG_APP)
        configure_macos_theme(root)
        apply_macos_window_chrome(win)
        self.win = win
        pad = {"padx": 12, "pady": 4}

        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        # ==================== タブ1: 基本・録音 ====================
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="基本・録音")

        g1 = ttk.LabelFrame(tab1, text="録音の操作")
        g1.pack(fill="x", padx=10, pady=(10, 4))
        row = ttk.Frame(g1)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="録音キー", width=14).pack(side="left")
        self.hotkey_lbl = ttk.Label(row, text=cfg.hotkey, font=("Meiryo UI", 10, "bold"))
        self.hotkey_lbl.pack(side="left")
        if on_change_hotkey:
            ttk.Button(row, text="変更（次に押したキー）", command=on_change_hotkey).pack(side="right")

        self.mode = tk.StringVar(value="hold" if str(cfg.hotkey_mode).lower() == "hold" else "toggle")
        row = ttk.Frame(g1)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="操作方式", width=14).pack(side="left")
        ttk.Radiobutton(row, text="1回押して開始、もう1回で確定", variable=self.mode, value="toggle").pack(side="left")
        row = ttk.Frame(g1)
        row.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Label(row, text="", width=14).pack(side="left")
        ttk.Radiobutton(row, text="押している間だけ録音", variable=self.mode, value="hold").pack(side="left")

        self.auto_stop_on_silence = tk.BooleanVar(value=bool(cfg.auto_stop_on_silence))
        self.silence_stop_sec = tk.StringVar(value=str(cfg.silence_stop_sec))
        row = ttk.Frame(g1)
        row.pack(fill="x", **pad)
        ttk.Checkbutton(
            row,
            text="話し終わって無音が続いたら自動で確定（トグル時）",
            variable=self.auto_stop_on_silence,
        ).pack(side="left")
        ttk.Spinbox(row, from_=0.5, to=10.0, increment=0.2, textvariable=self.silence_stop_sec, width=5, font=FONT).pack(
            side="left", padx=(6, 2)
        )
        ttk.Label(row, text="秒", foreground=HINT_FG).pack(side="left")

        row = ttk.Frame(g1)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="キャンセルキー", width=14).pack(side="left")
        self.cancel_key = ttk.Combobox(row, values=CANCEL_KEY_CHOICES, width=12, font=FONT)
        self.cancel_key.set(cfg.cancel_key)
        self.cancel_key.pack(side="left")

        row = ttk.Frame(g1)
        row.pack(fill="x", padx=12, pady=(4, 8))
        ttk.Label(row, text="最長の録音", width=14).pack(side="left")
        self.max_sec = tk.StringVar(value=str(int(cfg.max_duration_sec)))
        ttk.Spinbox(row, from_=10, to=600, increment=10, textvariable=self.max_sec, width=6, font=FONT).pack(side="left")
        ttk.Label(row, text=" 秒（押し忘れたら自動で確定）", foreground=HINT_FG).pack(side="left")

        g2 = ttk.LabelFrame(tab1, text="マイク")
        g2.pack(fill="x", padx=10, pady=4)
        mics = [DEFAULT_MIC_LABEL] + (list_microphones() if list_microphones else [])
        current = cfg.microphone if isinstance(cfg.microphone, str) and cfg.microphone else DEFAULT_MIC_LABEL
        if current not in mics:
            mics.append(current)
        self.mic = ttk.Combobox(g2, values=mics, state="readonly", font=FONT)
        self.mic.set(current)
        self.mic.pack(fill="x", padx=12, pady=8)
        self._mic_original = cfg.microphone

        g_boot = ttk.LabelFrame(tab1, text="起動")
        g_boot.pack(fill="x", padx=10, pady=4)
        init_startup = bool(is_startup_enabled()) if is_startup_enabled else False
        self.startup_enabled = tk.BooleanVar(value=init_startup)
        ttk.Checkbutton(
            g_boot,
            text="Windows 起動時に OnseiMan を自動で常駐させる",
            variable=self.startup_enabled,
        ).pack(anchor="w", padx=12, pady=6)

        # ==================== タブ2: 入力の仕上げ ====================
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="入力の仕上げ")

        g3 = ttk.LabelFrame(tab2, text="テキストの自動整形・動作")
        g3.pack(fill="x", padx=10, pady=10)
        self.remove_fillers = tk.BooleanVar(value=bool(cfg.remove_fillers))
        self.normalize_text = tk.BooleanVar(value=bool(cfg.normalize_text))
        self.voice_commands = tk.BooleanVar(value=bool(cfg.voice_commands))
        self.strip_trailing_period = tk.BooleanVar(value=bool(cfg.strip_trailing_period))
        self.app_specific_rules = tk.BooleanVar(value=bool(cfg.app_specific_rules))
        self.learn_from_edits = tk.BooleanVar(value=bool(cfg.learn_from_edits))
        self.auto_learn_after_count = tk.StringVar(value=str(int(cfg.auto_learn_after_count)))
        self.restore_clipboard = tk.BooleanVar(value=bool(cfg.restore_clipboard))
        self.beep = tk.BooleanVar(value=bool(cfg.beep_enabled))
        self.overlay = tk.BooleanVar(value=bool(cfg.show_overlay))
        for var, text in (
            (self.remove_fillers, "「えーと」「うん」などの言いよどみを取り除く"),
            (self.normalize_text, "数字や略語を読みやすく（五千九十→5090、ピーディーエフ→PDF）"),
            (self.voice_commands, "「かいぎょう」「改行」を実際の改行にする（改行コード等はそのまま）"),
            (self.strip_trailing_period, "文末の「。」を自動でつけない（全アプリで削る）"),
            (self.app_specific_rules, "チャットアプリ（Discord / Slack / LINE 等）で文末の「。」を自動削除"),
            (self.learn_from_edits, "貼り付け後に手で直した言葉を感知して「学習候補」に出す（半自動）"),
            (self.restore_clipboard, "貼り付けのあと、クリップボードを元の内容に戻す"),
            (self.beep, "録音の開始・終了で音を鳴らす"),
            (self.overlay, "録音中・認識中の表示を画面下に出す"),
        ):
            ttk.Checkbutton(g3, text=text, variable=var).pack(anchor="w", padx=12, pady=2)

        lrow = ttk.Frame(g3)
        lrow.pack(fill="x", padx=28, pady=(2, 8))
        ttk.Label(lrow, text="同じ修正を", foreground=HINT_FG).pack(side="left")
        ttk.Spinbox(lrow, from_=0, to=10, increment=1, textvariable=self.auto_learn_after_count, width=4, font=FONT).pack(
            side="left", padx=4
        )
        ttk.Label(lrow, text="回したら自動で辞書登録（0＝常に手動承認のみ）", foreground=HINT_FG).pack(side="left")

        # ==================== タブ3: サーバー・AI整形 ====================
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text="サーバー・AI整形")

        g4 = ttk.LabelFrame(tab3, text="認識サーバー（上から順に試します。1行に1つ）")
        g4.pack(fill="x", padx=10, pady=(10, 4))
        self.servers = tk.Text(g4, height=4, font=FONT, wrap="none")
        style_text_widget(self.servers)
        self.servers.insert("1.0", "\n".join(cfg.server_urls))
        self.servers.pack(fill="x", padx=12, pady=(8, 4))
        self.local_fallback = tk.BooleanVar(value=bool(cfg.local_fallback))
        ttk.Checkbutton(
            g4, text="サーバーにつながらないときは、このPCで認識する", variable=self.local_fallback
        ).pack(anchor="w", padx=12, pady=(0, 8))

        g5 = ttk.LabelFrame(tab3, text="AI 整形・選択テキスト書き換え（第2キー）")
        g5.pack(fill="x", padx=10, pady=6)
        tk.Label(
            g5,
            text="未選択で使うと言い直しを整理した文章に整形、テキストを選択した状態で使うと音声指示で書き換えます。",
            font=FONT_SMALL, fg=HINT_FG, justify="left", anchor="w", wraplength=540,
        ).pack(fill="x", padx=12, pady=(6, 4))

        row = ttk.Frame(g5)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="AI 整形キー", width=14).pack(side="left")
        self.ai_hotkey_lbl = ttk.Label(row, text=cfg.ai_hotkey or "（未設定）", font=("Meiryo UI", 10, "bold"))
        self.ai_hotkey_lbl.pack(side="left")
        if on_clear_ai_hotkey:
            ttk.Button(row, text="解除", command=on_clear_ai_hotkey).pack(side="right", padx=(4, 0))
        if on_change_ai_hotkey:
            ttk.Button(row, text="変更（次に押したキー）", command=on_change_ai_hotkey).pack(side="right")

        row = ttk.Frame(g5)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="LLM URL", width=14).pack(side="left")
        self.llm_base_url = ttk.Entry(row, font=FONT)
        self.llm_base_url.insert(0, cfg.llm_base_url)
        self.llm_base_url.pack(side="left", fill="x", expand=True)

        row = ttk.Frame(g5)
        row.pack(fill="x", padx=12, pady=(4, 8))
        ttk.Label(row, text="モデル名", width=14).pack(side="left")
        self.llm_model = ttk.Entry(row, font=FONT)
        self.llm_model.insert(0, cfg.llm_model)
        self.llm_model.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text="（空欄で自動選択）", foreground=HINT_FG).pack(side="left", padx=(6, 0))

        bottom = ttk.Frame(win)
        bottom.pack(fill="x", padx=10, pady=10)
        self.status = tk.Label(bottom, text="", font=FONT_SMALL, fg=HINT_FG)
        self.status.pack(side="left")
        ttk.Button(bottom, text="閉じる", command=self._close).pack(side="right")
        ttk.Button(bottom, text="保存", style="Accent.TButton", command=self.save).pack(side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", self._close)
        win.bind("<Control-s>", lambda _e: self.save())

        self._saved_snapshot = self._snapshot()
        self._poll_hotkey()
        _focus_window(win)

    def _poll_hotkey(self) -> None:
        if not self.win.winfo_exists():
            return
        if self.get_hotkey is not None:
            self.hotkey_lbl.config(text=self.get_hotkey())
        if self.get_ai_hotkey is not None:
            self.ai_hotkey_lbl.config(text=self.get_ai_hotkey() or "（未設定）")
        self.win.after(500, self._poll_hotkey)

    def _snapshot(self) -> tuple:
        return (
            self.mode.get(),
            self.auto_stop_on_silence.get(),
            self.silence_stop_sec.get().strip(),
            self.cancel_key.get().strip(),
            self.max_sec.get().strip(),
            self.mic.get(),
            self.startup_enabled.get(),
            self.remove_fillers.get(),
            self.normalize_text.get(),
            self.voice_commands.get(),
            self.strip_trailing_period.get(),
            self.app_specific_rules.get(),
            self.learn_from_edits.get(),
            self.auto_learn_after_count.get().strip(),
            self.restore_clipboard.get(),
            self.beep.get(),
            self.overlay.get(),
            self.servers.get("1.0", "end").strip(),
            self.local_fallback.get(),
            self.llm_base_url.get().strip(),
            self.llm_model.get().strip(),
        )

    def apply_to(self, cfg: ClientConfig) -> ClientConfig:
        """画面の値を cfg に書き込む（録音キーは別操作で保存されるので触らない）。"""
        from client.input_hook import parse_binding

        cancel = self.cancel_key.get().strip() or "esc"
        parse_binding(cancel)  # 不正なら ValueError
        try:
            max_sec = float(self.max_sec.get())
        except ValueError:
            raise ValueError("最長の録音は数字で入力してください")
        if not 5 <= max_sec <= 3600:
            raise ValueError("最長の録音は 5〜3600 秒にしてください")
        try:
            silence_sec = float(self.silence_stop_sec.get())
        except ValueError:
            raise ValueError("無音自動確定の秒数は数字で入力してください")
        if not 0.3 <= silence_sec <= 30.0:
            raise ValueError("無音自動確定は 0.3〜30 秒にしてください")
        urls = [u.strip() for u in self.servers.get("1.0", "end").splitlines() if u.strip()]
        for u in urls:
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"サーバーの書き方が正しくありません: {u}\n（例: http://192.168.1.50:8765）")
        llm_url = self.llm_base_url.get().strip()
        if llm_url and not llm_url.startswith(("http://", "https://")):
            raise ValueError(f"LLM URL の書き方が正しくありません: {llm_url}\n（例: http://192.168.1.50:11434）")

        cfg.hotkey_mode = self.mode.get()
        cfg.auto_stop_on_silence = bool(self.auto_stop_on_silence.get())
        cfg.silence_stop_sec = silence_sec
        cfg.cancel_key = cancel
        cfg.max_duration_sec = max_sec
        mic = self.mic.get()
        cfg.microphone = None if mic == DEFAULT_MIC_LABEL else mic
        cfg.remove_fillers = bool(self.remove_fillers.get())
        cfg.normalize_text = bool(self.normalize_text.get())
        cfg.voice_commands = bool(self.voice_commands.get())
        cfg.strip_trailing_period = bool(self.strip_trailing_period.get())
        cfg.app_specific_rules = bool(self.app_specific_rules.get())
        cfg.learn_from_edits = bool(self.learn_from_edits.get())
        try:
            auto_cnt = int(self.auto_learn_after_count.get())
        except ValueError:
            raise ValueError("自動辞書登録の回数は整数で入力してください")
        if not 0 <= auto_cnt <= 20:
            raise ValueError("自動辞書登録の回数は 0〜20 の範囲にしてください")
        cfg.auto_learn_after_count = auto_cnt
        cfg.restore_clipboard = bool(self.restore_clipboard.get())
        cfg.beep_enabled = bool(self.beep.get())
        cfg.show_overlay = bool(self.overlay.get())
        cfg.server_urls = urls
        cfg.local_fallback = bool(self.local_fallback.get())
        cfg.llm_base_url = llm_url
        cfg.llm_model = self.llm_model.get().strip()
        return cfg

    def save(self) -> bool:
        try:
            cfg = self.apply_to(ClientConfig.load(self.path))  # 録音キーなど他の値は最新のファイルから
            cfg.save(self.path)
            if self._set_startup_enabled is not None:
                self._set_startup_enabled(bool(self.startup_enabled.get()))
        except ValueError as exc:
            messagebox.showwarning("OnseiMan", str(exc), parent=self.win)
            return False
        except Exception as exc:
            logger.exception("設定の保存に失敗しました")
            messagebox.showerror("OnseiMan", f"設定を保存できませんでした:\n{exc}", parent=self.win)
            return False
        self._saved_snapshot = self._snapshot()
        self.status.config(text="保存しました（すぐに反映されます）", fg="#047857")
        if self.on_saved:
            self.on_saved()
        return True

    def _close(self) -> None:
        if self._snapshot() != self._saved_snapshot:
            ans = messagebox.askyesnocancel("OnseiMan", "変更を保存しますか？", parent=self.win)
            if ans is None:
                return
            if ans and not self.save():
                return
        self.win.destroy()
