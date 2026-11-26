"""speech.cpp（MIT）と Haas-ja-v1.0（Apache-2.0）の GGUF を、OnseiMan のフォルダの中に用意する。

- speech.cpp: Python も CUDA も WSL2 も要らない、1 つの実行ファイルで動く聞き取りの道具。
  Windows は Vulkan 版（CPU でも動く）、Mac は Metal 版。置き場所は tools/speech/。
- Haas-ja-v1.0 の Q8_0（0.66GB）: models/haas-ja/。CPU でも 1 回 1 秒以内。
- どちらも決まった版に固定し、ダウンロードしたあと確認用の値（SHA-256）を確かめる。
- パソコン全体には何も入れない（PATH も変えない）。

使い方: python -m shared.speechcpp_setup [OnseiMan のフォルダ]
"""
from __future__ import annotations

import hashlib
import os
import platform
import stat
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

VERSION = "0.11.2"
_BASE = f"https://github.com/nyosegawa/speech.cpp/releases/download/v{VERSION}/"
# 配布元（GitHub の Releases の SHA256SUMS）が示す確認用の値
TOOLS = {
    "windows": (f"speech-{VERSION}-windows-x64-vulkan.zip", "2cf46f0351a2a90b9ce0bfb83cf63ac209ca31c88df9a5bc0126898a20a58586"),
    "mac": (f"speech-{VERSION}-macos-arm64-metal.zip", "1984a2742131bfa0048b932853c94859618bdc383e5dbbea1a1006fd8baece23"),
}
MODEL_FILE = "Haas-616M-ja-v1.0-Q8_0.gguf"
MODEL_REVISION = "6b965d5007ba780585de51549d57a8c1c0d67c7e"
MODEL_URL = f"https://huggingface.co/sakasegawa/Haas-ja-v1.0-GGUF/resolve/{MODEL_REVISION}/{MODEL_FILE}"
# Hugging Face が示す確認用の値（LFS の oid）
MODEL_SHA256 = "cd5f60b2f1c411404b83fca4a65e3ce30a26d9e951b6a346529e65f22758afeb"
MODEL_BYTES = 656_381_472

Progress = Callable[[str, int, int], None]   # (何をしているか, 済んだバイト, 全部のバイト)


def platform_key() -> Optional[str]:
    if os.name == "nt" and platform.machine().lower() in ("amd64", "x86_64"):
        return "windows"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mac"
    return None


def tool_dir(root: Path) -> Path:
    return Path(root) / "tools" / "speech"


def model_path(root: Path) -> Path:
    return Path(root) / "models" / "haas-ja" / MODEL_FILE


def find_exe(root: Path) -> Optional[Path]:
    name = "speech.exe" if os.name == "nt" else "speech"
    d = tool_dir(root)
    if not d.is_dir():
        return None
    for p in sorted(d.rglob(name)):
        if p.is_file():
            return p
    return None


def ready(root: Path) -> bool:
    """部品が揃っているか（大きさだけ確かめる。中身はダウンロードのときに確かめてある）。"""
    m = model_path(root)
    return find_exe(root) is not None and m.is_file() and m.stat().st_size == MODEL_BYTES


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, dest: Path, sha256: str, label: str, progress: Optional[Progress],
              cancel: Optional[threading.Event]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "OnseiMan"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("やめました")
            block = r.read(1 << 20)
            if not block:
                break
            f.write(block)
            done += len(block)
            if progress:
                progress(label, done, total)
    if _sha256(part) != sha256:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{label}: ダウンロードしたものが正しいものと一致しませんでした")
    os.replace(part, dest)


def ensure(root: Path, progress: Optional[Progress] = None, cancel: Optional[threading.Event] = None) -> bool:
    """揃っていなければ取ってくる。この機械で使えないとき（Windows x64・Apple の Mac 以外）は False。"""
    root = Path(root)
    key = platform_key()
    if key is None:
        return False
    if find_exe(root) is None:
        name, sha = TOOLS[key]
        with tempfile.TemporaryDirectory(prefix="onseiman-speech-") as tmp:
            z = Path(tmp) / name
            _download(_BASE + name, z, sha, "聞き取りの道具（speech.cpp）", progress, cancel)
            d = tool_dir(root)
            d.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    target = (d / info.filename).resolve()
                    if not str(target).startswith(str(d.resolve())):
                        raise RuntimeError("zip の中身が正しくありません")
                zf.extractall(d)
            if os.name != "nt":
                # zip から出したファイルは実行してよい印が消えるので付け直す
                for p in d.rglob("*"):
                    if p.is_file():
                        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if find_exe(root) is None:
            raise RuntimeError("speech.cpp の実行ファイルが見つかりません")
    m = model_path(root)
    if not (m.is_file() and m.stat().st_size == MODEL_BYTES):
        _download(MODEL_URL, m, MODEL_SHA256, "Haas-ja の聞き取りのデータ（約 0.66GB）", progress, cancel)
    return True


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    last = {"label": "", "pct": -1}

    def show(label: str, done: int, total: int) -> None:
        pct = done * 100 // total if total else 0
        if label != last["label"] or pct // 10 != last["pct"] // 10:
            last.update(label=label, pct=pct)
            print(f"  {label}: {pct}%", flush=True)

    try:
        ok = ensure(root, show)
    except Exception as exc:
        print(f"用意できませんでした: {exc}")
        return 1
    print("用意できました" if ok else "この機械では使えません（Windows x64 か、Apple の Mac で使えます）")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
