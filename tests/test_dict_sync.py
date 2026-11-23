"""辞書をほかのパソコンと合わせる仕組みのテスト。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.dict_sync import Conflict, DictSync, FolderStore, clean_data, merge3  # noqa: E402

try:
    from fastapi.testclient import TestClient
    from server import app as srv
    HAVE_SERVER = True
except Exception:
    HAVE_SERVER = False


def d(hot=(), reps=None, snips=None, fillers=(), off=()):
    return {"hotwords": list(hot), "replacements": dict(reps or {}), "snippets": dict(snips or {}),
            "fillers": list(fillers), "builtin_off": list(off)}


class TestMerge(unittest.TestCase):
    def test_both_sides_add(self) -> None:
        base = d(["A"])
        out = merge3(base, d(["A", "B"]), d(["A", "C"]))
        self.assertEqual(out["hotwords"], ["A", "C", "B"])

    def test_delete_here_and_there(self) -> None:
        base = d(["A", "B", "C"], {"x": "1", "y": "2"})
        local = d(["A", "C"], {"x": "1"})            # このパソコンで B と y を消した
        remote = d(["A", "B"], {"x": "1", "y": "2"})  # ほかのパソコンで C を消した
        out = merge3(base, local, remote)
        self.assertEqual(out["hotwords"], ["A"])
        self.assertEqual(out["replacements"], {"x": "1"})

    def test_change_and_conflict_prefers_here(self) -> None:
        base = d(reps={"x": "1", "y": "1"})
        out = merge3(base, d(reps={"x": "2", "y": "1"}), d(reps={"x": "3", "y": "9"}))
        self.assertEqual(out["replacements"], {"x": "2", "y": "9"})

    def test_first_time_never_deletes(self) -> None:
        out = merge3(None, d(["A"], {"x": "1"}), d(["B"], {"y": "2"}))
        self.assertEqual(out["hotwords"], ["B", "A"])
        self.assertEqual(out["replacements"], {"y": "2", "x": "1"})

    def test_clean_data(self) -> None:
        c = clean_data({"hotwords": [" A ", "A", "", 3], "replacements": {"": "x", "k": 1}, "other": 1},
                       default_fillers=["えー"])
        self.assertEqual(c["hotwords"], ["A", "3"])
        self.assertEqual(c["replacements"], {"k": "1"})
        self.assertEqual(c["fillers"], ["えー"])  # 書いていないときは標準のもの
        self.assertNotIn("other", c)


class _PC:
    """1 台のパソコン（dict.json と、前回合わせた記録）。"""

    def __init__(self, root: Path, name: str, store) -> None:
        self.dict_path = root / name / "dict.json"
        self.dict_path.parent.mkdir(parents=True)
        self.write({"_comment": "メモ", "hotwords": [], "replacements": {}})
        self.sync = DictSync(self.dict_path, root / name / "logs" / "dict_sync.json", lambda: store,
                             default_fillers=["えー"])

    def write(self, data: dict) -> None:
        self.dict_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        time.sleep(0.01)

    def read(self) -> dict:
        return json.loads(self.dict_path.read_text(encoding="utf-8"))

    def add_hotword(self, w: str) -> None:
        j = self.read()
        j["hotwords"] = j.get("hotwords", []) + [w]
        self.write(j)


class TestFolderSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "OneDrive" / "OnseiMan").mkdir(parents=True)
        self.store = FolderStore(str(root / "OneDrive" / "OnseiMan"), "shared")
        self.a = _PC(root, "minipc", self.store)
        self.b = _PC(root, "pc5090", self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_on_one_appears_on_other(self) -> None:
        self.a.add_hotword("OnseiMan")
        self.assertNotEqual(self.a.sync.sync_once(), "error")
        self.assertEqual(self.b.sync.sync_once(), "updated")
        self.assertIn("OnseiMan", self.b.read()["hotwords"])
        self.assertEqual(self.b.read()["_comment"], "メモ")  # 合わせる項目以外はそのまま

    def test_delete_propagates(self) -> None:
        self.a.add_hotword("消す言葉")
        self.a.sync.sync_once()
        self.b.sync.sync_once()
        j = self.b.read()
        j["hotwords"].remove("消す言葉")
        self.b.write(j)
        self.b.sync.sync_once()
        self.a.sync.sync_once()
        self.assertNotIn("消す言葉", self.a.read()["hotwords"])

    def test_both_register_before_syncing(self) -> None:
        self.a.sync.sync_once()
        self.b.sync.sync_once()
        self.a.add_hotword("ミニの言葉")
        self.b.add_hotword("5090の言葉")
        self.a.sync.sync_once()
        self.b.sync.sync_once()
        self.a.sync.sync_once()
        for pc in (self.a, self.b):
            self.assertIn("ミニの言葉", pc.read()["hotwords"])
            self.assertIn("5090の言葉", pc.read()["hotwords"])

    def test_existing_dicts_are_joined_first_time(self) -> None:
        # すでにそれぞれで登録していた辞書は、初めて合わせたとき、消さずに足し合わせる
        self.a.write({"hotwords": ["A"], "replacements": {"x": "1"}})
        self.b.write({"hotwords": ["B"], "replacements": {"y": "2"}})
        self.a.sync.sync_once()
        self.b.sync.sync_once()
        self.a.sync.sync_once()
        for pc in (self.a, self.b):
            self.assertEqual(sorted(pc.read()["hotwords"]), ["A", "B"])
            self.assertEqual(pc.read()["replacements"], {"x": "1", "y": "2"})

    def test_unreachable_server_message_is_plain(self) -> None:
        # 認識役が止まっているときは、機械向けの長いエラーではなく、ふつうの言葉で出す
        import requests
        from shared.dict_sync import ServerStore

        store = ServerStore("http://192.0.2.1:8765", "shared", timeout=0.2, display="Mac")
        with mock.patch("requests.get", side_effect=requests.ConnectionError("HTTPConnectionPool(...) Max retries")):
            self.a.sync.get_store = lambda: store
            self.assertEqual(self.a.sync.sync_once(), "error")
        err = self.a.sync.status()["last_error"]
        self.assertIn("認識役のパソコン（Mac）につながりませんでした", err)
        self.assertNotIn("HTTPConnectionPool", err)

    def test_conflict_is_detected(self) -> None:
        self.store.put(0, d(["A"]))
        with self.assertRaises(Conflict):
            self.store.put(0, d(["B"]))

    def test_missing_folder_is_an_error_not_a_crash(self) -> None:
        pc = self.a
        pc.sync.get_store = lambda: FolderStore(str(Path(self.tmp.name) / "ない"), "shared")
        self.assertEqual(pc.sync.sync_once(), "error")
        self.assertIn("フォルダが見つかりません", pc.sync.status()["last_error"])

    def test_off(self) -> None:
        self.a.sync.get_store = lambda: None
        self.assertEqual(self.a.sync.sync_once(), "off")

    def test_does_not_overwrite_a_change_made_meanwhile(self) -> None:
        # 合わせている途中に画面で辞書を直しても、その変更を消さない
        self.b.add_hotword("ほかの言葉")
        self.b.sync.sync_once()
        real_write = self.a.sync._write_local
        calls = []

        def write_after_user_edit(full, merged, mtime):
            if not calls:
                self.a.add_hotword("いま足した言葉")  # 合わせている途中に、画面で足した
            calls.append(1)
            return real_write(full, merged, mtime)

        with mock.patch.object(self.a.sync, "_write_local", side_effect=write_after_user_edit):
            self.assertNotEqual(self.a.sync.sync_once(), "error")
        self.assertGreaterEqual(len(calls), 2)  # 1 回目は書かずに、読み直して合わせ直した
        hot = self.a.read()["hotwords"]
        self.assertIn("いま足した言葉", hot)
        self.assertIn("ほかの言葉", hot)


class TestSettings(unittest.TestCase):
    """設定画面から「辞書を合わせる」を保存するとき。"""

    def setUp(self) -> None:
        from client.config import ClientConfig
        from client.web_ui import WebUIServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = Path(self.tmp.name)
        self.cfg_path = p / "config.json"
        ClientConfig().save(self.cfg_path)
        self.ClientConfig = ClientConfig
        self.ui = WebUIServer(
            dict_path=p / "dict.json", config_path=self.cfg_path, get_history_fn=lambda: [],
            get_status_info=lambda: ("Local CPU", "ralt"), get_learning_candidates_fn=lambda: [],
            on_copy_text=lambda _t: None, on_quick_add_dict=lambda s, d, h: None, on_retranscribe_last=lambda: None,
            on_approve_candidate=lambda _s, _d: None, on_reject_candidate=lambda _s, _d: None,
            on_learn_from_manual_edit=lambda _b, _a: 1, get_hotkey=lambda: "ralt", preferred_port=0,
        )

    def _save(self, **kw):
        body = {**self.ui.get_settings_payload(), **kw}
        self.ui.save_settings_payload(body)
        return self.ClientConfig.load(self.cfg_path)

    def test_folder_is_created_inside_sync_folder(self) -> None:
        target = Path(self.tmp.name) / "OneDrive" / "OnseiMan"
        target.parent.mkdir()
        cfg = self._save(dict_share="folder", dict_share_folder=str(target))
        self.assertEqual((cfg.dict_share, cfg.dict_share_folder), ("folder", str(target)))
        self.assertTrue(target.is_dir())

    def test_bad_inputs(self) -> None:
        with self.assertRaises(ValueError):
            self._save(dict_share="folder", dict_share_folder="")
        with self.assertRaises(ValueError):
            self._save(dict_share="folder", dict_share_folder=str(Path(self.tmp.name) / "ない" / "OnseiMan"))
        with self.assertRaises(ValueError):
            self._save(dict_share="server", server_urls=[])
        with self.assertRaises(ValueError):
            self._save(dict_share="", dict_share_name="../x")

    def test_server_and_off(self) -> None:
        cfg = self._save(dict_share="server", server_urls="http://mac:8765", recognition_place="server")
        self.assertEqual(cfg.dict_share, "server")
        cfg = self._save(dict_share="")
        self.assertEqual(cfg.dict_share, "")


@unittest.skipUnless(HAVE_SERVER, "fastapi が入っていません")
class TestServerDict(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(srv, "DICT_DIR", Path(self.tmp.name) / "dicts")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.c = TestClient(srv.app)

    def test_store_and_conflict(self) -> None:
        r = self.c.get("/dict/shared").json()
        self.assertEqual((r["version"], r["data"]), (0, {}))
        r = self.c.put("/dict/shared", json={"version": 0, "data": d(["A"])})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(self.c.put("/dict/shared", json={"version": 0, "data": d(["B"])}).status_code, 409)
        got = self.c.get("/dict/shared").json()
        self.assertEqual((got["version"], got["data"]["hotwords"]), (1, ["A"]))
        self.assertEqual(got["store"], r.json()["store"])

    def test_names_are_separate_and_checked(self) -> None:
        self.c.put("/dict/reo", json={"version": 0, "data": d(["A"])})
        self.assertEqual(self.c.get("/dict/family").json()["version"], 0)
        for bad in ("..%2Fx", "a b", "x" * 40, "日本語"):
            self.assertIn(self.c.get(f"/dict/{bad}").status_code, (400, 404), bad)

    def test_bad_and_huge_bodies(self) -> None:
        self.assertEqual(self.c.put("/dict/shared", content=b"not json").status_code, 400)
        self.assertEqual(self.c.put("/dict/shared", json={"version": 0, "data": []}).status_code, 400)
        with mock.patch.object(srv, "MAX_DICT_BYTES", 100):
            self.assertEqual(self.c.put("/dict/shared", json={"version": 0, "data": d(["A" * 200])}).status_code, 413)

    def test_only_dictionary_fields_are_kept(self) -> None:
        self.c.put("/dict/shared", json={"version": 0, "data": {**d(["A"]), "evil": "<script>"}})
        self.assertNotIn("evil", self.c.get("/dict/shared").json()["data"])

    def test_client_sync_through_server(self) -> None:
        # アプリの合わせる係が、サーバー経由で 2 台の辞書を合わせられる
        from shared import dict_sync

        class _Req:
            def __init__(self, c):
                self.c = c

            def get(self, url, timeout=None):
                return self.c.get(url.replace("http://mac:8765", ""))

            def put(self, url, json=None, timeout=None):
                return self.c.put(url.replace("http://mac:8765", ""), json=json)

        fake = _Req(self.c)
        with mock.patch.dict(sys.modules, {"requests": fake}):
            store = dict_sync.ServerStore("http://mac:8765", "shared")
            root = Path(self.tmp.name)
            a, b = _PC(root, "a", store), _PC(root, "b", store)
            a.add_hotword("サーバー経由")
            a.sync.sync_once()
            self.assertEqual(b.sync.sync_once(), "updated", b.sync.status())
            self.assertIn("サーバー経由", b.read()["hotwords"])


if __name__ == "__main__":
    unittest.main()
