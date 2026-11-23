"""辞書を預けるときの形の決まり（認識役 server/app.py とアプリの両方で使う）。

認識役の「指紋」（shared/server_fingerprint.py）は、このファイルを含めて作る。
アプリだけで使う合わせ方（shared/dict_sync.py）を直しても、認識役が「古い版」にならないよう分けてある。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

LIST_KEYS = ("hotwords", "fillers", "builtin_off")
MAP_KEYS = ("replacements", "snippets")
SYNC_KEYS = LIST_KEYS + MAP_KEYS
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_NAME = "shared"

# 預ける辞書の大きさの上限（サーバー側と同じ）
MAX_ITEMS = 5000
MAX_WORD_CHARS = 200
MAX_TEXT_CHARS = 10000


# ---------------------------------------------------------------- 形を整える


def clean_data(data: Any, default_fillers: Optional[List[str]] = None) -> Dict[str, Any]:
    """合わせる項目だけを、決まった形に整える。"""
    data = data if isinstance(data, dict) else {}
    out: Dict[str, Any] = {}
    for k in LIST_KEYS:
        v = data.get(k)
        if v is None and k == "fillers" and default_fillers is not None:
            v = list(default_fillers)  # 書いていないときは標準のものを使っている
        if isinstance(v, list):
            seen: List[str] = []
            for w in v:
                w = str(w).strip()[:MAX_WORD_CHARS]
                if w and w not in seen:
                    seen.append(w)
            out[k] = seen[:MAX_ITEMS]
        elif v is None:
            out[k] = []
    for k in MAP_KEYS:
        v = data.get(k)
        if isinstance(v, dict):
            out[k] = {str(a).strip()[:MAX_WORD_CHARS]: str(b)[:MAX_TEXT_CHARS]
                      for a, b in list(v.items())[:MAX_ITEMS] if str(a).strip()}
        else:
            out[k] = {}
    return out
