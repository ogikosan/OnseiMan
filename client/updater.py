"""起動時に新しいバージョンがないか確かめ、あれば更新する（配布版のみ）。

latest.json の形:
  {"version": "1.0.1", "url": "https://.../files/OnseiMan-Setup-1.0.1.exe",
   "sha256": "...", "notes": "変更点の説明"}

流れ: 確認 → 「更新しますか？」→ インストーラーをダウンロード → 中身が壊れていないか sha256 で確認
→ インストーラーを無人モードで起動して OnseiMan は終了 → インストーラーが入れ替えて OnseiMan を起動し直す。
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests

from client.version import UPDATE_URL, __version__

logger = logging.getLogger("onseiman.updater")

MB_YESNO = 0x04
MB_ICONINFORMATION = 0x40
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
IDYES = 6
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class UpdateInfo:
    version: str
    url: str
    sha256: str
    notes: str = ""


def parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def fetch_latest(url: str = UPDATE_URL, timeout: float = 8.0) -> Optional[UpdateInfo]:
    if not url:
        return None
    resp = requests.get(url, timeout=timeout, headers={"Cache-Control": "no-cache"})
    resp.raise_for_status()
    data = resp.json()
    info = UpdateInfo(
        version=str(data.get("version", "")),
        url=str(data.get("url", "")),
        sha256=str(data.get("sha256", "")).lower(),
        notes=str(data.get("notes", "")),
    )
    if not info.version or not info.url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", info.sha256):
        raise ValueError("最新版の情報の形が正しくありません")
    return info


def download_installer(info: UpdateInfo, on_progress: Optional[Callable[[int, int], None]] = None) -> Path:
    """インストーラーを一時フォルダにダウンロードし、sha256 が合えばそのパスを返す。"""
    dest_dir = Path(tempfile.gettempdir()) / "OnseiManUpdate"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"OnseiMan-Setup-{re.sub(r'[^0-9A-Za-z.]', '', info.version)}.exe"
    h = hashlib.sha256()
    with requests.get(info.url, stream=True, timeout=(10, 60)) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1024 * 256):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
    if h.hexdigest() != info.sha256:
        dest.unlink(missing_ok=True)
        raise ValueError("ダウンロードしたファイルが壊れていました（確認用の値が一致しません）")
    return dest


def launch_installer(installer: Path) -> None:
    """インストーラーを無人モードで起動する。/UPDATE=1 で、入れ替え後に OnseiMan を起動し直してくれる。"""
    subprocess.Popen(
        [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/UPDATE=1"],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        cwd=os.path.dirname(str(installer)),
    )


def ask_user(info: UpdateInfo) -> bool:
    notes = f"\n\n変更点:\n{info.notes}" if info.notes else ""
    text = (
        f"OnseiMan の新しいバージョン {info.version} があります（いまは {__version__}）。{notes}\n\n"
        "今すぐ更新しますか？（1分ほどで終わり、自動で起動し直します）"
    )
    r = ctypes.windll.user32.MessageBoxW(
        None, text, "OnseiMan の更新", MB_YESNO | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND
    )
    return r == IDYES
