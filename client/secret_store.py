"""API キーを config.json に書くときに、Windows の仕組み（DPAPI）で鍵をかける。

鍵をかけた文字は、同じパソコンの同じ Windows ユーザーでしか戻せない（config.json をほかへ持ち出しても読めない）。
"""
from __future__ import annotations

import base64
import ctypes
import sys
from ctypes import wintypes

PREFIX = "dpapi:"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _call(fn_name: str, data: bytes) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("API キーの保存は Windows だけで使えます")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = getattr(crypt32, fn_name)
    # CryptProtectData / CryptUnprotectData(pDataIn, desc, entropy, reserved, prompt, flags, pDataOut)
    ok = fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))  # 0x1 = 画面を出さない
    if not ok:
        raise RuntimeError("API キーの鍵をかける／外すことができませんでした")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(value: str) -> str:
    if not value:
        return ""
    return PREFIX + base64.b64encode(_call("CryptProtectData", value.encode("utf-8"))).decode("ascii")


def unprotect(stored: str) -> str:
    """鍵を外す。戻せないとき（別のパソコンから持ってきたなど）は空にする。"""
    if not stored or not stored.startswith(PREFIX):
        return ""
    try:
        return _call("CryptUnprotectData", base64.b64decode(stored[len(PREFIX):])).decode("utf-8")
    except Exception:
        return ""
