"""ファイルの置き場所をまとめる。

- 開発中（python client/main.py で動かす）: すべてプロジェクトのフォルダ（今までどおり）。
- 配布版（PyInstaller で exe にしたもの）:
  - プログラム本体と画面のファイル: exe のあるフォルダ（読み取り専用でよい）
  - 設定・辞書・ログ・モデル: %LOCALAPPDATA%\\OnseiMan（ユーザーごとに書き込める場所）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

FROZEN = bool(getattr(sys, "frozen", False))

# ソースのルート（開発中）／exe のフォルダ（配布版）
CODE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else CODE_DIR
# 同梱ファイル（初期設定など）の場所
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", CODE_DIR)) if FROZEN else CODE_DIR


def _data_dir() -> Path:
    if not FROZEN:
        return CODE_DIR
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "OnseiMan"


DATA_DIR = _data_dir()
LOG_DIR = DATA_DIR / "logs"


def client_command(*args: str) -> List[str]:
    """OnseiMan 本体をもう一度起動するコマンド（見張りからの再起動・ログオン時の自動起動で使う）。"""
    if FROZEN:
        return [str(Path(sys.executable).resolve()), *args]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return [str(exe), str(CODE_DIR / "client" / "main.py"), *args]


def watchdog_command(client_pid: int) -> List[str]:
    """見張りプロセスを起動するコマンド。配布版では同じ exe を --watchdog 付きで起動する。"""
    if FROZEN:
        return [str(Path(sys.executable).resolve()), "--watchdog", str(client_pid)]
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.exists():
        exe = pyw
    return [str(exe), "-m", "client.watchdog", str(client_pid)]
