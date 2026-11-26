"""GPU で聞き取る版の中継（Windows 側）。

認識役は WSL2 の Linux の中（ポート 8766）で動く。WSL2 の中のポートは、ほかのパソコンからは
見えないので、Windows のポート 8765 で受けて、そのまま中へ渡す。中身は見ない・残さない。
渡す先はまず 127.0.0.1（WSL が Windows に転送してくれる）。パソコンによってはこの転送が不安定なので
（T3 Code も同じ理由で避けている）、届かなければ Linux のアドレス（hostname -I）に渡す。
Python の標準の部品だけで動く（server\start_server_gpu_hidden.vbs から起動）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import time
from typing import Optional

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


def wsl_ip(distro: str) -> Optional[str]:
    """WSL の Linux のアドレス（取れなければ None）。"""
    try:
        r = subprocess.run(["wsl.exe", "-d", distro, "-u", "root", "--", "hostname", "-I"],
                           capture_output=True, timeout=10, creationflags=0x08000000)
        for ip in r.stdout.decode("utf-8", "replace").split():
            if ip.count(".") == 3 and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None


class Targets:
    """渡す先。127.0.0.1 で届かなければ、Linux のアドレス（30 秒覚えておく）。"""

    def __init__(self, host: str, port: int, distro: str = "") -> None:
        self.host, self.port, self.distro = host, port, distro
        self._ip: Optional[str] = None
        self._ip_at = 0.0
        self.prefer_ip = False  # 127.0.0.1 で届かず、Linux のアドレスで届いた

    async def linux_ip(self) -> Optional[str]:
        if not self.distro:
            return None
        if self._ip is None or time.monotonic() - self._ip_at > 30:
            self._ip = await asyncio.get_running_loop().run_in_executor(None, wsl_ip, self.distro)
            self._ip_at = time.monotonic()
        return self._ip

    async def connect(self):
        if not self.prefer_ip:
            try:
                return await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=3.0)
            except Exception:
                if not self.distro:
                    raise
        ip = await self.linux_ip()
        if ip:
            try:
                conn = await asyncio.wait_for(asyncio.open_connection(ip, self.port), timeout=3.0)
                if not self.prefer_ip:
                    logger.info("127.0.0.1 では届かないので、Linux のアドレス %s に渡します", ip)
                self.prefer_ip = True
                return conn
            except Exception:
                self._ip = None  # Linux が起動し直してアドレスが変わったかもしれない。次は取り直す
        if self.prefer_ip:
            self.prefer_ip = False
            return await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=3.0)
        raise ConnectionError("認識役に届きません")


def make_handler(target_host: str, target_port: int, distro: str = ""):
    sem = asyncio.Semaphore(MAX_CONNECTIONS)
    targets = Targets(target_host, target_port, distro)

    async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
        async with sem:
            try:
                up_r, up_w = await targets.connect()
            except Exception:
                # 認識役がまだ起動中（読み込み中）など
                client_w.close()
                return
            await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))

    return handle


async def serve(listen_host: str, listen_port: int, target_host: str, target_port: int, distro: str = "") -> None:
    server = await asyncio.start_server(make_handler(target_host, target_port, distro), listen_host, listen_port)
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
    p.add_argument("--wsl-distro", default="", help="127.0.0.1 で届かないとき、この Linux のアドレスに渡す")
    a = p.parse_args()
    asyncio.run(serve(a.listen_host, a.listen_port, a.target_host, a.target_port, a.wsl_distro))


if __name__ == "__main__":
    main()
