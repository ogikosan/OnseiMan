"""Windows UI Automation を使ってペースト後の入力欄修正を静かに感知する。"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, List, Optional, Tuple

from shared.correction_learner import extract_corrections

logger = logging.getLogger("onseiman.uia")

user32 = ctypes.WinDLL("user32", use_last_error=True)


def _clean_uia_text(text: str) -> str:
    """Chrome/Electron の contenteditable が混ぜるゼロ幅文字や CRLF を正規化する。"""
    return text.replace("\u200b", "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")


def _collect_descendant_text(ctrl, max_nodes: int = 40) -> str:
    """Discord Web / Slate.js など contenteditable 内の子 TextControl から本文を結合する。"""
    parts: List[str] = []
    visited = 0

    def walk(node, depth: int) -> None:
        nonlocal visited
        if depth > 5 or visited >= max_nodes:
            return
        try:
            child = node.GetFirstChildControl()
        except Exception:
            return
        while child is not None and visited < max_nodes:
            visited += 1
            try:
                ctype = child.ControlTypeName
                name = child.Name or ""
                if ctype == "TextControl" and name:
                    parts.append(name)
                else:
                    walk(child, depth + 1)
                child = child.GetNextSiblingControl()
            except Exception:
                break

    walk(ctrl, 0)
    return "".join(parts)


def read_focused_control_text() -> Tuple[Optional[str], str]:
    """現在フォーカスがあるコントロールの (テキスト, コントロール情報) を返す。読めなければ (None, reason)。"""
    try:
        import uiautomation as auto

        ctrl = auto.GetFocusedControl()
        if ctrl is None:
            return None, "no_focused_control"

        info = f"{getattr(ctrl, 'ControlTypeName', '?')}/{getattr(ctrl, 'ClassName', '')}"

        # 1. TextPattern (メモ帳、リッチエディット、Chromium / Electron の多く)
        try:
            tp = ctrl.GetTextPattern()
            if tp is not None:
                rng = tp.DocumentRange
                if rng is not None:
                    text = rng.GetText(4000)
                    if isinstance(text, str) and text.strip():
                        return _clean_uia_text(text), f"{info}:TextPattern"
        except Exception:
            pass

        # 2. ValuePattern (1行テキストボックス等)
        try:
            vp = ctrl.GetValuePattern()
            if vp is not None:
                val = vp.Value
                if isinstance(val, str) and val.strip():
                    return _clean_uia_text(val), f"{info}:ValuePattern"
        except Exception:
            pass

        # 3. 子孫 TextControl 結合 (Chrome の Discord / Slate.js / T3 Code 等の contenteditable)
        try:
            desc = _collect_descendant_text(ctrl)
            if desc.strip():
                return _clean_uia_text(desc), f"{info}:ChildrenText"
        except Exception:
            pass

        # 4. LegacyIAccessiblePattern
        try:
            lp = ctrl.GetLegacyIAccessiblePattern()
            if lp is not None:
                val = lp.Value
                if isinstance(val, str) and val.strip():
                    return _clean_uia_text(val), f"{info}:LegacyValue"
        except Exception:
            pass

        return "", f"{info}:empty"
    except Exception as exc:
        return None, f"error:{exc}"


class EditCorrectionWatcher:
    """ペースト直後から最大 watch_sec 秒間、ユーザーが直した差分を 1 回だけ拾う。"""

    def __init__(
        self,
        watch_sec: float = 25.0,
        poll_interval_sec: float = 0.6,
        settle_sec: float = 1.6,
    ) -> None:
        self.watch_sec = watch_sec
        self.poll_interval_sec = poll_interval_sec
        self.settle_sec = settle_sec
        self._lock = threading.Lock()
        self._cancel_event: Optional[threading.Event] = None

    def cancel(self) -> None:
        with self._lock:
            if self._cancel_event is not None:
                self._cancel_event.set()
                self._cancel_event = None

    def watch_after_paste(
        self,
        pasted_text: str,
        target_hwnd: int,
        on_corrections: Callable[[List[Tuple[str, str]], str, str], None],
    ) -> None:
        if not pasted_text or len(pasted_text.strip()) < 2:
            return
        self.cancel()
        cancel_ev = threading.Event()
        with self._lock:
            self._cancel_event = cancel_ev

        threading.Thread(
            target=self._watch_worker,
            args=(pasted_text, target_hwnd, on_corrections, cancel_ev),
            name="UIACorrectionWatcher",
            daemon=True,
        ).start()

    def _watch_worker(
        self,
        pasted_text: str,
        target_hwnd: int,
        on_corrections: Callable[[List[Tuple[str, str]], str, str], None],
        cancel_ev: threading.Event,
    ) -> None:
        try:
            import uiautomation as auto
        except ImportError:
            logger.info("UIA 修正監視スキップ: uiautomation 未インストール")
            return

        # 貼り付け反映を少し待つ
        if cancel_ev.wait(0.45):
            return

        pasted_norm = _clean_uia_text(pasted_text).strip()

        try:
            with auto.UIAutomationInitializerInThread(debug=False):
                baseline, info = read_focused_control_text()
                if baseline is None or not baseline.strip():
                    logger.info("UIA 修正監視スキップ（テキスト読み取り不可: %s）", info)
                    return

                # ペースト文そのものか、先頭8文字が入っていれば監視対象とみなす
                probe = pasted_norm[:8]
                if pasted_norm not in baseline and probe not in baseline:
                    logger.info(
                        "UIA 修正監視スキップ（貼り付け文と一致せず: %s, read_len=%d）",
                        info,
                        len(baseline),
                    )
                    return

                logger.info("UIA 修正監視を開始 (%s, %d文字)", info, len(baseline))

                deadline = time.monotonic() + self.watch_sec
                last_text = baseline
                last_change_time = 0.0
                best_edited_text: Optional[str] = None

                while time.monotonic() < deadline and not cancel_ev.is_set():
                    if cancel_ev.wait(self.poll_interval_sec):
                        return

                    fg = user32.GetForegroundWindow()
                    if target_hwnd and fg and int(fg) != int(target_hwnd):
                        break

                    cur, _ = read_focused_control_text()
                    if cur is None:
                        break

                    # チャットで Enter 送信して入力欄が空になった → 直前の編集状態を評価
                    if cur.strip() == "" and best_edited_text is not None:
                        break

                    if cur != last_text:
                        last_text = cur
                        last_change_time = time.monotonic()
                        if cur != baseline and cur.strip():
                            best_edited_text = cur
                    elif best_edited_text is not None and (time.monotonic() - last_change_time) >= self.settle_sec:
                        break

                if cancel_ev.is_set() or not best_edited_text:
                    return

                pairs = extract_corrections(pasted_norm, best_edited_text, baseline_full=baseline)
                if pairs:
                    logger.info("ペースト後のユーザー修正を感知しました: %s", pairs)
                    on_corrections(pairs, pasted_norm, best_edited_text)
                else:
                    logger.info("UIA 修正は検知しましたが辞書候補の条件外でした（追記/助詞など）")
        except Exception as exc:
            logger.info("UIA 修正監視を終了しました: %s", exc)
