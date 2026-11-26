"""新しい版を GitHub に公開する（1 つの命令で、必ず全部そろえる）。

使い方（プロジェクトのフォルダで、develop にいるとき）:
  1. client\\version.py の __version__ を上げ、packaging\\friend\\変更点.md の先頭に「## <版>」の節を書く
  2. python packaging\\release.py            … テスト → zip 2 つ → インストーラー → main を進める → タグ → 公開
     python packaging\\release.py --dry-run  … 作るところまで（公開しない）

どれか 1 つでも失敗したら、公開しない（中途半端な版を出さない）。
公開するもの: OnseiMan-Setup-<版>.exe・OnseiMan-<版>-Windows.zip・OnseiMan-<版>-Mac.zip。
お知らせの文は、変更点.md の「## <版>」の節から作る。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTE_TAIL = "\n\n※ インストーラーはまだ署名していません。「PC を保護しました」と出たら「詳細情報」→「実行」で進めてください。"


def run(cmd: list, **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=True, **kw)


def out(cmd: list) -> str:
    return subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def version() -> str:
    return re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "client" / "version.py").read_text(encoding="utf-8")).group(1)


def notes_for(ver: str) -> str:
    text = (ROOT / "packaging" / "friend" / "変更点.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(ver)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise SystemExit(f"変更点.md に「## {ver}」の節がありません。先に書いてください")
    return m.group(1).strip() + NOTE_TAIL


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="作るところまで（公開しない）")
    a = ap.parse_args()
    ver = version()
    tag = f"v{ver}"

    # 0) 出してよい状態か
    if out(["git", "branch", "--show-current"]) != "develop":
        raise SystemExit("develop で実行してください")
    if out(["git", "status", "--porcelain"]):
        raise SystemExit("コミットしていない変更があります。先にコミットしてください")
    if out(["git", "tag", "-l", tag]):
        raise SystemExit(f"{tag} はもう公開しています。client\\version.py の版を上げてください")
    notes = notes_for(ver)

    # 1) テスト
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    run([str(py if py.exists() else sys.executable), "-m", "unittest", "discover", "-s", "tests"])

    # 2) 配るもの（zip 2 つとインストーラー。中身は同じ git の状態から作る）
    run([sys.executable, str(ROOT / "packaging" / "make_friend_package.py")])
    run([sys.executable, str(ROOT / "packaging" / "installer" / "build_installer.py")])
    files = [ROOT / "build_dist" / "installer" / f"OnseiMan-Setup-{ver}.exe",
             ROOT / "build_dist" / f"OnseiMan-{ver}-Windows.zip",
             ROOT / "build_dist" / f"OnseiMan-{ver}-Mac.zip"]
    missing = [f.name for f in files if not f.exists()]
    if missing:
        raise SystemExit("できていないものがあります: " + ", ".join(missing))
    if a.dry_run:
        print("作りました（--dry-run なので公開しません）:")
        for f in files:
            print("  ", f)
        return 0

    # 3) main を develop まで進めて、タグを付けて送る（main に直接コミットはしない）
    run(["git", "merge-base", "--is-ancestor", "main", "develop"])
    run(["git", "branch", "-f", "main", "develop"])
    run(["git", "tag", "-a", tag, "-m", ver])
    run(["git", "push", "origin", "main", "develop", f"refs/tags/{tag}"])

    # 4) 公開
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(notes)
        notes_path = f.name
    try:
        run(["gh", "release", "create", tag, *[str(p) for p in files], "--title", ver, "--notes-file", notes_path])
    finally:
        Path(notes_path).unlink(missing_ok=True)
    print(f"公開しました: {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
