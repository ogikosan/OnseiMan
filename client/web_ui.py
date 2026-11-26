"""OnseiMan ローカル Web UI サーバー（68_Agent_board 統一すりガラスデザイン）。

標準ライブラリの ThreadingHTTPServer のみで 127.0.0.1 上に管理画面（履歴・学習候補・辞書・設定）を提供し、
Windows 標準の Microsoft Edge (--app モード) でネイティブアプリ風のウィンドウとして開く。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from client.config import ClientConfig
from client.version import __version__
from shared.fillers import DEFAULT_FILLERS
from shared.normalize import BUILTIN_TERMS

logger = logging.getLogger("onseiman.web_ui")

WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_MIC_LABEL = "Windows の既定のマイク"


def _find_app_browser() -> Optional[str]:
    """--app モード対応ブラウザ（Edge 優先、次点 Chrome）の実行ファイルを探す。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


WINDOW_TITLE = "OnseiMan"  # client/web/index.html の <title>


def app_browser_args(browser: str, target: str, profile_dir: Optional[Path]) -> List[str]:
    """画面をブラウザのアプリ表示で開く命令。

    ふだんのプロフィールで開くと、ブラウザがアカウントの同期を始め、拡張機能をまとめて入れたり、
    拡張機能のボタン（AI ツールなど）が画面に出たりする（2026-11-22、ふだん使わない Edge に 28 個入った）。
    そこで OnseiMan 専用の空のプロフィールで開き、同期・拡張機能・初回の案内を止める。
    """
    args = [browser, f"--app={target}", "--window-size=1040,740"]
    if profile_dir is not None:
        try:
            Path(profile_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            return args
        args += [
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-features=msImplicitSignin,msEdgeSidebarV2,EdgeCollections,msEdgeShopping",
        ]
    return args


def _find_app_window() -> int:
    """すでに開いている OnseiMan の画面（Edge / Chrome のアプリ表示）の窓。無ければ 0。"""
    found = _find_app_windows(first_only=True)
    return found[0] if found else 0


def _find_app_windows(first_only: bool = False) -> List[int]:
    """開いている OnseiMan の画面の窓をすべて。"""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: List[int] = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value != WINDOW_TITLE:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value.startswith("Chrome_WidgetWin"):  # 同じ名前のフォルダの窓などは除く
            found.append(int(hwnd))
            return not first_only
        return True

    try:
        user32.EnumWindows(proc_type(_each), 0)
    except Exception:
        return []
    return found


def _close_windows(hwnds: List[int], timeout: float = 3.0) -> None:
    """窓に「閉じて」と伝え、閉じるまで少し待つ。"""
    import ctypes
    import time

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for hwnd in hwnds:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    end = time.monotonic() + timeout
    while time.monotonic() < end and any(user32.IsWindow(h) for h in hwnds):
        time.sleep(0.1)


def _bring_to_front(hwnd: int) -> bool:
    """窓を前に出す（最小化されていれば元に戻す）。"""
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    try:
        from client.paste import _restore_foreground

        return bool(_restore_foreground(hwnd))
    except Exception:
        return bool(user32.SetForegroundWindow(hwnd))


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


class WebUIServer:
    """127.0.0.1 上で OnseiMan 管理画面と REST API を提供する軽量サーバー。"""

    def __init__(
        self,
        *,
        dict_path: Path,
        config_path: Path,
        audio_dir: Optional[Path] = None,
        get_history_fn: Callable[[], list],
        get_status_info: Callable[[], tuple[str, str]],
        get_learning_candidates_fn: Callable[[], list],
        on_copy_text: Callable[[str], None],
        on_quick_add_dict: Callable[[str, str, bool], None],
        on_retranscribe_last: Callable[[], None],
        on_retry_entry: Optional[Callable[[str, str], None]] = None,
        on_approve_candidate: Callable[[str, str], None],
        on_reject_candidate: Callable[[str, str], None],
        on_restore_candidate: Optional[Callable[[dict], None]] = None,
        on_learn_from_manual_edit: Callable[[str, str], int],
        on_dict_saved: Optional[Callable[[], None]] = None,
        on_config_saved: Optional[Callable[[], None]] = None,
        get_dict_sync_status: Optional[Callable[[], dict]] = None,
        on_dict_sync_now: Optional[Callable[[], str]] = None,
        list_microphones: Optional[Callable[[], List[str]]] = None,
        get_hotkey: Optional[Callable[[], str]] = None,
        on_change_hotkey: Optional[Callable[[], None]] = None,
        get_ai_hotkey: Optional[Callable[[], str]] = None,
        on_change_ai_hotkey: Optional[Callable[[], None]] = None,
        on_clear_ai_hotkey: Optional[Callable[[], None]] = None,
        is_startup_enabled: Optional[Callable[[], bool]] = None,
        set_startup_enabled: Optional[Callable[[bool], None]] = None,
        preferred_port: int = 18765,
        browser_profile_dir: Optional[Path] = None,
    ) -> None:
        self.dict_path = dict_path
        self.config_path = config_path
        self.audio_dir = audio_dir
        self.get_history_fn = get_history_fn
        self.get_status_info = get_status_info
        self.get_learning_candidates_fn = get_learning_candidates_fn
        self.on_copy_text = on_copy_text
        self.on_quick_add_dict = on_quick_add_dict
        self.on_retranscribe_last = on_retranscribe_last
        self.on_retry_entry = on_retry_entry
        self.on_approve_candidate = on_approve_candidate
        self.on_reject_candidate = on_reject_candidate
        self.on_restore_candidate = on_restore_candidate
        self.on_learn_from_manual_edit = on_learn_from_manual_edit
        self.on_dict_saved = on_dict_saved
        self.on_config_saved = on_config_saved
        self.get_dict_sync_status = get_dict_sync_status
        self.on_dict_sync_now = on_dict_sync_now
        self.list_microphones = list_microphones
        self.get_hotkey = get_hotkey
        self.on_change_hotkey = on_change_hotkey
        self.get_ai_hotkey = get_ai_hotkey
        self.on_change_ai_hotkey = on_change_ai_hotkey
        self.on_clear_ai_hotkey = on_clear_ai_hotkey
        self.is_startup_enabled = is_startup_enabled
        self.set_startup_enabled = set_startup_enabled
        self._preferred_port = preferred_port
        # 画面を開くときに使う、OnseiMan 専用のブラウザのプロフィール（None ならふだんのプロフィール）
        self.browser_profile_dir = browser_profile_dir

        self._server: Optional[ThreadingHTTPServer] = None
        # 開いている画面に切り替えてほしいタブ（トレイのメニューから押したとき）
        self._pending_tab: str = ""
        self._thread: Optional[threading.Thread] = None
        self._port: int = 0
        # この OnseiMan を起動する前から開いていた画面（前の起動の画面）。中身が古いので、出すときは開き直す
        self._stale_windows: set = set(_find_app_windows())

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def start(self) -> str:
        if self._server is not None:
            return self.url

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("WebUI: " + format, *args)

            # ---- 守り: この画面はこのパソコンの中だけで使う管理画面。
            # ブラウザで開いているほかのサイトから、設定を書き換えたり履歴を読んだりされないようにする。
            def _host_ok(self) -> bool:
                """Host が 127.0.0.1 / localhost のときだけ受け付ける（DNS リバインディング対策）。"""
                host = (self.headers.get("Host") or "").strip().lower()
                name, _, port = host.rpartition(":") if host.count(":") == 1 else (host, "", "")
                return name in ("127.0.0.1", "localhost") and (not port or port == str(owner._port))

            def _origin_ok(self) -> bool:
                """書き込みは、この画面自身（同じ出どころ）からのものだけ（CSRF 対策）。"""
                origin = (self.headers.get("Origin") or "").strip().lower()
                if not origin:
                    return True  # ブラウザ以外（テスト・自分のツール）
                return origin in (f"http://127.0.0.1:{owner._port}", f"http://localhost:{owner._port}")

            def _fetch_site_ok(self) -> bool:
                """ブラウザが付ける「どこからの要求か」を見て、ほかのサイト（別のポートの画面も含む）からの
                読み込み・埋め込みを断る。付いていない要求（ブラウザ以外）は通す。"""
                site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                return site in ("", "same-origin", "none")

            def _deny(self) -> None:
                self.send_error(HTTPStatus.FORBIDDEN)

            def end_headers(self) -> None:
                # どの返事にも付ける守り: ほかのサイトへの埋め込み（クリックの横取り）と、
                # 画面に紛れ込んだスクリプトの実行・外への送信を防ぐ
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; "
                    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'",
                )
                super().end_headers()

            def _send_json(self, data: Any, status: int = 200) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read_json_body(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return {}
                if length > 2_000_000:
                    raise ValueError("送られたデータが大きすぎます")
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}

            def _serve_static(self, rel_path: str) -> None:
                rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
                target = (WEB_DIR / rel).resolve()
                try:
                    target.relative_to(WEB_DIR.resolve())
                except ValueError:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                if not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = target.read_bytes()
                ctype, _ = mimetypes.guess_type(str(target))
                if not ctype:
                    ctype = "application/octet-stream"
                if ctype.startswith(("text/", "application/javascript")):
                    ctype += "; charset=utf-8"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache" if not rel.endswith(".jpg") else "public, max-age=86400")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_audio(self, name: str) -> None:
                if not owner.audio_dir or not name or "/" in name or "\\" in name or ".." in name:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                base = owner.audio_dir.resolve()
                target = (base / name).resolve()
                try:
                    target.relative_to(base)
                except ValueError:
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                if target.suffix.lower() != ".wav" or not target.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                data = target.read_bytes()
                total = len(data)
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    try:
                        part = rng[6:].split(",")[0].strip()
                        start_s, end_s = part.split("-", 1)
                        start = int(start_s) if start_s else 0
                        end = int(end_s) if end_s else total - 1
                        start = max(0, min(start, total - 1))
                        end = max(start, min(end, total - 1))
                        chunk = data[start : end + 1]
                        self.send_response(HTTPStatus.PARTIAL_CONTENT)
                        self.send_header("Content-Type", "audio/wav")
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                        return
                    except Exception:
                        pass
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(total))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                if not self._host_ok() or not self._fetch_site_ok():
                    return self._deny()
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    if path == "/api/state":
                        self._send_json(owner.get_state_payload())
                    elif path == "/api/dict":
                        self._send_json(owner.get_dict_payload())
                    elif path == "/api/settings":
                        self._send_json(owner.get_settings_payload())
                    elif path == "/api/ui-request":
                        tab, owner._pending_tab = owner._pending_tab, ""
                        self._send_json({"tab": tab})
                    elif path == "/api/dict-sync":
                        self._send_json(owner.get_dict_sync_payload())
                    elif path == "/api/host":
                        host = getattr(owner, "host_setup", None)
                        self._send_json(host.status() if host else {"supported": False})
                    elif path.startswith("/api/audio/"):
                        self._serve_audio(path[len("/api/audio/") :])
                    else:
                        self._serve_static(path)
                except Exception as exc:
                    logger.exception("WebUI GET %s エラー", path)
                    self._send_json({"ok": False, "error": str(exc)}, status=500)

            def do_POST(self) -> None:
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                # JSON 以外の送り方（フォームなど）はほかのサイトから送れてしまうので受け付けない
                if (
                    not self._host_ok()
                    or not self._origin_ok()
                    or not self._fetch_site_ok()
                    or ctype not in ("application/json", "")
                ):
                    return self._deny()
                if ctype == "" and int(self.headers.get("Content-Length", "0") or 0) > 0:
                    return self._deny()
                parsed = urlparse(self.path)
                path = parsed.path
                try:
                    body = self._read_json_body()
                    if path == "/api/copy":
                        text = str(body.get("text", ""))
                        if text:
                            owner.on_copy_text(text)
                        self._send_json({"ok": True})
                    elif path == "/api/retranscribe":
                        owner.on_retranscribe_last()
                        self._send_json({"ok": True})
                    elif path == "/api/find_servers":
                        from client.discovery import find_servers

                        self._send_json({"servers": find_servers()})
                    elif path == "/api/retry":
                        if owner.on_retry_entry:
                            owner.on_retry_entry(str(body.get("timestamp", "")), str(body.get("audio", "")))
                        self._send_json({"ok": True})
                    elif path == "/api/quick_dict":
                        src = str(body.get("src", "")).strip()
                        dst = str(body.get("dst", "")).strip()
                        also_hotword = bool(body.get("also_hotword", True))
                        if not src or not dst:
                            self._send_json({"ok": False, "error": "誤認識語と正しい言葉を入力してください"}, status=400)
                            return
                        owner.on_quick_add_dict(src, dst, also_hotword)
                        self._send_json({"ok": True})
                    elif path == "/api/candidates/approve":
                        src = str(body.get("src", "")).strip()
                        dst = str(body.get("dst", "")).strip()
                        if src and dst:
                            owner.on_approve_candidate(src, dst)
                        self._send_json({"ok": True})
                    elif path == "/api/candidates/reject":
                        src = str(body.get("src", "")).strip()
                        dst = str(body.get("dst", "")).strip()
                        if src and dst:
                            owner.on_reject_candidate(src, dst)
                        self._send_json({"ok": True})
                    elif path == "/api/update/run":
                        fn_run = getattr(owner, "on_run_update", None)
                        if not fn_run:
                            raise ValueError("この版では使えません")
                        fn_run()
                        self._send_json({"ok": True})
                    elif path == "/api/update/check":
                        fn_chk = getattr(owner, "on_check_update", None)
                        fn_upd = getattr(owner, "get_update_fn", None)
                        if fn_chk:
                            fn_chk()
                        self._send_json({"ok": True, "update": fn_upd() if fn_upd else None})
                    elif path.startswith("/api/host/"):
                        # このパソコンを「声を文字にする役」にする（設定画面の操作）
                        host = getattr(owner, "host_setup", None)
                        if host is None:
                            raise ValueError("このパソコンでは使えません")
                        action = path[len("/api/host/"):]
                        if action == "start":
                            host.start_server(str(body.get("kind", "")))
                        elif action == "stop":
                            host.stop_server(remove_gpu=bool(body.get("remove_gpu")))
                        elif action == "firewall":
                            host.allow_network()
                        elif action == "tailscale":
                            host.tailscale_action(str(body.get("action", "")))
                        else:
                            raise ValueError("できない操作です")
                        self._send_json({"ok": True, **host.status()})
                    elif path == "/api/candidates/restore":
                        if owner.on_restore_candidate and str(body.get("src", "")).strip() and str(body.get("dst", "")).strip():
                            owner.on_restore_candidate(body)
                        self._send_json({"ok": True})
                    elif path == "/api/learn_manual":
                        before = str(body.get("before", ""))
                        after = str(body.get("after", ""))
                        added = owner.on_learn_from_manual_edit(before, after)
                        self._send_json({"ok": True, "added": added})
                    elif path == "/api/dict":
                        owner.save_dict_payload(body)
                        self._send_json({"ok": True})
                    elif path == "/api/settings":
                        owner.save_settings_payload(body)
                        self._send_json({"ok": True})
                    elif path == "/api/dict-sync/now":
                        result = owner.on_dict_sync_now() if owner.on_dict_sync_now else "off"
                        self._send_json({"ok": result != "error", "result": result, **owner.get_dict_sync_payload()})
                    elif path == "/api/capture_hotkey":
                        if owner.on_change_hotkey:
                            owner.on_change_hotkey()
                        self._send_json({"ok": True})
                    elif path == "/api/capture_ai_hotkey":
                        if owner.on_change_ai_hotkey:
                            owner.on_change_ai_hotkey()
                        self._send_json({"ok": True})
                    elif path == "/api/capture_extra_hotkey":
                        fn_cap = getattr(owner, "on_capture_extra_hotkey", None)
                        if fn_cap:
                            fn_cap()
                        self._send_json({"ok": True})
                    elif path == "/api/clear_extra_hotkey":
                        fn_clr = getattr(owner, "on_clear_extra_hotkey", None)
                        if fn_clr:
                            fn_clr()
                        self._send_json({"ok": True})
                    elif path == "/api/clear_ai_hotkey":
                        if owner.on_clear_ai_hotkey:
                            owner.on_clear_ai_hotkey()
                        self._send_json({"ok": True})
                    else:
                        self._send_json({"ok": False, "error": "Not found"}, status=404)
                except ValueError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                except Exception as exc:
                    logger.exception("WebUI POST %s エラー", path)
                    self._send_json({"ok": False, "error": str(exc)}, status=500)

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", self._preferred_port), Handler)
        except OSError:
            srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.daemon_threads = True
        self._server = srv
        self._port = int(srv.server_address[1])
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True, name="OnseiManWebUI")
        self._thread.start()
        logger.info("Web UI サーバーを起動しました: %s", self.url)
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def open_window(self, tab: str = "history") -> None:
        url = self.start()
        # すでに開いていれば、新しく開かずに前に出す（トレイを押すたびに窓が増えないように）
        hwnd = _find_app_window()
        if hwnd and self._stale_windows:
            # 前の起動（前の版）のときから開いたままの画面は、ボタンが今の OnseiMan とかみ合わないことがあるので開き直す
            stale = [h for h in _find_app_windows() if h in self._stale_windows]
            self._stale_windows = set()
            if stale:
                logger.info("前の起動のときから開いていた画面を開き直します")
                _close_windows(stale)
                hwnd = _find_app_window()
        if hwnd:
            self._pending_tab = tab or ""
            if not _bring_to_front(hwnd):
                logger.info("OnseiMan の画面を前に出せませんでした（開いたままです）")
            return
        target = f"{url}/#{tab}" if tab else url
        browser = _find_app_browser()
        if browser:
            try:
                subprocess.Popen(
                    app_browser_args(browser, target, self.browser_profile_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except Exception as exc:
                logger.warning("アプリモード起動に失敗したため通常ブラウザで開きます: %s", exc)
        webbrowser.open(target)

    # -------------------- Payload Builders --------------------

    def _stats(self) -> Dict[str, Any]:
        fn = getattr(self, "get_stats_fn", None)
        try:
            return fn() if fn else {}
        except Exception:
            return {}

    def get_state_payload(self) -> Dict[str, Any]:
        backend, hotkey_info = self.get_status_info()
        history = []
        for e in self.get_history_fn()[:200]:
            audio_name = getattr(e, "audio", "")
            has_audio = bool(
                audio_name
                and self.audio_dir is not None
                and (self.audio_dir / audio_name).is_file()
            )
            history.append(
                {
                    "timestamp": e.timestamp,
                    "backend": e.backend,
                    "elapsed_ms": round(float(e.elapsed_ms), 1),
                    "duration_sec": round(float(e.duration_sec), 2),
                    "raw_text": getattr(e, "raw_text", "") or e.text,
                    "text": e.text,
                    "audio": audio_name,
                    "has_audio": has_audio,
                    "failed": getattr(e, "failed", ""),
                }
            )
        candidates = []
        for c in self.get_learning_candidates_fn():
            candidates.append(
                {
                    "src": c.src,
                    "dst": c.dst,
                    "count": int(c.count),
                    "example_before": c.example_before,
                    "example_after": c.example_after,
                }
            )
        dismissed = []
        fn_d = getattr(self, "get_dismissed_candidates_fn", None)
        for c in (fn_d() if fn_d else [])[:100]:
            dismissed.append({"src": c.src, "dst": c.dst, "count": int(c.count),
                              "example_before": c.example_before, "example_after": c.example_after})
        update = None
        fn_u = getattr(self, "get_update_fn", None)
        if fn_u:
            try:
                update = fn_u()
            except Exception:
                update = None
        server_name = ""
        fn = getattr(self, "get_server_name_fn", None)
        if fn:
            try:
                server_name = fn() or ""
            except Exception:
                server_name = ""
        return {
            "backend": backend,
            "server_name": server_name,
            "hotkey_info": hotkey_info,
            "hotkey": self.get_hotkey() if self.get_hotkey else "",
            "ai_hotkey": self.get_ai_hotkey() if self.get_ai_hotkey else "",
            "hotkey_extra": self._safe_call("get_extra_hotkey_fn", ""),
            "history": history,
            "stats": self._stats(),
            "candidates": candidates,
            "dismissed": dismissed,
            "update": update,
            "version": __version__,
            "server_outdated": self._safe_call("get_outdated_servers_fn", []),
        }

    def get_dict_payload(self) -> Dict[str, Any]:
        data = _read_json(self.dict_path)
        fillers = data.get("fillers")
        return {
            "replacements": data.get("replacements") or {},
            "snippets": data.get("snippets") or {},
            "hotwords": [str(w) for w in (data.get("hotwords") or [])],
            "fillers": [str(w) for w in fillers] if isinstance(fillers, list) else list(DEFAULT_FILLERS),
            "builtin_off": [str(w) for w in (data.get("builtin_off") or [])],
            "default_fillers": list(DEFAULT_FILLERS),
            "builtin_terms": dict(BUILTIN_TERMS),
        }

    def save_dict_payload(self, body: dict) -> None:
        data = _read_json(self.dict_path)
        reps = body.get("replacements")
        if isinstance(reps, dict):
            data["replacements"] = {str(k).strip(): str(v) for k, v in reps.items() if str(k).strip()}
        snips = body.get("snippets")
        if isinstance(snips, dict):
            data["snippets"] = {str(k).strip(): str(v) for k, v in snips.items() if str(k).strip() and str(v) != ""}
        if isinstance(body.get("hotwords"), list):
            data["hotwords"] = [str(w).strip() for w in body["hotwords"] if str(w).strip()]
        if isinstance(body.get("fillers"), list):
            data["fillers"] = [str(w).strip() for w in body["fillers"] if str(w).strip()]
        if isinstance(body.get("builtin_off"), list):
            data["builtin_off"] = [str(w).strip() for w in body["builtin_off"] if str(w).strip()]
        _write_json(self.dict_path, data)
        if self.on_dict_saved:
            self.on_dict_saved()

    def _safe_call(self, attr: str, default: Any) -> Any:
        fn = getattr(self, attr, None)
        if not fn:
            return default
        try:
            return fn()
        except Exception:
            logger.exception("%s に失敗しました", attr)
            return default

    def get_dict_sync_payload(self) -> Dict[str, Any]:
        from shared.dict_sync import find_sync_folders

        st = self.get_dict_sync_status() if self.get_dict_sync_status else {}
        return {"status": st, "folders": find_sync_folders()}

    def get_settings_payload(self) -> Dict[str, Any]:
        cfg = ClientConfig.load(self.config_path)
        mics = [DEFAULT_MIC_LABEL] + (self.list_microphones() if self.list_microphones else [])
        current_mic = cfg.microphone if isinstance(cfg.microphone, str) and cfg.microphone else DEFAULT_MIC_LABEL
        if current_mic not in mics:
            mics.append(current_mic)
        return {
            "hotkey": self.get_hotkey() if self.get_hotkey else cfg.hotkey,
            "ai_hotkey": self.get_ai_hotkey() if self.get_ai_hotkey else cfg.ai_hotkey,
            "hotkey_extra": self._safe_call("get_extra_hotkey_fn", "") or getattr(cfg, "hotkey_extra", ""),
            "hotkey_mode": "hold" if str(cfg.hotkey_mode).lower() == "hold" else "toggle",
            "auto_stop_on_silence": bool(cfg.auto_stop_on_silence),
            "silence_stop_sec": float(cfg.silence_stop_sec),
            "cancel_key": cfg.cancel_key,
            "max_duration_sec": int(cfg.max_duration_sec),
            "microphone": current_mic,
            "microphones": mics,
            "startup_enabled": bool(self.is_startup_enabled()) if self.is_startup_enabled else False,
            "remove_fillers": bool(cfg.remove_fillers),
            "normalize_text": bool(cfg.normalize_text),
            "voice_commands": bool(cfg.voice_commands),
            "undo_voice_command": bool(cfg.undo_voice_command),
            "keep_mic_open": bool(cfg.keep_mic_open),
            "overlay_pinned": bool(cfg.overlay_position),
            "recording_indicator": cfg.recording_indicator,
            "mute_other_audio": bool(cfg.mute_other_audio),
            "overlay_place": cfg.overlay_place,
            "live_segmentation": bool(cfg.live_segmentation),
            "auto_refine_self_correction": bool(cfg.auto_refine_self_correction),
            "strip_trailing_period": bool(cfg.strip_trailing_period),
            "app_specific_rules": bool(cfg.app_specific_rules),
            "learn_from_edits": bool(cfg.learn_from_edits),
            "auto_learn_after_count": int(cfg.auto_learn_after_count),
            "restore_clipboard": bool(cfg.restore_clipboard),
            "beep_enabled": bool(cfg.beep_enabled),
            "show_overlay": bool(cfg.show_overlay),
            "server_urls": list(cfg.server_urls),
            "server_names": dict(getattr(cfg, "server_names", {}) or {}),
            "preferred_server": getattr(cfg, "preferred_server", "") or "",
            "recognition_place": (cfg.recognition_place or ("server" if cfg.server_urls else "local")),
            "local_fallback": bool(cfg.local_fallback),
            "server_info": self._safe_call("get_server_info_fn", {}),
            "dict_share": cfg.dict_share if cfg.dict_share in ("server", "folder") else "",
            "dict_share_folder": cfg.dict_share_folder,
            "dict_share_name": cfg.dict_share_name,
            "llm_base_url": cfg.llm_base_url,
            "llm_model": cfg.llm_model,
        }

    def save_settings_payload(self, body: dict) -> None:
        from client.input_hook import parse_binding

        cancel = str(body.get("cancel_key", "esc")).strip() or "esc"
        parse_binding(cancel)
        max_sec = float(body.get("max_duration_sec", 120))
        if not 5 <= max_sec <= 3600:
            raise ValueError("最長の録音は 5〜3600 秒にしてください")
        silence_sec = float(body.get("silence_stop_sec", 1.8))
        if not 0.3 <= silence_sec <= 30.0:
            raise ValueError("無音自動確定は 0.3〜30 秒にしてください")
        auto_cnt = int(body.get("auto_learn_after_count", 2))
        if not 0 <= auto_cnt <= 20:
            raise ValueError("自動辞書登録の回数は 0〜20 の範囲にしてください")

        raw_urls = body.get("server_urls", [])
        if isinstance(raw_urls, str):
            urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
        else:
            urls = [str(u).strip() for u in raw_urls if str(u).strip()]
        for u in urls:
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"サーバーの書き方が正しくありません: {u}")

        llm_url = str(body.get("llm_base_url", "")).strip()
        if llm_url and not llm_url.startswith(("http://", "https://")):
            raise ValueError(f"LLM URL の書き方が正しくありません: {llm_url}")

        cfg = ClientConfig.load(self.config_path)
        cfg.hotkey_mode = "hold" if str(body.get("hotkey_mode", "toggle")).lower() == "hold" else "toggle"
        cfg.auto_stop_on_silence = bool(body.get("auto_stop_on_silence", False))
        cfg.silence_stop_sec = silence_sec
        cfg.cancel_key = cancel
        cfg.max_duration_sec = max_sec
        mic = str(body.get("microphone", DEFAULT_MIC_LABEL))
        cfg.microphone = None if mic == DEFAULT_MIC_LABEL else mic
        cfg.remove_fillers = bool(body.get("remove_fillers", True))
        cfg.normalize_text = bool(body.get("normalize_text", True))
        cfg.voice_commands = bool(body.get("voice_commands", True))
        cfg.undo_voice_command = bool(body.get("undo_voice_command", True))
        cfg.keep_mic_open = bool(body.get("keep_mic_open", True))
        cfg.mute_other_audio = bool(body.get("mute_other_audio", True))
        cfg.recording_indicator = "text" if body.get("recording_indicator") == "text" else "pill"
        cfg.overlay_place = "caret" if body.get("overlay_place") == "caret" else "bottom"
        if not body.get("overlay_pinned", True):
            cfg.overlay_position = None  # 固定をやめてカーソルの近くに戻す
        cfg.live_segmentation = bool(body.get("live_segmentation", True))
        cfg.auto_refine_self_correction = bool(body.get("auto_refine_self_correction", True))
        cfg.strip_trailing_period = bool(body.get("strip_trailing_period", True))
        cfg.app_specific_rules = bool(body.get("app_specific_rules", True))
        cfg.learn_from_edits = bool(body.get("learn_from_edits", True))
        cfg.auto_learn_after_count = auto_cnt
        cfg.restore_clipboard = bool(body.get("restore_clipboard", True))
        cfg.beep_enabled = bool(body.get("beep_enabled", True))
        cfg.show_overlay = bool(body.get("show_overlay", True))
        cfg.server_urls = urls
        names = body.get("server_names")
        if isinstance(names, dict):
            old = dict(getattr(cfg, "server_names", {}) or {})
            old.update({str(k): str(v)[:64] for k, v in names.items() if str(k) in urls and str(v).strip()})
            cfg.server_names = {u: old[u] for u in urls if u in old}
        if "preferred_server" in body:
            want = str(body.get("preferred_server") or "").strip()
            cfg.preferred_server = want if want in urls else ""
        elif getattr(cfg, "preferred_server", "") not in urls:
            cfg.preferred_server = ""
        cfg.recognition_place = "server" if body.get("recognition_place") == "server" else "local"
        if cfg.recognition_place == "server" and not urls:
            raise ValueError("使うパソコンが選ばれていません。「サーバーをさがす」を押して、見つかったパソコンにチェックを入れてください")
        cfg.local_fallback = bool(body.get("local_fallback", True))
        if "dict_share" in body:
            from shared.dict_sync import NAME_RE

            share = str(body.get("dict_share") or "")
            share = share if share in ("server", "folder") else ""
            folder = str(body.get("dict_share_folder") or "").strip().strip('"')
            name = str(body.get("dict_share_name") or "shared").strip() or "shared"
            if not NAME_RE.match(name):
                raise ValueError("辞書の名前は、英数字と - _ で 32 文字までにしてください")
            if share == "server" and not urls:
                raise ValueError("辞書を預けるパソコンがありません。「声を文字にする場所」で、別のパソコンを見つけてください")
            if share == "folder":
                p = Path(folder)
                if not folder or not p.is_absolute():
                    raise ValueError("辞書を置くフォルダを選んでください")
                if not p.is_dir():
                    if not p.parent.is_dir():
                        raise ValueError(f"フォルダが見つかりません: {folder}")
                    p.mkdir()
            cfg.dict_share, cfg.dict_share_folder, cfg.dict_share_name = share, folder, name
        cfg.llm_base_url = llm_url
        cfg.llm_model = str(body.get("llm_model", "")).strip()
        cfg.save(self.config_path)

        if self.set_startup_enabled is not None and "startup_enabled" in body:
            self.set_startup_enabled(bool(body["startup_enabled"]))
        if self.on_config_saved:
            self.on_config_saved()
