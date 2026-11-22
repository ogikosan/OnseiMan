"""「サーバーをさがす」で、同じパソコンが 2 つの行き方（Tailscale と家のネットワーク）で見つかったときに
1 つにまとめるテスト。Tailscale だけ・家のネットワークだけの人でも、そのまま使えること。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import discovery  # noqa: E402

MAC_TS = {"ip": "100.64.0.10", "name": "Mac-mini", "lan_ips": ["192.168.1.20"]}
PC_TS = {"ip": "100.64.0.11", "name": "Gaming-PC", "lan_ips": []}


def _hit(ip: str, via: str, name: str, ms: int, sid: str = "", host: str = "") -> dict:
    return {"url": f"http://{ip}:8765", "provider": "cpu", "ms": ms, "ready": True, "id": sid, "host": host,
            "name": name, "via": via}


class TestMergeSameMachines(unittest.TestCase):
    def test_same_mac_found_twice_becomes_one_tailscale(self) -> None:
        found = [_hit("192.168.1.20", "家のネットワーク", "192.168.1.20", 5),
                 _hit("100.64.0.10", "Tailscale", "Mac-mini", 30)]
        out = discovery.merge_same_machines(found, [MAC_TS])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["url"], "http://100.64.0.10:8765")       # 家でも外でも使える方を残す
        self.assertEqual(out[0]["aliases"], ["http://192.168.1.20:8765"])
        self.assertIn("Tailscale", out[0]["via"])

    def test_same_machine_by_server_id(self) -> None:
        # Tailscale が家のアドレスを知らなくても、サーバーが返す番号が同じなら同じパソコン
        found = [_hit("192.168.1.30", "家のネットワーク", "192.168.1.30", 5, sid="abc"),
                 _hit("100.64.0.11", "Tailscale", "Gaming-PC", 30, sid="abc")]
        out = discovery.merge_same_machines(found, [PC_TS])
        self.assertEqual([r["url"] for r in out], ["http://100.64.0.11:8765"])

    def test_different_machines_stay_separate(self) -> None:
        found = [_hit("100.64.0.10", "Tailscale", "Mac-mini", 30, sid="a"),
                 _hit("192.168.1.99", "家のネットワーク", "192.168.1.99", 5, sid="b")]
        out = discovery.merge_same_machines(found, [MAC_TS])
        self.assertEqual(len(out), 2)

    def test_tailscale_only(self) -> None:
        out = discovery.merge_same_machines([_hit("100.64.0.10", "Tailscale", "Mac-mini", 30)], [MAC_TS])
        self.assertEqual([(r["url"], r["aliases"]) for r in out], [("http://100.64.0.10:8765", [])])

    def test_home_network_only_without_tailscale(self) -> None:
        # Tailscale を入れていない人: 家のネットワークで見つけたものをそのまま使う
        out = discovery.merge_same_machines([_hit("192.168.1.20", "家のネットワーク", "192.168.1.20", 5)], [])
        self.assertEqual([r["url"] for r in out], ["http://192.168.1.20:8765"])
        self.assertEqual(out[0]["aliases"], [])

    def test_find_servers_without_tailscale_installed(self) -> None:
        with mock.patch.object(discovery, "tailscale_peers", return_value=[]), \
                mock.patch.object(discovery, "lan_hosts", return_value=["192.168.1.20", "192.168.1.21"]), \
                mock.patch.object(discovery, "_probe", side_effect=lambda ip, port, t: (
                    {"url": f"http://{ip}:8765", "provider": "cpu", "ms": 3, "ready": True, "id": "x", "host": "mac"}
                    if ip == "192.168.1.20" else None)):
            out = discovery.find_servers()
        self.assertEqual([(r["url"], r["name"]) for r in out], [("http://192.168.1.20:8765", "mac")])

    def test_tailscale_lan_addresses_are_read(self) -> None:
        import json
        import subprocess

        status = {"Peer": {"k": {"Online": True, "HostName": "Mac-mini", "TailscaleIPs": ["100.64.0.10", "fd7a::1"],
                                 "CurAddr": "192.168.1.20:41641", "Addrs": ["203.0.113.5:41641", "192.168.1.20:41641"]}}}
        fake = subprocess.CompletedProcess([], 0, stdout=json.dumps(status).encode())
        with mock.patch.object(discovery, "_tailscale_exe", return_value="tailscale"), \
                mock.patch.object(discovery.subprocess, "run", return_value=fake):
            peers = discovery.tailscale_peers()
        self.assertEqual(peers, [{"ip": "100.64.0.10", "name": "Mac-mini", "lan_ips": ["192.168.1.20"]}])


class TestDefaults(unittest.TestCase):
    def test_silence_does_not_finish_by_default(self) -> None:
        from client.config import ClientConfig

        # 考えている間に確定してしまい不具合に見えるので、初期設定はオフ
        self.assertFalse(ClientConfig().auto_stop_on_silence)

    def test_server_names_round_trip(self) -> None:
        import tempfile
        from client.config import ClientConfig

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            ClientConfig(server_urls=["http://100.64.0.10:8765"], server_names={"http://100.64.0.10:8765": "Mac-mini"}).save(p)
            self.assertEqual(ClientConfig.load(p).server_names, {"http://100.64.0.10:8765": "Mac-mini"})


if __name__ == "__main__":
    unittest.main()
