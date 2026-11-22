"""初回起動時に、音声認識モデルをダウンロードする画面。

- 約 840MB。途中で止まっても、次回は続きからダウンロードする。
- 展開は一時フォルダで行い、全部そろってから正式な場所へ移す（途中で落ちても壊れたモデルが残らない）。
"""
from __future__ import annotations

import ctypes
import logging
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("onseiman.model_setup")

MODEL_NAME = "sherpa-onnx-funasr-nano-int8-2025-12-30"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_NAME}.tar.bz2"
)
REQUIRED_FILES = ("encoder_adaptor.int8.onnx", "llm.int8.onnx", "embedding.int8.onnx")
APPROX_MB = 840


def model_ready(model_dir: Path) -> bool:
    return all((model_dir / f).exists() for f in REQUIRED_FILES)


def download_and_extract(
    models_root: Path,
    on_progress: Callable[[int, int], None],
    on_phase: Callable[[str], None],
    cancel: threading.Event,
    url: str = MODEL_URL,
) -> Path:
    """モデルを models_root/MODEL_NAME に用意する。cancel が立ったら InterruptedError。"""
    models_root.mkdir(parents=True, exist_ok=True)
    final_dir = models_root / MODEL_NAME
    if model_ready(final_dir):
        return final_dir
    part = models_root / f"{MODEL_NAME}.tar.bz2.part"
    archive = models_root / f"{MODEL_NAME}.tar.bz2"

    if not archive.exists():
        on_phase("download")
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"User-Agent": "OnseiMan-setup"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            resp_cm = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:  # 前回すでに全部受け取っていた
                part.replace(archive)
                return download_and_extract(models_root, on_progress, on_phase, cancel, url)
            raise
        with resp_cm as resp:
            if have and resp.status != 206:
                have = 0  # 続きからに対応していない返事: 最初から
            total = have + int(resp.headers.get("Content-Length") or 0)
            mode = "ab" if have else "wb"
            done = have
            with open(part, mode) as f:
                while True:
                    if cancel.is_set():
                        raise InterruptedError
                    buf = resp.read(1024 * 256)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    on_progress(done, total)
        if total and done < total:
            raise IOError(f"ダウンロードが途中で切れました ({done}/{total})")
        part.replace(archive)

    on_phase("extract")
    tmp = models_root / f"_extract_{int(time.time())}"
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            members = [m for m in tar.getmembers() if not m.name.startswith(("/", "..")) and ".." not in m.name]
            for i, m in enumerate(members):
                if cancel.is_set():
                    raise InterruptedError
                tar.extract(m, tmp, filter="data")  # フォルダの外・リンク・特殊なファイルは拒む
                on_progress(i + 1, len(members))
        src = tmp / MODEL_NAME
        if not model_ready(src):
            raise IOError("ダウンロードしたモデルの中身が足りません")
        if final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        src.replace(final_dir)
    except tarfile.ReadError:
        archive.unlink(missing_ok=True)  # 壊れたファイル: 次回は最初からダウンロードし直す
        raise IOError("ダウンロードしたファイルが壊れていました。もう一度お試しください")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    archive.unlink(missing_ok=True)
    return final_dir


def run_setup_window(models_root: Path) -> bool:
    """ダウンロード画面を出し、モデルが用意できたら True。閉じた・失敗したら False。"""
    import tkinter as tk
    from tkinter import ttk

    # 2 つ同時にダウンロードしないように
    ctypes.WinDLL("kernel32", use_last_error=True).CreateMutexW(None, False, "Local\\OnseiManModelSetup")
    if ctypes.get_last_error() == 183:
        return False

    result = {"ok": False}
    cancel = threading.Event()
    root = tk.Tk()
    root.title("OnseiMan の準備")
    root.geometry("460x220")
    root.resizable(False, False)
    root.configure(padx=22, pady=18)
    try:
        root.attributes("-topmost", True)
        root.after(1500, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    tk.Label(
        root,
        text="音声を文字にするためのデータをダウンロードします",
        font=("Meiryo UI", 11, "bold"),
        anchor="w",
    ).pack(fill="x")
    tk.Label(
        root,
        text=f"最初の一度だけです（約 {APPROX_MB}MB）。途中で閉じても、次回は続きから再開します。",
        font=("Meiryo UI", 9),
        fg="#555",
        anchor="w",
        justify="left",
        wraplength=410,
    ).pack(fill="x", pady=(4, 12))
    bar = ttk.Progressbar(root, length=410, mode="determinate", maximum=1000)
    bar.pack(fill="x")
    status = tk.Label(root, text="接続しています…", font=("Meiryo UI", 9), anchor="w")
    status.pack(fill="x", pady=(8, 0))
    btns = tk.Frame(root)
    btns.pack(fill="x", side="bottom")
    retry_btn = ttk.Button(btns, text="もう一度試す")
    close_btn = ttk.Button(btns, text="やめる")
    close_btn.pack(side="right")

    state = {"phase": "download", "t0": time.time(), "first": 0}

    def ui(fn):
        try:
            root.after(0, fn)
        except Exception:
            pass  # 画面がもう閉じている

    def on_phase(p: str) -> None:
        state["phase"] = p
        if p == "extract":
            ui(lambda: status.configure(text="展開しています…（1〜2分かかります）"))

    def on_progress(done: int, total: int) -> None:
        def upd():
            if total > 0:
                bar["value"] = done * 1000 // total
            if state["phase"] == "download" and total > 0:
                if not state["first"]:
                    state["first"] = done
                    state["t0"] = time.time()
                el = max(time.time() - state["t0"], 0.001)
                speed = (done - state["first"]) / el
                remain = (total - done) / speed if speed > 0 else 0
                rtxt = f"／あと約 {int(remain // 60)}分{int(remain % 60):02d}秒" if speed > 0 and el > 2 else ""
                status.configure(text=f"{done / 1048576:,.0f} / {total / 1048576:,.0f} MB{rtxt}")

        ui(upd)

    def worker() -> None:
        try:
            download_and_extract(models_root, on_progress, on_phase, cancel)
            result["ok"] = True
            ui(root.destroy)
        except InterruptedError:
            ui(root.destroy)
        except Exception as exc:
            logger.exception("モデルの準備に失敗しました")
            msg = str(exc)

            def show_err():
                status.configure(text=f"失敗しました: {msg[:80]}\nインターネット接続を確認して、もう一度お試しください。", fg="#b91c1c")
                retry_btn.pack(side="right", padx=(0, 8))

            ui(show_err)

    def start() -> None:
        retry_btn.pack_forget()
        status.configure(text="接続しています…", fg="black")
        state.update(phase="download", first=0)
        threading.Thread(target=worker, daemon=True).start()

    def on_close() -> None:
        cancel.set()
        root.after(300, root.destroy)

    retry_btn.configure(command=start)
    close_btn.configure(command=on_close)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(200, start)
    root.mainloop()
    return result["ok"]


def show_message(title: str, text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
