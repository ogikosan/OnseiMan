"""バッチ・PowerShell のファイルが、Windows で壊れずに動く形かどうかのテスト。

2026-11-23: install_client.bat に BEL（\\a）と CR（\\r）が紛れ込み、利用者の環境で失敗した。
同じことを起こさないよう、配るスクリプトの形をここで確かめる。
"""
from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _files(*patterns: str) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", *patterns], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout.split()
    return [ROOT / f for f in out]


class TestBatchFiles(unittest.TestCase):
    def test_bat_files_are_plain_ascii_crlf(self) -> None:
        bats = _files("*.bat")
        self.assertGreaterEqual(len(bats), 5)
        for f in bats:
            raw = f.read_bytes()
            # cmd.exe は、日本語などの文字が入ったバッチを読み違えることがある → 英数字だけにする
            self.assertTrue(all(b < 127 for b in raw), f"{f.name}: 英数字以外の文字があります")
            # BEL などの制御文字（\\a や \\r の書き損じ）が無いこと
            ctrl = sorted({b for b in raw if b < 32 and b not in (9, 10, 13)})
            self.assertEqual(ctrl, [], f"{f.name}: 制御文字があります")
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), f"{f.name}: 改行が CRLF ではありません")
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\r"), f"{f.name}: 単独の CR があります")

    def test_bat_files_point_to_existing_files(self) -> None:
        for f in _files("*.bat"):
            text = f.read_text(encoding="ascii")
            base = f.parent
            for m in re.finditer(r'%~dp0((?:\.\.\\)*)([A-Za-z0-9_\\.\-]+\.(?:ps1|bat|py))', text):
                target = (base / m.group(1).replace("\\", "/") / m.group(2).replace("\\", "/")).resolve()
                self.assertTrue(target.exists(), f"{f.name}: {m.group(0)} がありません")
            for m in re.finditer(r'(?<![\w\\."])((?:client|server|packaging|tools)\\[A-Za-z0-9_\\.\-]+\.py)', text):
                self.assertTrue((ROOT / m.group(1).replace("\\", "/")).exists(), f"{f.name}: {m.group(1)} がありません")

    def test_ps1_files_have_bom_and_crlf(self) -> None:
        ps = _files("*.ps1")
        self.assertGreaterEqual(len(ps), 2)
        for f in ps:
            raw = f.read_bytes()
            # Windows PowerShell 5.1 は、BOM が無いと日本語を読み違える
            self.assertEqual(raw[:3], b"\xef\xbb\xbf", f"{f.name}: BOM がありません")
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"), f"{f.name}: 改行が CRLF ではありません")
            ctrl = sorted({b for b in raw if b < 32 and b not in (9, 10, 13)})
            self.assertEqual(ctrl, [], f"{f.name}: 制御文字があります")

    @unittest.skipUnless(sys.platform == "win32", "Windows だけ")
    def test_ps1_files_parse(self) -> None:
        for f in _files("*.ps1"):
            cmd = ("$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e); "
                   "if ($e) { exit 1 } else { exit 0 }") % str(f)
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True)
            self.assertEqual(r.returncode, 0, f"{f.name}: PowerShell が読めません")

    def test_requirements_files_exist(self) -> None:
        for rel in ("client/requirements.txt", "server/requirements.txt", "download_model.py"):
            self.assertTrue((ROOT / rel).exists(), rel)


if __name__ == "__main__":
    unittest.main()
