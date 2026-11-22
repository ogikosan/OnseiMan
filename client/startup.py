"""Windows ログオン時の自動起動（HKCU\\...\\Run）を読み書きする。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("onseiman.startup")

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE_NAME = "OnseiMan"


def build_startup_command(root_dir: Path) -> str:
    """pythonw.exe を使ってコンソール窓なしで client/main.py を起動するコマンドを組み立てる。
    配布版（exe）では exe そのものを登録する。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    venv_pyw = root_dir / ".venv" / "Scripts" / "pythonw.exe"
    if venv_pyw.exists():
        exe = venv_pyw
    else:
        py = Path(sys.executable)
        pyw = py.with_name("pythonw.exe")
        exe = pyw if pyw.exists() else py
    main_py = (root_dir / "client" / "main.py").resolve()
    return f'"{exe.resolve()}" "{main_py}"'


def is_startup_enabled(value_name: str = APP_VALUE_NAME) -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, value_name)
            return bool(str(val).strip())
    except FileNotFoundError:
        return False
    except Exception as exc:
        logger.debug("自動起動設定の確認に失敗: %s", exc)
        return False


def set_startup_enabled(
    enabled: bool,
    root_dir: Path,
    value_name: str = APP_VALUE_NAME,
    command_override: Optional[str] = None,
) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            cmd = command_override or build_startup_command(root_dir)
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, cmd)
            logger.info("Windows 起動時の自動起動を有効にしました: %s", cmd)
        else:
            try:
                winreg.DeleteValue(key, value_name)
                logger.info("Windows 起動時の自動起動を無効にしました")
            except FileNotFoundError:
                pass
