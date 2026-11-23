"""GitHub の新しい版を見つけて知らせる・更新を始める仕組みのテスト（ネットには出ない）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import github_update as gu  # noqa: E402

NOTES = """# 変更点

## 1.2.0

- 新しいこと

## 1.1.4

- 直したこと

## 1.1.3

- 前のこと
"""


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        pass


def _fake_get(version: str):
    def get(url, timeout=None, headers=None):
        if url.endswith("client/version.py"):
            return _Resp(f'"""x"""\n__version__ = "{version}"\nUPDATE_URL = ""\n')
        return _Resp(NOTES)
    return get


class TestCheck(unittest.TestCase):
    def test_notes_only_newer(self) -> None:
        got = gu.parse_notes(NOTES, "1.1.3")
        self.assertEqual([v for v, _ in got], ["1.2.0", "1.1.4"])
        self.assertIn("新しいこと", got[0][1])

    def test_newer_version_found(self) -> None:
        with mock.patch("requests.get", _fake_get("1.2.0")):
            info = gu.check("1.1.3")
        self.assertEqual((info["version"], info["current"]), ("1.2.0", "1.1.3"))
        self.assertIn("直したこと", info["notes"])
        self.assertNotIn("前のこと", info["notes"])

    def test_same_or_older_is_none(self) -> None:
        with mock.patch("requests.get", _fake_get("1.1.3")):
            self.assertIsNone(gu.check("1.1.3"))
        with mock.patch("requests.get", _fake_get("1.0.0")):
            self.assertIsNone(gu.check("1.1.3"))

    def test_strange_version_is_rejected(self) -> None:
        with mock.patch("requests.get", _fake_get("1.2.0<script>")):
            with self.assertRaises(ValueError):
                gu.check("1.1.3")

    def test_notes_failure_still_reports_version(self) -> None:
        def get(url, timeout=None, headers=None):
            if url.endswith("client/version.py"):
                return _Resp('__version__ = "1.2.0"')
            raise OSError("no network")
        with mock.patch("requests.get", get):
            info = gu.check("1.1.3")
        self.assertEqual((info["version"], info["notes"]), ("1.2.0", ""))

    def test_dev_folder_and_launch(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertFalse(gu.is_dev_folder(root))
            (root / ".git").mkdir()
            self.assertTrue(gu.is_dev_folder(root))
            (root / "tools").mkdir()
            (root / "tools" / "update.ps1").write_text("x", encoding="utf-8")
            with mock.patch("client.github_update.subprocess.Popen") as popen:
                gu.launch_update(root, 4321)
            args = popen.call_args.args[0]
            self.assertIn("-WaitPid", args)
            self.assertEqual(args[args.index("-WaitPid") + 1], "4321")
            self.assertIn("-Restart", args)


class TestApp(unittest.TestCase):
    """本体: 見つけたら知らせ、開発用のフォルダでは更新を始めない。"""

    def _app(self):
        from client.main import OnseiManApp

        app = OnseiManApp.__new__(OnseiManApp)
        app.ui = mock.MagicMock()
        app._tray = None
        app.update_info = None
        app._update_notified = ""
        return app

    def test_found_and_notified_once(self) -> None:
        app = self._app()
        info = {"version": "9.9.9", "current": "1.1.3", "notes": "x", "url": ""}
        with mock.patch("client.github_update.check", return_value=info):
            app._check_github_update()
            app._check_github_update()
        self.assertEqual(app.update_info["version"], "9.9.9")
        self.assertEqual(app.ui.show_status.call_count, 1)  # 同じ版は 1 回だけ知らせる

    def test_offline_keeps_quiet(self) -> None:
        app = self._app()
        with mock.patch("client.github_update.check", side_effect=OSError("offline")):
            app._check_github_update(manual=False)
        app.ui.show_status.assert_not_called()

    def test_dev_folder_can_run_and_is_marked(self) -> None:
        # 開発用のフォルダでも押せる（update.ps1 が、作業中の変更などがあれば何も変えずに止める）
        app = self._app()
        app.update_info = {"version": "9.9.9", "current": "1.1.3", "notes": "", "url": ""}
        with mock.patch("client.github_update.is_dev_folder", return_value=True):
            p = app._update_payload()
            self.assertTrue(p["can_run"])
            self.assertTrue(p["dev"])

    def test_no_update_cannot_run(self) -> None:
        app = self._app()
        with self.assertRaises(ValueError):
            app._run_github_update()


class TestWebUI(unittest.TestCase):
    def setUp(self) -> None:
        from client.web_ui import WebUIServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name)
        self.srv = WebUIServer(
            dict_path=p / "dict.json", config_path=p / "config.json", get_history_fn=lambda: [],
            get_status_info=lambda: ("x", "ralt"), get_learning_candidates_fn=lambda: [],
            on_copy_text=lambda _t: None, on_quick_add_dict=lambda s, d, h: None, on_retranscribe_last=lambda: None,
            on_approve_candidate=lambda a, b: None, on_reject_candidate=lambda a, b: None,
            on_learn_from_manual_edit=lambda a, b: 1, get_hotkey=lambda: "ralt", preferred_port=0,
        )
        self.addCleanup(self.srv.stop)
        self.url = self.srv.start()
        self.runs = []
        self.srv.get_update_fn = lambda: {"version": "9.9.9", "current": "1.1.3", "notes": "", "can_run": True}
        self.srv.on_run_update = lambda: self.runs.append(1)

    def _post(self, path: str):
        req = urllib.request.Request(self.url + path, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.url})
        return json.loads(urllib.request.urlopen(req).read())

    def test_state_has_update_and_run(self) -> None:
        state = json.loads(urllib.request.urlopen(self.url + "/api/state").read())
        self.assertEqual(state["update"]["version"], "9.9.9")
        from client.version import __version__
        self.assertEqual(state["version"], __version__)  # 設定の「新しい版にする」に、いまの版を出す
        self.assertTrue(self._post("/api/update/run")["ok"])
        self.assertEqual(self.runs, [1])


if __name__ == "__main__":
    unittest.main()
