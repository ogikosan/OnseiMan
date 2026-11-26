"""OnseiMan のインストーラー（OnseiMan-Setup-<版>.exe）を作る。

使い方: python packaging\\installer\\build_installer.py [--sign]
出力:   build_dist\\installer\\OnseiMan-Setup-<版>.exe

- 中身は Windows 用の zip と同じ（make_friend_package.py の collect("Windows")）。自分の情報が混ざっていたら作らない。
- Inno Setup 6 の ISCC.exe を使う（ISCC という環境変数か、いつもの場所から探す）。
- --sign: できたインストーラーに署名する（packaging\\installer\\signing.json に署名の設定。git では管理しない）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
import make_friend_package as pkg  # noqa: E402

STAGE = ROOT / "build_dist" / "installer_stage" / "OnseiMan"
OUT_DIR = ROOT / "build_dist" / "installer"
SIGNING = HERE / "signing.json"


def find_iscc() -> Path:
    cands = [os.environ.get("ISCC", "")]
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", "")):
        if base:
            cands += [str(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"), str(Path(base) / "Inno Setup 6" / "ISCC.exe")]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    raise SystemExit("Inno Setup 6（ISCC.exe）が見つかりません。https://jrsoftware.org/isdl.php から入れてください。")


def stage(forbidden: list) -> int:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    problems = []
    files = pkg.collect("Windows")
    for src, arc in files:
        data = src.read_bytes()
        if arc == "client/version.py":
            data = re.sub(rb'UPDATE_URL\s*=\s*"[^"]*"', b'UPDATE_URL = ""', data)
        try:
            text = data.decode("utf-8")
            problems += [f"{arc}: {p}" for p in forbidden if re.search(p, text)]
        except UnicodeDecodeError:
            pass
        dst = STAGE / arc
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    if problems:
        raise SystemExit("自分の情報が混ざっているため作りませんでした:\n  " + "\n  ".join(problems))
    return len(files)


def sign(path: Path) -> None:
    """Azure の Artifact Signing で署名する（signtool と、署名用の部品 Azure.CodeSigning.Dlib.dll を使う）。"""
    if not SIGNING.exists():
        raise SystemExit(f"署名の設定がありません: {SIGNING}")
    cfg = json.loads(SIGNING.read_text(encoding="utf-8"))
    signtool, dlib = cfg["signtool"], cfg["dlib"]
    meta = HERE / "signing-metadata.json"
    meta.write_text(json.dumps({
        "Endpoint": cfg["endpoint"],
        "CodeSigningAccountName": cfg["account"],
        "CertificateProfileName": cfg["profile"],
    }), encoding="utf-8")
    try:
        subprocess.run([signtool, "sign", "/v", "/fd", "SHA256", "/tr", "http://timestamp.acs.microsoft.com", "/td", "SHA256",
                        "/dlib", dlib, "/dmdf", str(meta), str(path)], check=True)
    finally:
        meta.unlink(missing_ok=True)
    subprocess.run([signtool, "verify", "/pa", "/v", str(path)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sign", action="store_true", help="できたインストーラーに署名する")
    a = ap.parse_args()
    ver = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "client" / "version.py").read_text(encoding="utf-8")).group(1)
    n = stage(pkg._forbidden())
    print(f"並べました: {n} ファイル → {STAGE}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(find_iscc()), "/Qp", f"/DAppVersion={ver}", f"/DStageDir={STAGE}", str(HERE / "onseiman_setup.iss")], check=True)
    out = OUT_DIR / f"OnseiMan-Setup-{ver}.exe"
    if a.sign:
        sign(out)
    print(f"できました: {out}（{out.stat().st_size / 1024 / 1024:.1f} MB）{'・署名済み' if a.sign else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
