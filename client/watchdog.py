"""キーボード保護用の見張りプロセス。

OnseiMan 本体は Windows 全体のキー入力を監視するフック (WH_KEYBOARD_LL) を持つ。本体が固まったり
落ちてエラー報告中に止まったりすると、そのあいだ PC 全体のキー入力が本体の応答待ちで詰まる。
そこで本体のフック処理スレッドに心拍を書かせ、別プロセスのこの見張りが心拍の途絶を検知したら
本体を強制終了する（プロセスが消えると Windows がフックを外すのでキーボードが即座に戻る）。
その後、短時間に何度も落ちていなければ本体を自動で再起動する。

本体側: Heartbeat.create() で共有メモリを作り、フックスレッドから beat() を呼ぶ。
見張り: python -m client.watchdog <本体PID>
"""
from __future__ import annotations

import ctypes
import json
import logging
import mmap
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
try:
    from client.paths import DATA_DIR, LOG_DIR, client_command, watchdog_command
except ImportError:  # python client/watchdog.py として直接動かしたとき
    sys.path.insert(0, str(ROOT_DIR))
    from client.paths import DATA_DIR, LOG_DIR, client_command, watchdog_command

SHM_NAME = "Local\\OnseiManKeyboardHeartbeat"
SHM_SIZE = 16  # <Q tick_ms><I state><I pid>
STATE_RUNNING = 1
STATE_QUITTING = 2

STALE_LIMIT_MS = 1500        # この時間フックスレッドが応答しなければ本体を止める
STARTUP_GRACE_MS = 1000      # 見張り起動直後の猶予（心拍は本体が先に書き始めている）
MAX_RESTARTS = 3             # RESTART_WINDOW_SEC 内の自動再起動の上限
RESTART_WINDOW_SEC = 600

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None

KEYEVENTF_KEYUP = 0x0002
MENU_MASK_VK = 0xE8
# Alt / Ctrl / Shift / Win (左右・共通)
MODIFIER_VKS = (0xA4, 0xA5, 0x12, 0xA2, 0xA3, 0x11, 0xA0, 0xA1, 0x10, 0x5B, 0x5C)


def release_stuck_modifiers(log: logging.Logger | None = None) -> list[int]:
    """Windows が押しっぱなしだと思い込んでいる修飾キーを離す。

    本体が修飾キーの「押す」だけ OS に通した直後に落ちると、Alt などが押されたままの扱いになり、
    文字キーがすべてショートカット扱いになって「キーボードが効かない」状態になる。
    Alt/Win を離すときにメニュー/スタートが開かないよう、先に無害なキーを1回送る。
    """
    held = [vk for vk in MODIFIER_VKS if user32.GetAsyncKeyState(vk) & 0x8000]
    if not held:
        return []
    user32.keybd_event(MENU_MASK_VK, 0, 0, 0)
    user32.keybd_event(MENU_MASK_VK, 0, KEYEVENTF_KEYUP, 0)
    for vk in held:
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    if log is not None:
        log.warning("押しっぱなし扱いの修飾キーを解放しました: %s", ", ".join(f"0x{vk:02X}" for vk in held))
    return held


def now_ms() -> int:
    return int(kernel32.GetTickCount64())


class Heartbeat:
    """本体と見張りが共有する心拍（名前付き共有メモリ）。"""

    def __init__(self, mm: mmap.mmap) -> None:
        self._mm = mm

    @classmethod
    def create(cls) -> "Heartbeat":
        hb = cls(mmap.mmap(-1, SHM_SIZE, tagname=SHM_NAME))
        hb.write(STATE_RUNNING)
        return hb

    @classmethod
    def open(cls) -> "Heartbeat":
        return cls(mmap.mmap(-1, SHM_SIZE, tagname=SHM_NAME))

    def write(self, state: int) -> None:
        self._mm.seek(0)
        self._mm.write(struct.pack("<QII", now_ms(), state, os.getpid()))

    def beat(self) -> None:
        self.write(STATE_RUNNING)

    def mark_quitting(self) -> None:
        self.write(STATE_QUITTING)

    def read(self) -> tuple[int, int, int]:
        self._mm.seek(0)
        return struct.unpack("<QII", self._mm.read(SHM_SIZE))


def spawn_watchdog(client_pid: int) -> subprocess.Popen | None:
    """本体から見張りプロセスを起動する。"""
    try:
        return subprocess.Popen(
            watchdog_command(client_pid),
            cwd=str(ROOT_DIR),
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
    except Exception:
        logging.getLogger("onseiman.client").exception("見張りプロセスを起動できませんでした")
        return None


# ---------------------------------------------------------------- watchdog process

def _setup_log() -> logging.Logger:
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("onseiman.watchdog")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(log_dir / "watchdog.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger


def _restart_allowed(log: logging.Logger) -> bool:
    path = LOG_DIR / "watchdog_restarts.json"
    now = time.time()
    try:
        history = [t for t in json.loads(path.read_text(encoding="utf-8")) if now - t < RESTART_WINDOW_SEC]
    except Exception:
        history = []
    if len(history) >= MAX_RESTARTS:
        log.error("直近%d分で%d回落ちているため自動再起動を止めます", RESTART_WINDOW_SEC // 60, len(history))
        return False
    history.append(now)
    try:
        path.write_text(json.dumps(history), encoding="utf-8")
    except Exception:
        pass
    return True


def _restart_client(log: logging.Logger) -> None:
    if not _restart_allowed(log):
        return
    try:
        subprocess.Popen(
            client_command(),
            cwd=str(ROOT_DIR),
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        log.info("OnseiMan を自動再起動しました")
    except Exception:
        log.exception("OnseiMan の自動再起動に失敗しました")


def run_watchdog(client_pid: int, restart: bool = True) -> int:
    log = _setup_log()
    hproc = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, client_pid
    )
    if not hproc:
        log.error("本体プロセス(pid=%d)を開けません", client_pid)
        return 1
    try:
        hb = Heartbeat.open()
    except Exception:
        log.exception("心拍の共有メモリを開けません")
        kernel32.CloseHandle(hproc)
        return 1

    log.info("見張り開始: 本体pid=%d", client_pid)
    started = now_ms()
    last_loop = started
    grace_until = started + STARTUP_GRACE_MS
    try:
        while True:
            # 本体が自分で終了したか
            if kernel32.WaitForSingleObject(hproc, 200) == WAIT_OBJECT_0:
                code = wintypes.DWORD(0)
                kernel32.GetExitCodeProcess(hproc, ctypes.byref(code))
                _tick, state, _pid = hb.read()
                if state == STATE_QUITTING:
                    log.info("本体が正常終了しました")
                    return 0
                log.error("本体が異常終了しました (exit=0x%08X)", code.value)
                release_stuck_modifiers(log)
                if restart:
                    _restart_client(log)
                return 0

            now = now_ms()
            # PC のスリープ復帰などで見張り自身が止まっていた場合は判定をやり直す
            if now - last_loop > 1000:
                grace_until = now + 3000
            last_loop = now
            if now < grace_until:
                continue

            tick, state, pid = hb.read()
            if pid != client_pid or state != STATE_RUNNING:
                continue
            stale = now - tick
            if stale > STALE_LIMIT_MS:
                log.error(
                    "本体のキーボード監視が %d ms 応答していません。キーボードを解放するため強制終了します", stale
                )
                kernel32.TerminateProcess(hproc, 0xDEAD)
                kernel32.WaitForSingleObject(hproc, 3000)
                release_stuck_modifiers(log)
                if restart:
                    _restart_client(log)
                return 0
    finally:
        kernel32.CloseHandle(hproc)


if __name__ == "__main__":
    pid = int(sys.argv[1])
    no_restart = "--no-restart" in sys.argv
    sys.exit(run_watchdog(pid, restart=not no_restart))
