"""はじめて起動したときに、聞き取りの部品（speech.cpp と Haas-ja、約 0.7GB）を用意する画面。

取ってくる手順そのものは shared/speechcpp_setup.py（版と確認の値を固定。途中で止まっても続きから）。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from pathlib import Path

from shared import speechcpp_setup

logger = logging.getLogger("onseiman.model_setup")

APPROX_MB = 690


def run_setup_window(root: Path) -> bool:
    """進み具合の画面を出して部品を用意する。用意できたら True。閉じた・失敗したら False。"""
    import tkinter as tk
    from tkinter import ttk

    # 2 つ同時に用意しないように
    ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW(None, False, "Local\\OnseiManModelSetup")
    if ctypes.get_last_error() == 183:
        return False

    result = {"ok": False}
    cancel = threading.Event()
    win = tk.Tk()
    win.title("OnseiMan の準備")
    win.geometry("460x220")
    win.resizable(False, False)
    win.configure(padx=22, pady=18)
    try:
        win.attributes("-topmost", True)
        win.after(1500, lambda: win.attributes("-topmost", False))
    except Exception:
        pass

    tk.Label(win, text="声を文字にするための部品をダウンロードします", font=("Meiryo UI", 11, "bold"), anchor="w").pack(fill="x")
    tk.Label(
        win,
        text=f"最初の一度だけです（約 {APPROX_MB}MB）。途中で閉じても、次回は続きから再開します。",
        font=("Meiryo UI", 9), fg="#555", anchor="w", justify="left", wraplength=410,
    ).pack(fill="x", pady=(4, 12))
    bar = ttk.Progressbar(win, length=410, mode="determinate", maximum=1000)
    bar.pack(fill="x")
    status = tk.Label(win, text="接続しています…", font=("Meiryo UI", 9), anchor="w", justify="left")
    status.pack(fill="x", pady=(8, 0))
    btns = tk.Frame(win)
    btns.pack(fill="x", side="bottom")
    retry_btn = ttk.Button(btns, text="もう一度試す")
    close_btn = ttk.Button(btns, text="やめる")
    close_btn.pack(side="right")
    speed = {"label": "", "t0": 0.0, "first": 0}

    def ui(fn) -> None:
        try:
            win.after(0, fn)
        except Exception:
            pass   # 画面がもう閉じている

    def on_progress(label: str, done: int, total: int) -> None:
        def upd() -> None:
            if label != speed["label"]:
                speed.update(label=label, t0=time.time(), first=done)
            if total > 0:
                bar["value"] = done * 1000 // total
            el = max(time.time() - speed["t0"], 0.001)
            rate = (done - speed["first"]) / el
            remain = (total - done) / rate if rate > 0 and total > 0 else 0
            rtxt = f"／あと約 {int(remain // 60)}分{int(remain % 60):02d}秒" if rate > 0 and el > 2 else ""
            size = f"{done / 1048576:,.0f} / {total / 1048576:,.0f} MB" if total > 0 else f"{done / 1048576:,.0f} MB"
            status.configure(text=f"{label}\n{size}{rtxt}")

        ui(upd)

    def worker() -> None:
        try:
            if not speechcpp_setup.ensure(root, progress=on_progress, cancel=cancel):
                raise RuntimeError("このパソコンでは使えません（Windows の 64 ビット版が要ります）")
            result["ok"] = True
            ui(win.destroy)
        except Exception as exc:
            if cancel.is_set():
                ui(win.destroy)
                return
            logger.exception("聞き取りの部品を用意できませんでした")
            msg = str(exc)

            def show_err() -> None:
                status.configure(text=f"失敗しました: {msg[:80]}\nインターネット接続を確認して、もう一度お試しください。", fg="#b91c1c")
                retry_btn.pack(side="right", padx=(0, 8))

            ui(show_err)

    def start() -> None:
        retry_btn.pack_forget()
        status.configure(text="接続しています…", fg="black")
        threading.Thread(target=worker, daemon=True).start()

    def on_close() -> None:
        cancel.set()
        win.after(300, win.destroy)

    retry_btn.configure(command=start)
    close_btn.configure(command=on_close)
    win.protocol("WM_DELETE_WINDOW", on_close)
    win.after(200, start)
    win.mainloop()
    return result["ok"]


def show_message(title: str, text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
