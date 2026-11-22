"""バッチと PowerShell のファイルを、Windows で確実に動く形にそろえる（編集したあとに実行する）。

  python packaging\\normalize_scripts.py

- *.bat: 英数字だけ・CRLF（cmd.exe は日本語などの入ったバッチを読み違えることがある）
- *.ps1: BOM つき UTF-8・CRLF（Windows PowerShell 5.1 は BOM が無いと日本語を読み違える）
- 制御文字（\\a や \\b の書き損じ）が入っていたら止める
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CR, LF, TAB = chr(13), chr(10), chr(9)


def main() -> int:
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "*.bat", "*.ps1"], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8").stdout.split()
    bad = 0
    for rel in files:
        f = ROOT / rel
        text = f.read_bytes().decode("utf-8-sig").replace(CR + LF, LF).replace(CR, LF)
        ctrl = sorted({ord(c) for c in text if ord(c) < 32 and c not in (TAB, LF)})
        if ctrl:
            print(f"NG {rel}: 制御文字があります {ctrl}")
            bad += 1
            continue
        out = text.replace(LF, CR + LF)
        if rel.endswith(".bat"):
            if any(ord(c) > 126 for c in text):
                print(f"NG {rel}: 英数字以外の文字があります（日本語は tools\\*.ps1 に書く）")
                bad += 1
                continue
            data = out.encode("ascii")
        else:
            data = b"\xef\xbb\xbf" + out.encode("utf-8")
        if f.read_bytes() != data:
            f.write_bytes(data)
            print(f"直しました {rel}")
    print("問題なし" if not bad else f"{bad} 件の問題")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
