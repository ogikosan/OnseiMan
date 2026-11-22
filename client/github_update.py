"""GitHub に新しい版が出ていないかを確かめ、更新を始める（フォルダを置いて使う形の OnseiMan 用）。

- 確かめる先は update.ps1 と同じ: GitHub の main の client/version.py。
- 何が変わったかは packaging/friend/変更点.md から、いまより新しい版の分だけ取り出す。
- 更新は tools/update.ps1 に任せる。OnseiMan が終わるのを待ってから入れ替え、起動し直す（-WaitPid -Restart）。
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import quote

from client.updater import is_newer
from client.version import __version__

logger = logging.getLogger("onseiman.github_update")

REPO = "ogikosan/OnseiMan"
BRANCH = "main"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/"
RELEASES = f"https://github.com/{REPO}/releases"
VERSION_RE = re.compile(r'__version__\s*=\s*"(\d+(?:\.\d+){0,3})"')
NOTES_MAX = 3000


def fetch_remote_version(timeout: float = 8.0) -> str:
    import requests

    r = requests.get(RAW + "client/version.py", timeout=timeout, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    m = VERSION_RE.search(r.text[:4000])
    if not m:
        raise ValueError("版の番号を読めませんでした")
    return m.group(1)


def parse_notes(md: str, since: str) -> List[Tuple[str, str]]:
    """変更点.md から、since より新しい版の節だけを [(版, 本文)] で返す（新しい順）。"""
    out: List[Tuple[str, str]] = []
    parts = re.split(r"^##\s+(\d+(?:\.\d+){0,3})\s*$", md, flags=re.M)
    # parts = [前置き, 版1, 本文1, 版2, 本文2, ...]
    for i in range(1, len(parts) - 1, 2):
        ver, body = parts[i], parts[i + 1].strip()
        if is_newer(ver, since):
            out.append((ver, body))
    return out


def fetch_notes(since: str, timeout: float = 8.0) -> str:
    import requests

    r = requests.get(RAW + quote("packaging/friend/変更点.md"), timeout=timeout, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    text = "\n\n".join(f"{v}\n{b}" for v, b in parse_notes(r.text, since))
    return text[:NOTES_MAX]


def check(current: str = __version__) -> Optional[dict]:
    """新しい版があれば {"version", "current", "notes"}、なければ None。確かめられなければ例外。"""
    remote = fetch_remote_version()
    if not is_newer(remote, current):
        return None
    try:
        notes = fetch_notes(current)
    except Exception as exc:
        logger.info("変更点を取れませんでした: %s", exc)
        notes = ""
    return {"version": remote, "current": current, "notes": notes, "url": RELEASES}


def is_dev_folder(root: Path) -> bool:
    """git で管理している開発用のフォルダ（上書きすると作業中の変更を壊すので、自動では更新しない）。"""
    return (Path(root) / ".git").exists()


def launch_update(root: Path, pid: int) -> None:
    """更新を別の窓で始める。この OnseiMan（pid）が終わるのを待ってから入れ替え、起動し直す。"""
    root = Path(root)
    script = root / "tools" / "update.ps1"
    if not script.exists():
        raise FileNotFoundError(script)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-WaitPid", str(int(pid)), "-Restart"],
        cwd=str(root),
        creationflags=flags,
    )
