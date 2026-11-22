"""OnseiMan の認識サーバーを自動で探す（設定画面の「さがす」ボタン）。

- Tailscale につながっていれば、つながっている相手のパソコン（名前つき）
- このパソコンと同じ家のネットワーク（例: 192.168.1.x）の機械
に、サーバーの入口（既定 8765 番）の /health を短く問い合わせ、OnseiMan のサーバーだけを返す。
問い合わせるのはその 1 か所だけで、ほかには何もしない。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("onseiman.discovery")

PORT = 8765
CREATE_NO_WINDOW = 0x08000000


def _tailscale_exe() -> Optional[str]:
    for p in (
        shutil.which("tailscale"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Tailscale", "tailscale.exe"),
    ):
        if p and os.path.exists(p):
            return p
    return None


def tailscale_peers() -> List[Dict[str, str]]:
    """Tailscale でいまつながっている相手（名前と IP）。Tailscale が無ければ空。"""
    exe = _tailscale_exe()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=6,
                             creationflags=CREATE_NO_WINDOW).stdout
        data = json.loads(out.decode("utf-8", errors="replace") or "{}")
    except Exception as exc:
        logger.debug("Tailscale の一覧を取れませんでした: %s", exc)
        return []
    peers = []
    for p in (data.get("Peer") or {}).values():
        ips = [ip for ip in (p.get("TailscaleIPs") or []) if ":" not in ip]
        if p.get("Online") and ips:
            peers.append({"ip": ips[0], "name": str(p.get("HostName") or ips[0])})
    return peers


def lan_hosts(limit_networks: int = 3) -> List[str]:
    """このパソコンがいる家のネットワーク（/24）の IP 一覧（自分を除く）。"""
    mine = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            mine.add(info[4][0])
    except Exception:
        pass
    nets = []
    for ip in sorted(mine):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # 家のネットワークだけ（Tailscale の 100.x や、自分自身の 127.x は除く）
        if addr.is_private and not addr.is_loopback and not ip.startswith("169.254."):
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
            if net not in nets:
                nets.append(net)
    hosts: List[str] = []
    for net in nets[:limit_networks]:
        hosts += [str(h) for h in net.hosts() if str(h) not in mine]
    return hosts


def _probe(ip: str, port: int, timeout: float) -> Optional[dict]:
    url = f"http://{ip}:{port}"
    t0 = time.perf_counter()
    try:
        r = requests.get(url + "/health", timeout=timeout)
        if r.status_code != 200:
            return None
        data = r.json()
        status = data.get("status")
        # OnseiMan のサーバーの返事か（準備中なら status=initializing、準備できたら model と provider がある）
        if status == "ok" and "provider" in data:
            pass
        elif status != "initializing":
            return None
        return {"url": url, "provider": str(data.get("provider", "")), "ms": round((time.perf_counter() - t0) * 1000),
                "ready": data.get("status") == "ok"}
    except Exception:
        return None


def find_servers(port: int = PORT, timeout: float = 0.8) -> List[dict]:
    """見つかったサーバーを、返事の速い順に返す。各要素: url, name, via（Tailscale / 家のネットワーク）, provider, ms。"""
    targets: Dict[str, Dict[str, str]] = {}
    for p in tailscale_peers():
        targets[p["ip"]] = {"name": p["name"], "via": "Tailscale"}
    for ip in lan_hosts():
        targets.setdefault(ip, {"name": ip, "via": "家のネットワーク"})
    found: List[dict] = []
    with ThreadPoolExecutor(max_workers=64) as pool:
        results = pool.map(lambda ip: (ip, _probe(ip, port, timeout)), list(targets))
        for ip, res in results:
            if res:
                res.update(targets[ip])
                found.append(res)
    # 家のネットワークで見つかったものは、分かればパソコンの名前を付ける
    for r in found:
        if r["name"] == r["url"].split("//")[1].split(":")[0]:
            try:
                r["name"] = socket.gethostbyaddr(r["name"])[0].split(".")[0]
            except Exception:
                pass
    found.sort(key=lambda r: (not r["ready"], r["ms"]))
    logger.info("サーバーをさがしました: %d 台（%s）", len(found), ", ".join(f"{r['name']}" for r in found))
    return found
