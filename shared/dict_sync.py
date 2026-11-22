"""辞書（dict.json）を、ほかのパソコンと合わせる。

預け先は 2 通り:
- "server": 認識役のパソコン（server/app.py の /dict/<名前>）に預ける
- "folder": OneDrive・Google ドライブ・Dropbox などの同期フォルダにファイルで置く

合わせ方（三方向の合わせ込み）:
前回合わせたときの辞書（base）を覚えておき、このパソコンで足した・消した・変えたものと、
預け先で（ほかのパソコンが）足した・消した・変えたものを、1 件ずつ合わせる。
同じ言葉を両方で変えたときは、このパソコンの変更を使う。
預け先が変わった（別のサーバー・別のフォルダ）ときは、何も消さずに両方を足し合わせる。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("onseiman.dict_sync")

LIST_KEYS = ("hotwords", "fillers", "builtin_off")
MAP_KEYS = ("replacements", "snippets")
SYNC_KEYS = LIST_KEYS + MAP_KEYS
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DEFAULT_NAME = "shared"

# 預ける辞書の大きさの上限（サーバー側と同じ）
MAX_ITEMS = 5000
MAX_WORD_CHARS = 200
MAX_TEXT_CHARS = 10000


class Conflict(Exception):
    """預け先が、読んだあとにほかのパソコンから書き換えられていた。"""


# ---------------------------------------------------------------- 合わせ方


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


def _merge_list(base: List[str], local: List[str], remote: List[str]) -> List[str]:
    removed = set(base) - set(local)
    out = [w for w in remote if w not in removed]
    for w in local:
        if w not in base and w not in out:
            out.append(w)
    return out


def _merge_map(base: Dict[str, str], local: Dict[str, str], remote: Dict[str, str]) -> Dict[str, str]:
    out = dict(remote)
    for k in base:
        if k not in local:
            out.pop(k, None)  # このパソコンで消した
    for k, v in local.items():
        if k not in base or base[k] != v:
            out[k] = v  # このパソコンで足した・変えた
    return out


def merge3(base: Optional[Dict[str, Any]], local: Dict[str, Any], remote: Dict[str, Any]) -> Dict[str, Any]:
    """三方向の合わせ込み。base が None（初めて・預け先が変わった）なら、消さずに足し合わせる。"""
    b = base or {}
    out: Dict[str, Any] = {}
    for k in LIST_KEYS:
        out[k] = _merge_list(list(b.get(k, [])) if base is not None else [], local.get(k, []), remote.get(k, []))
    for k in MAP_KEYS:
        out[k] = _merge_map(dict(b.get(k, {})) if base is not None else {}, local.get(k, {}), remote.get(k, {}))
    return out


# ---------------------------------------------------------------- 預け先


class ServerStore:
    """認識役のパソコン（server/app.py）に預ける。"""

    def __init__(self, url: str, name: str, timeout: float = 4.0) -> None:
        self.url = url.rstrip("/") + "/dict/" + name
        self.timeout = timeout
        self.label = url

    def get(self) -> Tuple[str, int, Dict[str, Any]]:
        import requests

        r = requests.get(self.url, timeout=self.timeout)
        if r.status_code == 404:
            raise RuntimeError("このサーバーは辞書を預かれません（サーバーを新しい版にしてください）")
        r.raise_for_status()
        j = r.json()
        return str(j.get("store", "")), int(j.get("version", 0)), j.get("data") or {}

    def put(self, version: int, data: Dict[str, Any]) -> Tuple[str, int]:
        import requests

        r = requests.put(self.url, json={"version": version, "data": data}, timeout=self.timeout)
        if r.status_code == 409:
            raise Conflict()
        r.raise_for_status()
        j = r.json()
        return str(j.get("store", "")), int(j.get("version", 0))


class FolderStore:
    """同期フォルダ（OneDrive など）にファイルで置く。"""

    def __init__(self, folder: str, name: str) -> None:
        self.folder = Path(folder)
        self.path = self.folder / f"OnseiMan-dict-{name}.json"
        self.label = str(self.folder)

    def _read(self) -> Tuple[str, int, Dict[str, Any]]:
        if not self.folder.is_dir():
            raise RuntimeError(f"フォルダが見つかりません: {self.folder}")
        if not self.path.exists():
            return "", 0, {}
        j = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(j, dict):
            raise RuntimeError("辞書のファイルの形が正しくありません")
        return str(j.get("store", "")), int(j.get("version", 0)), j.get("data") or {}

    def get(self) -> Tuple[str, int, Dict[str, Any]]:
        return self._read()

    def put(self, version: int, data: Dict[str, Any]) -> Tuple[str, int]:
        store, current, _ = self._read()
        if current != version:
            raise Conflict()
        store = store or uuid.uuid4().hex
        body = {"store": store, "version": current + 1, "data": data,
                "_comment": "OnseiMan の辞書。各パソコンの OnseiMan が自動で読み書きします（手で直さないでください）"}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        return store, current + 1


def find_sync_folders() -> List[Dict[str, str]]:
    """このパソコンにある、よく使われる同期フォルダの候補。"""
    home = Path(os.path.expanduser("~"))
    cands: List[Tuple[str, Path]] = []
    for env, label in (("OneDrive", "OneDrive"), ("OneDriveConsumer", "OneDrive"), ("OneDriveCommercial", "OneDrive（仕事）")):
        if os.environ.get(env):
            cands.append((label, Path(os.environ[env])))
    cands += [("Dropbox", home / "Dropbox"), ("iCloud Drive", home / "iCloudDrive"), ("Box", home / "Box")]
    for letter in "GHIJKLMNOPQRSTUVWXYZ":
        for sub in ("マイドライブ", "My Drive"):
            cands.append(("Google ドライブ", Path(f"{letter}:/") / sub))
    out: List[Dict[str, str]] = []
    for label, p in cands:
        try:
            if p.is_dir():
                target = p / "OnseiMan"
                if not any(o["path"] == str(target) for o in out):
                    out.append({"label": label, "path": str(target)})
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- 合わせる係


class DictSync:
    """dict.json を預け先と合わせる。裏で動き、辞書が変わったときと 1 分ごとに合わせる。"""

    INTERVAL_SEC = 60.0
    CHECK_SEC = 3.0

    def __init__(
        self,
        dict_path: Path,
        state_path: Path,
        get_store: Callable[[], Optional[Any]],
        default_fillers: Optional[List[str]] = None,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.dict_path = Path(dict_path)
        self.state_path = Path(state_path)
        self.get_store = get_store
        self.default_fillers = default_fillers
        self.on_changed = on_changed
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_ok: float = 0.0
        self.last_error: str = ""
        self.last_label: str = ""
        self._local_mtime: float = -1.0

    # 状態（前回合わせたときの辞書）。利用者のデータなので logs/ に置く
    def _load_state(self) -> Dict[str, Any]:
        try:
            j = json.loads(self.state_path.read_text(encoding="utf-8"))
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: Dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _read_local(self) -> Tuple[Dict[str, Any], float]:
        if not self.dict_path.exists():
            return {}, -1.0
        mtime = self.dict_path.stat().st_mtime
        return json.loads(self.dict_path.read_text(encoding="utf-8-sig")), mtime

    def _write_local(self, full: Dict[str, Any], merged: Dict[str, Any], mtime: float) -> bool:
        """dict.json に書く。読んだあとに、この間に誰か（画面・学習）が書き換えていたら書かない。"""
        try:
            if self.dict_path.exists() and self.dict_path.stat().st_mtime != mtime:
                return False
        except OSError:
            return False
        out = dict(full)
        out.update(merged)
        tmp = self.dict_path.with_name(self.dict_path.name + ".sync.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.dict_path)
        return True

    def sync_once(self) -> str:
        """1 回合わせる。結果を短い言葉で返す（"off" / "same" / "updated" / "error"）。"""
        store = self.get_store()
        if store is None:
            return "off"
        with self._lock:
            try:
                for _ in range(4):
                    result = self._sync_with(store)
                    if result != "retry":
                        self.last_ok = time.time()
                        self.last_error = ""
                        self.last_label = store.label
                        return result
                raise RuntimeError("ほかのパソコンと書き込みが重なりました。少しあとでもう一度合わせます")
            except Exception as exc:
                self.last_error = str(exc) or exc.__class__.__name__
                self.last_label = store.label
                logger.info("辞書を合わせられませんでした（%s）: %s", store.label, self.last_error)
                return "error"

    def _sync_with(self, store: Any) -> str:
        full, mtime = self._read_local()
        local = clean_data(full, self.default_fillers)
        r_store, r_version, r_data = store.get()
        remote = clean_data(r_data, self.default_fillers) if r_version else None

        state = self._load_state()
        same_place = state.get("place") == store.label and state.get("store") == r_store and r_store
        base = state.get("base") if same_place else None

        if remote is None:
            merged = local  # 預け先が空: このパソコンの辞書をそのまま預ける
        else:
            merged = merge3(base, local, remote)

        new_store, new_version = r_store, r_version
        if remote is None or merged != remote:
            try:
                new_store, new_version = store.put(r_version, merged)
            except Conflict:
                return "retry"
        changed_here = merged != local
        if changed_here:
            if not self._write_local(full, merged, mtime):
                return "retry"  # 画面などが今ちょうど書き換えた: 読み直して合わせ直す
            if self.on_changed:
                try:
                    self.on_changed()
                except Exception:
                    logger.exception("辞書の読み直しに失敗しました")
        self._save_state({"place": store.label, "store": new_store, "version": new_version, "base": merged})
        try:
            self._local_mtime = self.dict_path.stat().st_mtime
        except OSError:
            pass
        if changed_here:
            logger.info("辞書をほかのパソコンと合わせました（%s, 版 %d）", store.label, new_version)
            return "updated"
        return "same" if remote is not None and merged == remote else "updated"

    # 裏で動かす
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="dict-sync", daemon=True)
        self._thread.start()

    def kick(self) -> None:
        """すぐに合わせる（設定を変えたとき・辞書を直したとき）。"""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        next_full = 0.0
        while not self._stop.is_set():
            now = time.time()
            try:
                mtime = self.dict_path.stat().st_mtime if self.dict_path.exists() else -1.0
            except OSError:
                mtime = self._local_mtime
            if self._wake.is_set() or now >= next_full or mtime != self._local_mtime:
                self._wake.clear()
                self._local_mtime = mtime
                self.sync_once()
                next_full = time.time() + self.INTERVAL_SEC
            self._wake.wait(self.CHECK_SEC)

    def status(self) -> Dict[str, Any]:
        return {"last_ok": self.last_ok, "last_error": self.last_error, "place": self.last_label}
