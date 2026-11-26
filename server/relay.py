"""GPU で聞き取る版の中継（Windows 側）。

認識役は WSL2 の Linux の中（127.0.0.1:8766）で動く。WSL2 の中のポートは、ほかのパソコンからは
見えないので、Windows のポート 8765 で受けて、そのまま中へ渡す。中身は見ない・残さない。
Python の標準の部品だけで動く（server\start_server_gpu_hidden.vbs から起動）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger("onseiman.relay")
IDLE_TIMEOUT_SEC = 120.0
MAX_CONNECTIONS = 64


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), timeout=IDLE_TIMEOUT_SEC)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def make_handler(target_host: str, target_port: int):
    sem = asyncio.Semaphore(MAX_CONNECTIONS)

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        async with sem:
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(target_host, target_port), timeout=5.0)
            except Exception:
                # 認識役がまだ起動中（読み込み中）など
                client_w.close()
                return
            await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))

    return handle


async def serve(listen_host: str, listen_port: int, target_host: str, target_port: int) -> None:
    server = await asyncio.start_server(make_handler(target_host, target_port), listen_host, listen_port)
    logger.info("中継を始めました: %s:%d → %s:%d", listen_host, listen_port, target_host, target_port)
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser(description="OnseiMan GPU 版の中継")
    p.add_argument("--listen-host", default="0.0.0.0")
    p.add_argument("--listen-port", type=int, default=8765)
    p.add_argument("--target-host", default="127.0.0.1")
    p.add_argument("--target-port", type=int, default=8766)
    a = p.parse_args()
    asyncio.run(serve(a.listen_host, a.listen_port, a.target_host, a.target_port))


if __name__ == "__main__":
    main()
