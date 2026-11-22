"""学習候補の×（消す・戻す）と、画面を開くときに窓を増やさないことのテスト。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.correction_learner import CorrectionCandidateStore, LearningCandidate  # noqa: E402


class TestDismiss(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cands.json"

    def test_dismissed_comes_back_and_can_be_restored(self) -> None:
        st = CorrectionCandidateStore(self.path)
        st.record("山駄さん", "山田さん", example_before="山駄さんへ", example_after="山田さんへ")
        st.record("山駄さん", "山田さん")
        st.dismiss("山駄さん", "山田さん")
        self.assertEqual(st.list_candidates(), [])
        gone = st.list_dismissed()
        self.assertEqual([(c.src, c.count, c.example_before) for c in gone], [("山駄さん", 2, "山駄さんへ")])
        self.assertTrue(st.is_dismissed("山駄さん", "山田さん"))

        # 戻す: 回数・例文もそのまま。自動登録の対象にも戻る
        st.restore(gone[0])
        self.assertEqual([(c.src, c.count) for c in st.list_candidates()], [("山駄さん", 2)])
        self.assertFalse(st.is_dismissed("山駄さん", "山田さん"))
        self.assertEqual(st.list_dismissed(), [])

    def test_dismissed_shows_up_again_when_corrected_again(self) -> None:
        st = CorrectionCandidateStore(self.path)
        st.record("abc", "ABC")
        st.dismiss("abc", "ABC")
        self.assertIsNotNone(st.record("abc", "ABC"))  # 二度と出ない、にはしない
        self.assertTrue(st.is_dismissed("abc", "ABC"))  # ただし自動では登録しない
        self.assertEqual(st.list_dismissed(), [])  # いま候補に出ているので、消した一覧には出さない

    def test_saved_and_reloaded(self) -> None:
        st = CorrectionCandidateStore(self.path)
        st.record("a1", "A1")
        st.dismiss("a1", "A1")
        st2 = CorrectionCandidateStore(self.path)
        self.assertEqual([(c.src, c.dst) for c in st2.list_dismissed()], [("a1", "A1")])

    def test_old_never_show_again_becomes_restorable(self) -> None:
        # 前の版の「今後出さない」（ignored）は、消した候補として戻せるようにする
        self.path.write_text(json.dumps({"candidates": [], "ignored": [["古い", "新しい"]]}, ensure_ascii=False),
                             encoding="utf-8")
        st = CorrectionCandidateStore(self.path)
        self.assertIsNotNone(st.record("古い", "新しい"))
        st.dismiss("古い", "新しい")
        self.assertEqual([(c.src, c.dst) for c in st.list_dismissed()], [("古い", "新しい")])

    def test_auto_learn_skips_dismissed(self) -> None:
        # ×で消したことのある直しは、回数がたまっても自動では辞書に登録しない
        from client.main import OnseiManApp

        st = CorrectionCandidateStore(self.path)
        app = OnseiManApp.__new__(OnseiManApp)
        app.candidate_store = st
        app.config = mock.MagicMock(auto_learn_after_count=2)
        app.dictionary = mock.MagicMock(_replacements=[])
        app.ui = mock.MagicMock()
        st.record("ながいことば", "長い言葉")
        st.dismiss("ながいことば", "長い言葉")
        for _ in range(3):
            app._on_detected_corrections([("ながいことば", "長い言葉")], "今日はながいことばの話です", "今日は長い言葉の話です")
        app.dictionary.add_replacement.assert_not_called()
        self.assertEqual([c.src for c in st.list_candidates()], ["ながいことば"])


class TestRewrite(unittest.TestCase):
    """文を丸ごと書き直したものは、聞き間違いの直しではないので学習候補にしない。"""

    def test_whole_rewrite(self) -> None:
        from shared.correction_learner import is_whole_rewrite

        self.assertTrue(is_whole_rewrite("私はさくらさくらちゃんよ", "常に出してれりゃいいだけなんだけどな",
                                         "私はさくらさくらちゃんよ", "常に出してれりゃいいだけなんだけどな。"))
        self.assertTrue(is_whole_rewrite("どっかに隠れてない", "tests", "どっかに隠れてない？", "tests"))
        # 文の一部の聞き間違いの直しは、候補にする
        self.assertFalse(is_whole_rewrite("この番", "本番", "この番だけの話です", "本番だけの話です"))
        self.assertFalse(is_whole_rewrite("グッドファブ", "GitHub", "グッドファブに上げてください", "GitHub に上げてください"))
        # 短い文（言葉 1 つだけ）は、そのまま候補にする
        self.assertFalse(is_whole_rewrite("ぎっと", "git", "ぎっと", "git"))

    def test_old_rewrites_move_to_dismissed(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            st = CorrectionCandidateStore(Path(t) / "c.json")
            st.record("私は私はサック裏", "1回もでない", example_before="私は私はサック裏", example_after="1回もでない")
            st.record("この番", "本番", example_before="この番だけの話です", example_after="本番だけの話です")
            self.assertEqual(st.dismiss_rewrites(), 1)
            self.assertEqual([c.src for c in st.list_candidates()], ["この番"])
            self.assertEqual([c.src for c in st.list_dismissed()], ["私は私はサック裏"])  # 戻せる


class TestCaseMerge(unittest.TestCase):
    def test_add_replacement_uses_existing_case(self) -> None:
        from shared.dictionary import CustomDictionary

        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "dict.json"
            p.write_text(json.dumps({"hotwords": ["GitHub"], "replacements": {"ギッドハブ": "GitHub"}}), encoding="utf-8")
            d = CustomDictionary(p)
            d.add_replacement("グッドファブ", "Github", also_hotword=True)
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(data["replacements"]["グッドファブ"], "GitHub")
            self.assertEqual(data["hotwords"], ["GitHub"])


class TestWindow(unittest.TestCase):
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

    def test_existing_window_is_reused(self) -> None:
        with mock.patch("client.web_ui._find_app_window", return_value=1234), \
             mock.patch("client.web_ui._bring_to_front", return_value=True) as front, \
             mock.patch("client.web_ui.subprocess.Popen") as popen, \
             mock.patch("client.web_ui.webbrowser.open") as wb:
            self.srv.open_window("settings")
        front.assert_called_once_with(1234)
        popen.assert_not_called()
        wb.assert_not_called()
        self.assertEqual(self.srv._pending_tab, "settings")

    def test_opens_with_own_empty_profile(self) -> None:
        # ふだんのプロフィールで開くと、ブラウザが同期を始めて拡張機能をまとめて入れることがある（2026-11-22）
        from client.web_ui import app_browser_args

        prof = Path(self.tmp.name) / "browser_profile"
        args = app_browser_args("edge.exe", "http://127.0.0.1:1/#history", prof)
        self.assertIn(f"--user-data-dir={prof}", args)
        for flag in ("--disable-extensions", "--disable-sync", "--no-first-run"):
            self.assertIn(flag, args)
        self.assertTrue(prof.is_dir())
        self.srv.browser_profile_dir = prof
        with mock.patch("client.web_ui._find_app_window", return_value=0), \
             mock.patch("client.web_ui._find_app_browser", return_value="edge.exe"), \
             mock.patch("client.web_ui.subprocess.Popen") as popen:
            self.srv.open_window("history")
        self.assertIn(f"--user-data-dir={prof}", popen.call_args.args[0])

    def test_new_window_when_none(self) -> None:
        with mock.patch("client.web_ui._find_app_window", return_value=0), \
             mock.patch("client.web_ui._find_app_browser", return_value="edge.exe"), \
             mock.patch("client.web_ui.subprocess.Popen") as popen:
            self.srv.open_window("history")
        popen.assert_called_once()

    def test_ui_request_is_given_once(self) -> None:
        import urllib.request

        url = self.srv.start()
        self.srv._pending_tab = "dict"
        get = lambda: json.loads(urllib.request.urlopen(url + "/api/ui-request").read())  # noqa: E731
        self.assertEqual(get()["tab"], "dict")
        self.assertEqual(get()["tab"], "")


if __name__ == "__main__":
    unittest.main()
