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
# 家・会社のネットワークのアドレス（RFC 1918）
_HOME_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
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
            # Tailscale が知っている、相手の家のネットワークでのアドレス（同じパソコンかどうかの判定に使う）
            lan = set()
            for addr in [p.get("CurAddr") or ""] + list(p.get("Addrs") or []):
                host = str(addr).rsplit(":", 1)[0].strip("[]")
                try:
                    a = ipaddress.ip_address(host)
                except ValueError:
                    continue
                if a.version == 4 and any(a in n for n in _HOME_NETS):
                    lan.add(host)
            peers.append({"ip": ips[0], "name": str(p.get("HostName") or ips[0]), "lan_ips": sorted(lan)})
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
                "ready": data.get("status") == "ok",
                # 新しいサーバーは、起動ごとの番号とパソコンの名前を返す（同じパソコンかどうかの判定に使う）
                "id": str(data.get("id", ""))[:64], "host": str(data.get("name", ""))[:64],
                # 聞き取りの仕組み（haas = GPU で聞き取る。聞き間違いが少ないので先に選ぶ）
                "engine": str(data.get("engine", "") or "")[:16]}
    except Exception:
        return None


def merge_same_machines(found: List[dict], peers: List[Dict]) -> List[dict]:
    """同じパソコンが「Tailscale」と「家のネットワーク」の両方で見つかったら、1 つにまとめる。

    両方を登録すると、外出先で家のアドレスの待ち時間が毎回かかるなど、動きが不安定になる。
    Tailscale は家の中では自動で直接つながるので、Tailscale のアドレスだけを残す（家でも外でも使える）。
    まとめられた側のアドレスは aliases に入れる（登録済みなら画面で外す）。

    同じパソコンの判定: (1) サーバーが返す番号（id）が同じ (2) Tailscale が知っている相手の家のアドレスと一致
    """
    lan_owner: Dict[str, str] = {}  # 家のアドレス → その相手の Tailscale アドレス
    for p in peers:
        for lan_ip in p.get("lan_ips", []):
            lan_owner[lan_ip] = p["ip"]

    def ip_of(r: dict) -> str:
        return r["url"].split("//")[1].split(":")[0]

    groups: List[List[dict]] = []
    for r in found:
        placed = False
        for g in groups:
            for o in g:
                same_id = bool(r.get("id")) and r.get("id") == o.get("id")
                a, b = ip_of(r), ip_of(o)
                same_ts = lan_owner.get(a) == b or lan_owner.get(b) == a
                if same_id or same_ts:
                    g.append(r)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            groups.append([r])

    merged: List[dict] = []
    for g in groups:
        # Tailscale を優先。同じ種類なら返事の速いほう
        g.sort(key=lambda r: (r.get("via") != "Tailscale", not r.get("ready"), r.get("ms", 0)))
        best = dict(g[0])
        best["aliases"] = [r["url"] for r in g[1:]]
        if best["aliases"] and best.get("via") == "Tailscale":
            best["via"] = "Tailscale（家でも外でも使えます）"
        merged.append(best)
    merged.sort(key=lambda r: (not r["ready"], r.get("engine") != "haas", r["ms"]))
    return merged


def find_servers(port: int = PORT, timeout: float = 1.0) -> List[dict]:
    """見つかったサーバーを、返事の速い順に返す。同じパソコンは 1 つにまとめる。

    各要素: url, name, via（Tailscale / 家のネットワーク）, provider, ms, ready, aliases（同じパソコンの別のアドレス）
    """
    peers = tailscale_peers()
    targets: Dict[str, Dict[str, str]] = {}
    for p in peers:
        targets[p["ip"]] = {"name": p["name"], "via": "Tailscale"}
    for ip in lan_hosts():
        targets.setdefault(ip, {"name": ip, "via": "家のネットワーク"})
    found: List[dict] = []
    with ThreadPoolExecutor(max_workers=96) as pool:
        results = pool.map(lambda ip: (ip, _probe(ip, port, timeout)), list(targets))
        for ip, res in results:
            if res:
                res.update(targets[ip])
                found.append(res)
    merged = merge_same_machines(found, peers)
    # 家のネットワークで見つかったものは、分かればパソコンの名前を付ける
    for r in merged:
        if r["name"] == r["url"].split("//")[1].split(":")[0]:
            if r.get("host"):
                r["name"] = r["host"]
                continue
            try:
                r["name"] = socket.gethostbyaddr(r["name"])[0].split(".")[0]
            except Exception:
                pass
    logger.info("サーバーをさがしました: %d 台（%s）", len(merged), ", ".join(f"{r['name']}" for r in merged))
    return merged
