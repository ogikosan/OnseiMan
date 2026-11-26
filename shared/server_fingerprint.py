"""認識役（server/app.py）のプログラムの「指紋」と版。

認識役は /health で自分の版と指紋を返す。Windows のアプリにも同じファイルが入っているので、
手元で同じ指紋を作って比べれば、認識役のプログラムが古いか（更新が要るか）が分かる。
版の番号だけで比べると、Windows 側だけを直した版でも「古い」と出てしまうので、指紋で比べる。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# 認識役が実際に使うファイル（ここを変えると指紋も変わる＝認識役の更新が要る）
SERVER_FILES = (
    "server/app.py",
    "server/requirements.txt",
    "shared/__init__.py",
    "shared/acronyms.py",
    "shared/speechcpp_engine.py",
    "shared/speechcpp_setup.py",
    "shared/audio.py",
    "shared/segmenter.py",
    "shared/dict_format.py",
    "shared/server_fingerprint.py",
)
VERSION_RE = re.compile(r'__version__\s*=\s*"(\d+(?:\.\d+){0,3})"')


def fingerprint(root: Path) -> str:
    """認識役のファイルの中身から作る短い値。改行の違い（Windows / Mac）は無視する。"""
    h = hashlib.sha256()
    for rel in SERVER_FILES:
        h.update(rel.encode("utf-8") + b"\0")
        p = Path(root) / rel
        try:
            data = p.read_bytes().replace(b"\r\n", b"\n")
            if data.startswith(b"\xef\xbb\xbf"):
                data = data[3:]
        except OSError:
            data = b"<missing>"
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()[:16]


def read_version(root: Path) -> str:
    """client/version.py の版（無ければ空）。"""
    try:
        m = VERSION_RE.search((Path(root) / "client" / "version.py").read_text(encoding="utf-8-sig")[:4000])
        return m.group(1) if m else ""
    except OSError:
        return ""
