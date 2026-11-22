"""認識役が古い版かどうかを、版と指紋で見分けるテスト。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.server_fingerprint import SERVER_FILES, fingerprint, read_version  # noqa: E402

try:
    from fastapi.testclient import TestClient
    from server import app as srv
    HAVE_SERVER = True
except Exception:
    HAVE_SERVER = False


class TestFingerprint(unittest.TestCase):
    def _copy(self, dst: Path) -> None:
        for rel in SERVER_FILES + ("client/version.py",):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / rel, dst / rel)

    def test_same_files_same_fingerprint_even_with_other_newlines(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._copy(d)
            self.assertEqual(fingerprint(d), fingerprint(ROOT))
            # Mac（LF）と Windows（CRLF）で改行が違っても同じ
            p = d / "server" / "app.py"
            p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            self.assertEqual(fingerprint(d), fingerprint(ROOT))
            self.assertEqual(read_version(d), read_version(ROOT))

    def test_changed_server_file_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._copy(d)
            with open(d / "shared" / "segmenter.py", "a", encoding="utf-8") as f:
                f.write("\n# 変更\n")
            self.assertNotEqual(fingerprint(d), fingerprint(ROOT))

    def test_client_only_change_does_not_matter(self) -> None:
        # アプリだけの部品（辞書など）が変わっても、認識役の指紋は変わらない
        self.assertNotIn("shared/dictionary.py", SERVER_FILES)
        self.assertFalse([f for f in SERVER_FILES if f.startswith("client/")])

    def test_server_files_exist(self) -> None:
        for rel in SERVER_FILES:
            self.assertTrue((ROOT / rel).exists(), rel)


try:
    import client.main  # noqa: F401
    HAVE_CLIENT = True
except Exception:  # サーバーだけの環境（アプリの部品が入っていない）
    HAVE_CLIENT = False


@unittest.skipUnless(HAVE_CLIENT, "アプリの部品が入っていません")
class TestOutdated(unittest.TestCase):
    def _app(self, info: dict, version: str = "1.2.1"):
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.config = mock.MagicMock(server_urls=["http://mac:8765"], server_names={"http://mac:8765": "Mac mini"})
        app.transcriber = mock.MagicMock(server_info={"http://mac:8765": info})
        app._local_fingerprint = "LOCAL"
        app._tailscale_names = lambda: {}
        return app, version

    def _outdated(self, info: dict, version: str = "1.2.1"):
        app, v = self._app(info, version)
        with mock.patch("client.main.__version__", v):
            return app._outdated_servers()

    def test_old_server_is_reported(self) -> None:
        out = self._outdated({"version": "1.1.0", "fingerprint": "OLD"})
        self.assertEqual([(s["name"], s["version"]) for s in out], [("Mac mini（mac）", "1.1.0")])

    def test_same_machine_twice_is_one(self) -> None:
        # Tailscale と家のネットワークの両方で登録された同じパソコンは、1 つにまとめて出す
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        info = {"version": "", "fingerprint": "", "name": "PC5090", "id": "abc"}
        app.config = mock.MagicMock(server_urls=["http://100.1.2.3:8765", "http://192.168.1.5:8765"], server_names={})
        app.transcriber = mock.MagicMock(server_info={"http://100.1.2.3:8765": info, "http://192.168.1.5:8765": info})
        app._local_fingerprint = "LOCAL"
        app._tailscale_names = lambda: {}
        out = app._outdated_servers()
        self.assertEqual([s["name"] for s in out], ["PC5090（100.1.2.3）"])

    def test_old_server_without_name_uses_tailscale(self) -> None:
        # 名前も番号も返さない古い認識役は、Tailscale の一覧から名前を引き、家のネットワーク側もまとめる
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        info = {"version": "", "fingerprint": "", "name": "", "id": ""}
        app.config = mock.MagicMock(server_urls=["http://100.1.2.3:8765", "http://192.168.1.5:8765"], server_names={})
        app.transcriber = mock.MagicMock(server_info={"http://100.1.2.3:8765": info, "http://192.168.1.5:8765": info})
        app._local_fingerprint = "LOCAL"
        app._tailscale_names = lambda: {"100.1.2.3": ("REO-PC5090", "100.1.2.3"), "192.168.1.5": ("REO-PC5090", "100.1.2.3")}
        out = app._outdated_servers()
        self.assertEqual([s["name"] for s in out], ["REO-PC5090（100.1.2.3）"])
        self.assertEqual(app._server_info_for_ui()["http://192.168.1.5:8765"]["name"], "REO-PC5090")

    def test_server_without_version_is_old(self) -> None:
        out = self._outdated({"version": "", "fingerprint": ""})
        self.assertEqual(len(out), 1)

    def test_same_program_is_not_reported(self) -> None:
        # Windows 側だけ直した版で、認識役の版の番号が古くても、中身が同じなら知らせない
        self.assertEqual(self._outdated({"version": "1.1.0", "fingerprint": "LOCAL"}), [])

    def test_newer_server_is_not_reported(self) -> None:
        self.assertEqual(self._outdated({"version": "1.3.0", "fingerprint": "OTHER"}), [])

    def test_unknown_server_is_skipped(self) -> None:
        app, _ = self._app({})
        app.transcriber.server_info = {}
        self.assertEqual(app._outdated_servers(), [])


@unittest.skipUnless(HAVE_SERVER, "fastapi が入っていません")
class TestHealth(unittest.TestCase):
    def test_health_has_version_and_fingerprint(self) -> None:
        engine = mock.MagicMock(active_provider="cpu")
        engine.model_dir.name = "m"
        with mock.patch.object(srv, "engine", engine):
            h = TestClient(srv.app).get("/health").json()
        self.assertEqual(h["version"], read_version(ROOT))
        self.assertEqual(h["fingerprint"], fingerprint(ROOT))


if __name__ == "__main__":
    unittest.main()
