"""配る zip を作る。Windows 用（アプリ＋この Windows を認識役にするもの）と、Mac 用（Mac を認識役にするものだけ）の 2 つ。

使い方: python packaging\\make_friend_package.py
出力:   build_dist\\OnseiMan-<版>-Windows.zip / build_dist\\OnseiMan-<版>-Mac.zip
        （どちらも中のフォルダ名は OnseiMan。新しい版をそのまま上書きできるように）

入れないもの: 自分の設定・辞書・ログ・履歴・録音（config.json, dict.json, logs/）、モデル、仮想環境、
exe 化・配布サイトの道具（packaging/）、テスト、作業用のバックアップ。
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "OnseiMan"
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".log"}
COMMON_FILES = ["download_model.py", "LICENSE", "SECURITY.md", "packaging/friend/変更点.md"]

# 中の場所 → 元のファイル。フォルダは中身をまとめて入れる。
PACKAGES = {
    "Windows": {
        "dirs": ["client", "shared", "server", "tools"],
        "skip_suffix": {".sh"},
        "files": ["install_client.bat", "start_client.bat", "update.bat", "server_windows.bat",
                  "packaging/friend/はじめに.md", "packaging/friend/AGENTS.md"],
    },
    "Mac": {
        "dirs": ["shared"],
        "skip_suffix": set(),
        "files": ["server/app.py", "server/requirements.txt", "server/setup_macos.sh", "server/start_server.sh",
                  "server_mac.command", "server_mac_remove.command", "packaging/mac/はじめに.md"],
    },
}
EXECUTABLE = {".command", ".sh"}  # Mac でダブルクリック・実行できるよう、実行してよい印を付ける

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


def _arcname(rel: str) -> str:
    # 説明（packaging/friend・packaging/mac）は zip の一番上に置く
    if rel.startswith("packaging/"):
        return rel.rsplit("/", 1)[1]
    return rel


def collect(kind: str) -> list:
    spec = PACKAGES[kind]
    files = []
    for d in spec["dirs"]:
        for p in sorted((ROOT / d).rglob("*")):
            if (p.is_file() and not (SKIP_PARTS & set(p.parts))
                    and p.suffix not in SKIP_SUFFIX and p.suffix not in spec["skip_suffix"]):
                files.append((p, p.relative_to(ROOT).as_posix()))
    for rel in spec["files"] + COMMON_FILES:
        files.append((ROOT / rel, _arcname(rel)))
    return files


def build(kind: str, ver: str, forbidden: list) -> int:
    out = ROOT / "build_dist" / f"OnseiMan-{ver}-{kind}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    files = collect(kind)
    problems = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for src, arc in files:
            data = src.read_bytes()
            if arc == "client/version.py":
                # 配る版は自動更新を使わない（配布サイトのアドレスも入れない）
                data = re.sub(rb'UPDATE_URL\s*=\s*"[^"]*"', b'UPDATE_URL = ""', data)
            try:
                text = data.decode("utf-8")
                for pat in forbidden:
                    if re.search(pat, text):
                        problems.append(f"{arc}: {pat}")
            except UnicodeDecodeError:
                pass
            info = zipfile.ZipInfo(f"{TOP}/{arc}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix: Mac で展開したときに、下の実行の印を使ってもらう
            mode = 0o755 if Path(arc).suffix in EXECUTABLE else 0o644
            info.external_attr = (0o100000 | mode) << 16
            z.writestr(info, data)
    if problems:
        out.unlink(missing_ok=True)
        print(f"{kind}: 自分の情報が混ざっているため作りませんでした:\n  " + "\n  ".join(problems))
        return 1
    print(f"できました: {out}（{len(files)} ファイル, {out.stat().st_size / 1024:.0f} KB）")
    return 0


def main() -> int:
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "client" / "version.py").read_text(encoding="utf-8")).group(1)
    forbidden = _forbidden()
    return max(build(kind, ver, forbidden) for kind in PACKAGES)


if __name__ == "__main__":
    sys.exit(main())
