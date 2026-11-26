"""speech.cpp（Haas-ja）を使う部分のテスト。本物の speech.cpp の代わりに、同じ決まり（JSON Lines）で返事をする
試し用のプログラムを動かす。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared import speechcpp_setup  # noqa: E402
from shared.speechcpp_engine import SpeechCppEngine, default_device  # noqa: E402

FAKE_WORKER = textwrap.dedent('''
    import base64, json, os, sys
    mode = sys.argv[1]
    print(json.dumps({"type": "ready", "protocol": 4}), flush=True)
    chunks = {}
    for line in sys.stdin:
        m = json.loads(line)
        if m["type"] == "chunk":
            chunks.setdefault(m["id"], b"")
            chunks[m["id"]] += base64.b64decode(m["pcm"])
            continue
        rid = m["id"]
        n = len(chunks.pop(rid, b"")) // 2
        if mode == "crash_once" and not os.path.exists(sys.argv[2]):
            open(sys.argv[2], "w").close()
            sys.exit(3)
        print(json.dumps({"type": "progress", "id": rid, "done": 0.5}), flush=True)
        if mode == "no_keywords" and "keywords" in m:
            print(json.dumps({"type": "error", "id": rid, "error": {"code": "invalid_argument", "option": "keywords", "message": "x"}}), flush=True)
            continue
        kw = m.get("keywords", "").replace("\\n", "|")
        print(json.dumps({"type": "end", "id": rid, "text": f"llmで{n}サンプル{kw}", "stop": "complete"}), flush=True)
''')


class TestEngineWithFakeWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.script = self.tmp / "fake_worker.py"
        self.script.write_text(FAKE_WORKER, encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, mode: str) -> SpeechCppEngine:
        return SpeechCppEngine(self.tmp, command=[sys.executable, str(self.script), mode, str(self.tmp / "crashed")])

    def test_text_comes_back_with_keywords_and_acronyms(self) -> None:
        e = self._engine("ok")
        try:
            # 10 秒（4 秒ずつ 3 回に分けて送る）。進み具合の返事は読み飛ばす。英字の略語は大文字に
            text, ms = e.transcribe(np.zeros(160000, dtype=np.float32), 16000, hotwords="GitHub, LLM")
            self.assertEqual(text, "LLMで160000サンプルGitHub|LLM")
            self.assertGreaterEqual(ms, 0)
            text2, _ = e.transcribe(np.zeros(16000, dtype=np.float32), 16000)
            self.assertEqual(text2, "LLMで16000サンプル")
        finally:
            e.close()

    def test_retry_without_keywords(self) -> None:
        e = self._engine("no_keywords")
        try:
            text, _ = e.transcribe(np.zeros(1600, dtype=np.float32), 16000, hotwords="だめな言葉")
            self.assertEqual(text, "LLMで1600サンプル")
        finally:
            e.close()

    def test_restarts_after_crash(self) -> None:
        e = self._engine("crash_once")
        try:
            text, _ = e.transcribe(np.zeros(1600, dtype=np.float32), 16000)
            self.assertEqual(text, "LLMで1600サンプル")
        finally:
            e.close()


class TestSetup(unittest.TestCase):
    def test_pinned(self) -> None:
        for name, sha in speechcpp_setup.TOOLS.values():
            self.assertIn(speechcpp_setup.VERSION, name)
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
        self.assertRegex(speechcpp_setup.MODEL_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn(speechcpp_setup.MODEL_REVISION, speechcpp_setup.MODEL_URL)

    def test_ready_needs_exe_and_full_model(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            self.assertFalse(speechcpp_setup.ready(tmp))
            exe = speechcpp_setup.tool_dir(tmp) / ("speech.exe" if os.name == "nt" else "speech")
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"x")
            m = speechcpp_setup.model_path(tmp)
            m.parent.mkdir(parents=True)
            m.write_bytes(b"x")   # 途中までしか無い
            self.assertFalse(speechcpp_setup.ready(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_device(self) -> None:
        self.assertEqual(default_device(), "gpu" if sys.platform == "darwin" else "cpu")


class TestServerAuto(unittest.TestCase):
    def test_health_reports_haas_for_speechcpp(self) -> None:
        try:
            from fastapi.testclient import TestClient
            from server import app as srv
        except Exception:
            self.skipTest("サーバー用の部品が無い")
        from unittest import mock

        eng = mock.MagicMock(active_provider="speech.cpp-gpu", model_name="Haas-ja-v1.0")
        with mock.patch.object(srv, "engine", eng), mock.patch.object(srv, "ENGINE_KIND", "speechcpp"):
            h = TestClient(srv.app).get("/health").json()
        self.assertEqual(h["engine"], "haas")      # つなぐ側に先に選ばれる
        self.assertEqual(h["runtime"], "speechcpp")
        self.assertEqual(h["max_segment_sec"], 15.0)


if __name__ == "__main__":
    unittest.main()
