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

    def test_requirements_are_ascii(self) -> None:
        # 日本語の Windows の pip は、この一覧を cp932 として読む。UTF-8 の日本語があると失敗する
        for rel in ("client/requirements.txt", "server/requirements.txt"):
            raw = (ROOT / rel).read_bytes()
            self.assertTrue(all(b < 127 for b in raw), f"{rel}: 英数字以外の文字があります")

    def test_update_bat_survives_being_replaced(self) -> None:
        # update.bat は実行中に自分自身が新しい版に置き換わる。cmd は 1 行を読み切ってから実行するので、
        # 置き換えのあとに実行する命令（pause など）を、更新の命令と同じ 1 行に置く
        lines = [ln for ln in (ROOT / "update.bat").read_text(encoding="ascii").splitlines()
                 if ln.strip() and not ln.lower().startswith(("rem ", "@echo off"))]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("update.ps1", lines[0])
        self.assertTrue(lines[0].rstrip().endswith("exit /b"))

    def test_start_runs_setup_when_not_ready(self) -> None:
        ps = (ROOT / "tools" / "install.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("onseiman-ready.txt", ps)
        self.assertIn(".venv\\onseiman-ready.txt", (ROOT / "start_client.bat").read_text(encoding="ascii"))
        self.assertIn(".venv_server\\onseiman-ready.txt", (ROOT / "server" / "start_server.bat").read_text(encoding="ascii"))


class TestMacFiles(unittest.TestCase):
    """Mac で動かすファイル（.command / .sh）。Mac は CRLF だと `bash\\r` を探して失敗する。"""

    MAC_FILES = ["server_mac.command", "server_mac_remove.command", "server/setup_macos.sh", "server/start_server.sh"]

    def test_mac_files_are_lf_with_shebang(self) -> None:
        for rel in self.MAC_FILES:
            raw = (ROOT / rel).read_bytes()
            self.assertNotIn(b"\r", raw, f"{rel}: CR があります（改行は LF だけにする）")
            self.assertTrue(raw.startswith(b"#!/"), f"{rel}: 1 行目に #! がありません")
            ctrl = sorted({b for b in raw if b < 32 and b not in (9, 10)})
            self.assertEqual(ctrl, [], f"{rel}: 制御文字があります")

    def test_mac_files_are_executable_in_git(self) -> None:
        # GitHub の ZIP や clone で、ダブルクリック・実行できる印が付いたまま届くように
        out = subprocess.run(["git", "ls-files", "-s", *self.MAC_FILES], cwd=ROOT,
                             capture_output=True, text=True, encoding="utf-8").stdout
        modes = {line.split("\t", 1)[1]: line.split()[0] for line in out.splitlines()}
        for rel in self.MAC_FILES:
            self.assertEqual(modes.get(rel), "100755", f"{rel}: git に実行の印（100755）がありません")

    def test_mac_launchers_point_to_existing_files(self) -> None:
        self.assertIn("server/setup_macos.sh", (ROOT / "server_mac.command").read_text(encoding="utf-8"))
        setup = (ROOT / "server" / "setup_macos.sh").read_text(encoding="utf-8")
        for rel in ("server/requirements.txt", "download_model.py", "server/app.py"):
            self.assertIn(rel.split("/")[-1], setup)
            self.assertTrue((ROOT / rel).exists(), rel)
        # uv は版と確認用の値を固定して使う（Mac の 2 種類とも）
        self.assertEqual(len(re.findall(r'UV_SHA256="[0-9a-f]{64}"', setup)), 2)
        self.assertIn('UV_VERSION="0.13.9"', setup)

    def test_mac_shell_syntax(self) -> None:
        import shutil
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash が無い")
        for rel in self.MAC_FILES:
            r = subprocess.run([bash, "-n", rel], cwd=ROOT, capture_output=True)
            self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr.decode('utf-8', 'replace')}")


class TestPackages(unittest.TestCase):
    """配る zip（Windows 用・Mac 用）の中身。"""

    @classmethod
    def setUpClass(cls) -> None:
        sys.path.insert(0, str(ROOT / "packaging"))
        import make_friend_package
        cls.pkg = make_friend_package

    def _names(self, kind: str) -> set:
        files = self.pkg.collect(kind)
        for src, arc in files:
            self.assertTrue(src.exists(), f"{kind}: {arc} の元がありません")
        return {arc for _, arc in files}

    def test_windows_package(self) -> None:
        names = self._names("Windows")
        for need in ("install_client.bat", "start_client.bat", "update.bat", "server_windows.bat",
                     "client/main.py", "server/app.py", "server/start_server.bat", "tools/install.ps1",
                     "はじめに.md", "AGENTS.md", "変更点.md", "LICENSE"):
            self.assertIn(need, names)
        self.assertFalse([n for n in names if n.endswith((".command", ".sh"))])

    def test_mac_package_has_only_server(self) -> None:
        names = self._names("Mac")
        for need in ("server_mac.command", "server_mac_remove.command", "server/setup_macos.sh", "server/app.py",
                     "server/requirements.txt", "shared/asr_engine.py", "download_model.py",
                     "はじめに.md", "変更点.md", "LICENSE"):
            self.assertIn(need, names)
        self.assertFalse([n for n in names if n.startswith(("client/", "tools/")) or n.endswith((".bat", ".ps1"))])

    def test_no_user_data_in_packages(self) -> None:
        for kind in self.pkg.PACKAGES:
            for name in self._names(kind):
                self.assertFalse(name in ("config.json", "dict.json") or name.startswith(("logs/", "models/", ".venv")),
                                 f"{kind}: {name}")


if __name__ == "__main__":
    unittest.main()
