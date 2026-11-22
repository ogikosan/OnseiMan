"""アクティブウィンドウのアプリ名を取得し、アプリ別の仕上げルールを適用する。"""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from pathlib import Path
from typing import Iterable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

# チャット系アプリでは文末の「。」を外すと会話になじみやすい
DEFAULT_CHAT_APPS: list[str] = [
    "Discord.exe",
    "Slack.exe",
    "LINE.exe",
    "ms-teams.exe",
    "Teams.exe",
    "Telegram.exe",
    "Chatwork.exe",
]


def get_window_process_name(hwnd: int) -> str:
    """指定ウィンドウの実行ファイル名（例: 'Discord.exe'）を返す。取得不可なら空文字。"""
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return ""
    try:
        buf_len = wintypes.DWORD(512)
        buf = ctypes.create_unicode_buffer(buf_len.value)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(buf_len)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(hproc)


def strip_chat_trailing_period(text: str) -> str:
    """チャット向け: 末尾の単独の「。」だけを落とす（「。。」「…」や「？」「！」は残す）。"""
    if not text:
        return text
    lines = text.split("\n")
    out = []
    for line in lines:
        # 末尾が1つだけの「。」（直前も「。」ではない）なら削る
        cleaned = re.sub(r"(?<!。)([。])(?=\s*$)", "", line)
        out.append(cleaned)
    return "\n".join(out)


def apply_app_rules(
    text: str,
    process_name: str,
    *,
    enabled: bool = True,
    chat_apps: Iterable[str] = DEFAULT_CHAT_APPS,
) -> str:
    """アクティブアプリに応じた自動調整を適用する。"""
    if not enabled or not text or not process_name:
        return text
    norm_proc = process_name.strip().lower()
    chat_set = {str(a).strip().lower() for a in chat_apps if str(a).strip()}
    if norm_proc in chat_set:
        return strip_chat_trailing_period(text)
    return text
