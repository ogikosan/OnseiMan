"""友達に直接渡すためのフォルダ（zip）を作る。exe ではなく、Windows のアプリと Mac のサーバーの両方が入ったソース一式。

使い方: python packaging\\make_friend_package.py
出力:   build_dist\\OnseiMan-<版>-友達用.zip

入れないもの: 自分の設定・辞書・ログ・履歴・録音（config.json, dict.json, logs/）、モデル、仮想環境、
exe 化・配布サイトの道具（packaging/）、テスト、作業用のバックアップ。
"""
from __future__ import annotations

import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRIEND = ROOT / "packaging" / "friend"

INCLUDE_DIRS = ["client", "shared", "server", "tools"]
INCLUDE_FILES = ["download_model.py", "install_client.bat", "start_client.bat", "update.bat"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log"}
# 中身に残っていたら止める言葉（自分のメールアドレス・自宅の IP・知り合いの名前など）。
# 公開しないファイル packaging/private-patterns.txt に 1 行 1 つ（正規表現）で書く（.gitignore 済み）。
PRIVATE_PATTERNS = ROOT / "packaging" / "private-patterns.txt"


def _forbidden() -> list:
    pats = []
    if PRIVATE_PATTERNS.exists():
        for line in PRIVATE_PATTERNS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pats.append(line)
    return pats


def main() -> int:
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "client" / "version.py").read_text(encoding="utf-8")).group(1)
    out = ROOT / "build_dist" / f"OnseiMan-{ver}-友達用.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    top = "OnseiMan"  # 版ごとに名前を変えない（新しい版をそのまま上書きできるように）
    files = []
    for d in INCLUDE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and not (SKIP_PARTS & set(p.parts)) and p.suffix not in SKIP_SUFFIX:
                files.append((p, p.relative_to(ROOT).as_posix()))
    for f in INCLUDE_FILES:
        files.append((ROOT / f, f))
    for p in sorted(FRIEND.iterdir()):
        files.append((p, p.name))

    problems = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            data = src.read_bytes()
            if arc == "client/version.py":
                # 友達用は自動更新を使わない（配布サイトのアドレスも入れない）
                data = re.sub(rb'UPDATE_URL\s*=\s*"[^"]*"', b'UPDATE_URL = ""', data)
            try:
                text = data.decode("utf-8")
                for pat in _forbidden():
                    if re.search(pat, text):
                        problems.append(f"{arc}: {pat}")
            except UnicodeDecodeError:
                pass
            z.writestr(f"{top}/{arc}", data)
    if problems:
        out.unlink(missing_ok=True)
        print("自分の情報が混ざっているため作りませんでした:\n  " + "\n  ".join(problems))
        return 1
    print(f"できました: {out}（{len(files)} ファイル, {out.stat().st_size / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
