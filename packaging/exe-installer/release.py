"""新しいバージョンを公開する: exe 化 → インストーラー作成 → Cloudflare R2 へアップロード → latest.json 更新。

使い方（プロジェクトのフォルダで）:
  1. client\\version.py の __version__ を上げる（例 1.0.0 → 1.0.1）
  2. packaging\\exe-installer\\release.bat "変更点の説明"
     （.venv\\Scripts\\python.exe packaging\\exe-installer\\release.py --notes "変更点の説明" と同じ）

--skip-build で作成済みのインストーラーをそのまま上げる。--dry-run でアップロードせずに確認だけする。
インストーラーを先に上げてから latest.json を書き換えるので、途中で失敗しても利用者に壊れた更新は届かない。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]  # packaging/exe-installer -> プロジェクト直下
HERE = Path(__file__).resolve().parent
BUCKET = "onseiman-releases"
ISCC_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
]


def read_version() -> tuple[str, str]:
    src = (ROOT / "client" / "version.py").read_text(encoding="utf-8")
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
    url = re.search(r'UPDATE_URL\s*=\s*"([^"]*)"', src).group(1)
    return ver, url


def run(cmd: list[str], **kw) -> None:
    print("> " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def wrangler(*args: str) -> None:
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise SystemExit("npx が見つかりません（Node.js を入れてください）")
    run([npx, "-y", "wrangler@4", *args], cwd=str(HERE / "cloudflare"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", default="", help="更新の確認画面に出す変更点")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ver, update_url = read_version()
    if not update_url.endswith("/latest.json"):
        raise SystemExit("client/version.py の UPDATE_URL が設定されていません")
    base = update_url[: -len("/latest.json")]
    installer = ROOT / "build_dist" / "installer" / f"OnseiMan-Setup-{ver}.exe"

    # 公開中より新しいか確認（同じ番号で上書きすると、更新が届かない）
    try:
        cur = requests.get(update_url, timeout=10).json().get("version", "")
    except Exception:
        cur = ""
    if cur and not args.dry_run:
        from_v = tuple(int(x) for x in re.findall(r"\d+", cur))
        to_v = tuple(int(x) for x in re.findall(r"\d+", ver))
        if to_v <= from_v:
            raise SystemExit(f"公開中は {cur} です。client/version.py の __version__（いま {ver}）を上げてください")

    if not args.skip_build:
        run([sys.executable, "-m", "PyInstaller", str(HERE / "onseiman.spec"), "--noconfirm",
             "--distpath", str(ROOT / "build_dist" / "dist"), "--workpath", str(ROOT / "build_dist" / "work")])
        iscc = next((p for p in ISCC_CANDIDATES if p.exists()), None)
        if not iscc:
            raise SystemExit("Inno Setup 6 が見つかりません（winget install JRSoftware.InnoSetup）")
        run([str(iscc), f"/DAppVersion={ver}", str(HERE / "onseiman.iss")])
    if not installer.exists():
        raise SystemExit(f"インストーラーがありません: {installer}")

    sha = hashlib.sha256(installer.read_bytes()).hexdigest()
    key = f"files/{installer.name}"
    latest = {"version": ver, "url": f"{base}/{key}", "sha256": sha, "notes": args.notes}
    print(json.dumps(latest, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("（--dry-run のためアップロードしません）")
        return 0

    wrangler("r2", "object", "put", f"{BUCKET}/{key}", "--file", str(installer),
             "--content-type", "application/vnd.microsoft.portable-executable", "--remote")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False)
        tmp = f.name
    try:
        wrangler("r2", "object", "put", f"{BUCKET}/latest.json", "--file", tmp,
                 "--content-type", "application/json", "--remote")
    finally:
        os.unlink(tmp)

    # 公開できたか確かめる
    got = requests.get(update_url, timeout=15, headers={"Cache-Control": "no-cache"}).json()
    head = requests.head(latest["url"], timeout=15)
    ok = got.get("version") == ver and got.get("sha256") == sha and head.status_code == 200
    print(f"\n公開{'しました' if ok else 'の確認に失敗しました'}: {ver}  ダウンロードページ: {base}/")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
