"""このパソコンを「声を文字にする役」にする準備を、設定画面から操作できるようにする（Windows）。

T3 Code の作りにならい、自分では何も入れず、決まった手順（tools\\*.ps1）を裏で動かして、状態を読んで見せる。
- 声を文字にする役（CPU 版・GPU 版）を始める・やめる（進み具合は logs\\host_setup.log。画面に出す）
- ほかのパソコンから使えるようにする（ファイアウォールの許可。管理者の確認を 1 回出す）
- つなぐためのアドレス（家のネットワーク・Tailscale。仮のネットワークは外す）
- Tailscale が入っているか・ログインしているか

時間のかかる確かめ（PowerShell・tailscale）は、裏のスレッドで行い、結果をしばらく覚えておく。
status() はすぐに返す（設定画面の 3 秒ごとの確認を止めない）。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("onseiman.host")

CREATE_NO_WINDOW = 0x08000000
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SERVER_RUN_NAME = "OnseiManServer"
TAILSCALE_DOWNLOAD_URL = "https://tailscale.com/download/windows"
# 仮のネットワーク（WSL・Docker・仮想マシン）。ほかのパソコンからは届かないので、アドレスの一覧から外す
VIRTUAL_ADAPTER_WORDS = ("vethernet", "wsl", "docker", "hyper-v", "vmware", "virtualbox", "loopback", "bluetooth", "vpn")


def is_tailscale_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def pick_addresses(rows: list) -> list:
    """[{"ip", "alias"}] から、ほかのパソコンがつなげるアドレスだけを選ぶ。Tailscale を先に。"""
    out, seen = [], set()
    for r in rows:
        ip = str(r.get("ip") or "")
        alias = str(r.get("alias") or "").lower()
        if not ip or ip in seen or ip.startswith(("127.", "169.254.")):
            continue
        ts = is_tailscale_ip(ip) or "tailscale" in alias
        if not ts and any(w in alias for w in VIRTUAL_ADAPTER_WORDS):
            continue
        seen.add(ip)
        out.append({"ip": ip, "kind": "tailscale" if ts else "lan", "url": f"http://{ip}:8765"})
    out.sort(key=lambda a: a["kind"] != "tailscale")
    return out


def parse_tailscale_status(text: str) -> dict:
    """`tailscale status --json` の結果から、画面に出す分だけ取り出す。"""
    try:
        j = json.loads(text)
    except Exception:
        return {"installed": True, "state": "unknown"}
    state = str(j.get("BackendState") or "")
    me = j.get("Self") or {}
    ips = [ip for ip in (me.get("TailscaleIPs") or []) if "." in str(ip)]
    return {
        "installed": True,
        "state": {"Running": "running", "NeedsLogin": "needs_login", "Stopped": "stopped"}.get(state, state.lower() or "unknown"),
        "ip": ips[0] if ips else "",
        "name": str(me.get("HostName") or ""),
    }


def server_kind_from_run_value(value: str) -> str:
    v = (value or "").lower()
    if "start_server_gpu_hidden" in v:
        return "gpu"
    if "start_server_hidden" in v:
        return "cpu"
    return ""


class HostSetup:
    def __init__(self, root: Path, on_change: Optional[Callable[[], None]] = None) -> None:
        self.root = Path(root)
        self.tools = self.root / "tools"
        self.log_path = self.root / "logs" / "host_setup.log"
        self.on_change = on_change
        self._lock = threading.Lock()
        self._job: Optional[subprocess.Popen] = None
        self._job_kind = ""
        self._job_title = ""
        self._job_result: Optional[int] = None
        self._cache: dict = {}
        self._cache_at: dict = {}
        self._refreshing: set = set()

    # ---------- 小さな道具 ----------
    def _powershell(self, script: str, timeout: float = 20.0) -> str:
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
               "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return r.stdout.decode("utf-8", "replace")

    def _cached(self, key: str, ttl: float, fn: Callable[[], object], default: object) -> object:
        """覚えている結果を返す。古ければ裏で取り直す（終わるまでは前の結果か default）。"""
        now = time.monotonic()
        if key not in self._cache or now - self._cache_at.get(key, 0) > ttl:
            with self._lock:
                start = key not in self._refreshing
                if start:
                    self._refreshing.add(key)
            if start:
                def run() -> None:
                    try:
                        self._cache[key] = fn()
                    except Exception as exc:
                        logger.debug("確かめられませんでした (%s): %s", key, exc)
                        self._cache.setdefault(key, default)
                    finally:
                        self._cache_at[key] = time.monotonic()
                        with self._lock:
                            self._refreshing.discard(key)
                threading.Thread(target=run, daemon=True, name=f"host-{key}").start()
        return self._cache.get(key, default)

    def forget(self, *keys: str) -> None:
        for k in keys or list(self._cache_at):
            self._cache_at.pop(k, None)

    # ---------- 状態 ----------
    def _registered_kind(self) -> str:
        if os.name != "nt":
            return ""
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                value, _ = winreg.QueryValueEx(k, SERVER_RUN_NAME)
        except OSError:
            return ""
        # このフォルダの役だけ（ほかの場所に置いた OnseiMan の登録は数えない）
        if str(self.root).lower() not in str(value).lower():
            return ""
        return server_kind_from_run_value(str(value))

    def _health(self) -> dict:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=0.6) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return {}

    def _check_gpu(self) -> dict:
        script = self.tools / "check_gpu.ps1"
        if not script.exists():
            return {"code": -1, "ok": False, "message": "この版では GPU を確かめられません（新しい版にしてください）"}
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW)
        msg = ""
        try:
            msg = (Path(os.environ.get("TEMP", "")) / "onseiman_gpu_check.txt").read_text(encoding="utf-8-sig").strip()
        except OSError:
            pass
        return {"code": r.returncode, "ok": r.returncode == 0, "message": msg}

    def _firewall(self) -> dict:
        # このフォルダの Python に「許可」があるか。以前に確認を見逃して「止める」ができていないか
        py = str(self.root / "tools" / "python").replace("'", "''")
        out = self._powershell(
            f"$f = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object {{ $_.Program -like '{py}\\*' }}); "
            "$rules = @($f | Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.Enabled -eq 'True' -and $_.Direction -eq 'Inbound' }); "
            "$a = @($rules | Where-Object { $_.Action -eq 'Allow' }); $b = @($rules | Where-Object { $_.Action -eq 'Block' }); "
            "Write-Output ('{0} {1}' -f $a.Count, $b.Count)")
        parts = out.split()
        allowed = int(parts[0]) if parts and parts[0].isdigit() else 0
        blocked = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return {"allowed": allowed > 0 and blocked == 0, "blocked": blocked > 0}

    def _addresses(self) -> list:
        out = self._powershell(
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.IPAddress + \"`t\" + $_.InterfaceAlias }")
        rows = []
        for line in out.splitlines():
            ip, _, alias = line.strip().partition("\t")
            if ip:
                rows.append({"ip": ip, "alias": alias})
        return pick_addresses(rows)

    def _tailscale_exe(self) -> str:
        exe = shutil.which("tailscale")
        if exe:
            return exe
        for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
            p = Path(base) / "Tailscale" / "tailscale.exe"
            if base and p.exists():
                return str(p)
        return ""

    def _tailscale(self) -> dict:
        exe = self._tailscale_exe()
        if not exe:
            return {"installed": False, "state": "not_installed"}
        r = subprocess.run([exe, "status", "--json"], capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW)
        return parse_tailscale_status(r.stdout.decode("utf-8", "replace"))

    def _log_tail(self, n: int = 14) -> list:
        try:
            raw = self.log_path.read_bytes()[-12000:]
        except OSError:
            return []
        # PowerShell が記録に書く文字は、日本語の Windows では cp932、uv などは UTF-8。混ざるので 1 行ずつ見分ける
        lines = []
        for chunk in raw.replace(b"\r", b"\n").split(b"\n"):
            try:
                line = chunk.decode("utf-8")
            except UnicodeDecodeError:
                line = chunk.decode("cp932", "replace")
            lines.append(line.lstrip("﻿").rstrip())
        return [ln for ln in lines if ln.strip() and not ln.startswith(("****", "Windows PowerShell", "トランスクリプト"))][-n:]

    def status(self) -> dict:
        if os.name != "nt":
            return {"supported": False}
        job = self._job
        running_job = job is not None and job.poll() is None
        if job is not None and not running_job and self._job_result is None:
            self._job_result = job.returncode
            self.forget("firewall", "addresses", "health")
            if self.on_change:
                self.on_change()
        # 止まっているときの問い合わせは Windows では遅い（0.5 秒ほど）ので、裏で確かめて 2 秒覚えておく
        health = self._cached("health", 2, self._health, {}) or {}
        registered = self._registered_kind()
        return {
            "supported": True,
            "registered": registered,                    # ログインしたときに自動で動く版（"cpu" / "gpu" / ""）
            "running": health.get("status") == "ok",
            "running_engine": health.get("engine") or ("funasr" if health.get("status") == "ok" else ""),
            "running_version": health.get("version", ""),
            "loading": health.get("status") == "initializing",
            "gpu_ready": (self.root / ".venv_relay" / "onseiman-gpu-ready.txt").exists(),
            "cpu_ready": (self.root / ".venv_server" / "onseiman-ready.txt").exists(),
            "job": {
                "running": running_job,
                "kind": self._job_kind,
                "title": self._job_title,
                "result": self._job_result,
                "log": self._log_tail() if (running_job or self._job_result is not None) else [],
            },
            "gpu": self._cached("gpu", 600, self._check_gpu, None),
            "firewall": self._cached("firewall", 60, self._firewall, None),
            "addresses": self._cached("addresses", 60, self._addresses, []),
            "tailscale": self._cached("tailscale", 30, self._tailscale, None),
        }

    # ---------- 操作 ----------
    def _start_job(self, kind: str, title: str, args: list) -> None:
        if self._job is not None and self._job.poll() is None:
            raise ValueError("いま、ほかの準備をしています。終わるまで待ってください")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "wb")
        try:
            self._job = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", *args],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                cwd=str(self.root), creationflags=CREATE_NO_WINDOW,
            )
        finally:
            log.close()
        self._job_kind, self._job_title, self._job_result = kind, title, None
        logger.info("準備を始めました: %s", title)

    def start_server(self, kind: str) -> None:
        """声を文字にする役を始める（はじめてなら準備から）。途中で管理者の確認が 1 回出る。"""
        if kind not in ("cpu", "gpu"):
            raise ValueError("CPU 版か GPU 版を選んでください")
        if kind == "gpu":
            gpu = self._cache.get("gpu") or self._check_gpu()
            self._cache["gpu"] = gpu
            if not gpu.get("ok"):
                raise ValueError(gpu.get("message") or "NVIDIA の GPU が見つかりません")
        title = "GPU で聞き取る版を準備しています（はじめては 10〜20 分）" if kind == "gpu" else "CPU で聞き取る版を準備しています（はじめては数分〜10 分）"
        self._start_job(kind, title, ["-File", str(self.tools / "installer_run.ps1"), "-Server", kind, "-NoPause"])

    def stop_server(self, remove_gpu: bool = False) -> None:
        """声を文字にする役をやめる（自動で動く登録も外す）。remove_gpu なら GPU 版の Linux も消す。"""
        args = ["-File", str(self.tools / "stop_host_server.ps1")]
        if remove_gpu:
            args.append("-RemoveGpu")
        self._start_job("stop", "声を文字にする役を止めています", args)

    def allow_network(self) -> None:
        """ファイアウォールで、家のネットワークと Tailscale からだけ受け付ける（管理者の確認を出す）。"""
        def run() -> None:
            try:
                self._powershell(
                    "try { $p = Start-Process -FilePath powershell.exe -Verb RunAs -WindowStyle Hidden -Wait -PassThru "
                    f"-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{self.tools / 'admin_setup.ps1'}\" -Root \"{self.root}\" -Firewall'; "
                    "Write-Output $p.ExitCode } catch { Write-Output 'cancel' }", timeout=180)
            finally:
                self.forget("firewall")
                if self.on_change:
                    self.on_change()
        threading.Thread(target=run, daemon=True, name="host-firewall").start()

    def tailscale_action(self, action: str) -> None:
        if action == "install":
            webbrowser.open(TAILSCALE_DOWNLOAD_URL)
        elif action == "login":
            # Tailscale の窓（タスクトレイのアプリ）を開くと、ログインの画面に進める
            exe = self._tailscale_exe()
            gui = Path(exe).with_name("tailscale-ipn.exe") if exe else None
            if gui and gui.exists():
                subprocess.Popen([str(gui)], creationflags=CREATE_NO_WINDOW)
            elif exe:
                subprocess.Popen([exe, "up"], creationflags=CREATE_NO_WINDOW)
            else:
                webbrowser.open(TAILSCALE_DOWNLOAD_URL)
        else:
            raise ValueError("できない操作です")
        self.forget("tailscale")
