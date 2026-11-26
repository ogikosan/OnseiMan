"""GPU で聞き取る版（Haas-ja）のうち、GPU なしで確かめられる所のテスト。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import relay  # noqa: E402
from shared.haas_engine import MAX_SEGMENT_SEC, _resample, fix_acronyms  # noqa: E402


class TestAcronyms(unittest.TestCase):
    def test_lowercase_acronyms_become_upper(self) -> None:
        cases = {
            "最後にllmで綺麗に": "最後にLLMで綺麗に",
            "cpuでできるか": "CPUでできるか",
            "gpuとvramの話": "GPUとVRAMの話",
            "okです": "OKです",
        }
        for src, want in cases.items():
            self.assertEqual(fix_acronyms(src), want)

    def test_inside_words_kept(self) -> None:
        # 英単語の一部（apple の ap など）や、略語でない言葉はそのまま
        for s in ("appleを買う", "macで動かす", "zoomで話す", "pythonで書く", "aiko"):
            self.assertEqual(fix_acronyms(s), s)


class TestResample(unittest.TestCase):
    def test_to_16k(self) -> None:
        import numpy as np

        x = np.zeros(48000, dtype=np.float32)
        self.assertEqual(_resample(x, 48000).size, 16000)
        self.assertIs(_resample(x, 16000), x)


class TestRelay(unittest.TestCase):
    def test_passes_bytes_both_ways(self) -> None:
        async def run() -> bytes:
            async def echo(r, w):
                data = await r.read(100)
                w.write(b"pong:" + data)
                await w.drain()
                w.close()

            up = await asyncio.start_server(echo, "127.0.0.1", 0)
            up_port = up.sockets[0].getsockname()[1]
            front = await asyncio.start_server(relay.make_handler("127.0.0.1", up_port), "127.0.0.1", 0)
            port = front.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"ping")
            await w.drain()
            got = await asyncio.wait_for(r.read(100), timeout=5)
            w.close()
            front.close()
            up.close()
            return got

        self.assertEqual(asyncio.run(run()), b"pong:ping")

    def test_closes_when_server_is_not_up(self) -> None:
        async def run() -> bytes:
            front = await asyncio.start_server(relay.make_handler("127.0.0.1", 9), "127.0.0.1", 0)
            port = front.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            got = await asyncio.wait_for(r.read(100), timeout=10)
            w.close()
            front.close()
            return got

        self.assertEqual(asyncio.run(run()), b"")


try:
    from fastapi.testclient import TestClient
    from server import app as srv
    HAVE_SERVER = True
except Exception:  # fastapi が無い（クライアントだけの環境）
    HAVE_SERVER = False


@unittest.skipUnless(HAVE_SERVER, "サーバー用の部品が無い")
class TestServerWithHaas(unittest.TestCase):
    def test_health_and_segment_length(self) -> None:
        eng = mock.MagicMock(active_provider="cuda", model_name="Haas-ja-v1.0")
        with mock.patch.object(srv, "engine", eng), mock.patch.object(srv, "ENGINE_KIND", "haas"):
            h = TestClient(srv.app).get("/health").json()
            self.assertEqual(h["engine"], "haas")
            self.assertEqual(h["model"], "Haas-ja-v1.0")
            self.assertEqual(h["max_segment_sec"], MAX_SEGMENT_SEC)
            # 優先語が多くても短くならない
            self.assertEqual(srv.segment_max_sec(",".join(f"w{i}" for i in range(60))), MAX_SEGMENT_SEC)

    def test_funasr_is_default(self) -> None:
        eng = mock.MagicMock(active_provider="cpu")
        eng.model_dir.name = "m"
        with mock.patch.object(srv, "engine", eng):
            h = TestClient(srv.app).get("/health").json()
            self.assertEqual(h["engine"], "funasr")
            self.assertEqual(h["max_segment_sec"], 18.0)


if __name__ == "__main__":
    unittest.main()
