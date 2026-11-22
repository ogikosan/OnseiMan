"""録音している間、ほかのアプリの音を消す（Typeless の「音声入力中にミュート」と同じ）。

- 自分（OnseiMan）と Windows のシステム音は消さない（開始・終了の音は鳴る）。
- もともと消音になっていたアプリには触らない。戻すときは、こちらで消したものだけ戻す。
- 消したアプリは logs/muted_sessions.json に書いておき、OnseiMan が途中で落ちても次の起動時に戻す。
- 音の操作は少し時間がかかることがあるので、専用のスレッドで順番に行う（キー操作を待たせない）。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("onseiman.audio_mute")


class OtherAudioMuter:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._muted: Dict[int, str] = {}  # pid -> アプリ名
        self._q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="onseiman-audio-mute", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 外から
    def mute_others(self) -> None:
        self._q.put("mute")

    def restore(self) -> None:
        self._q.put("restore")

    def restore_leftover(self) -> None:
        """前回落ちたときに消したままになっている音を戻す（起動時に呼ぶ）。"""
        self._q.put("leftover")

    def stop(self) -> None:
        self._q.put("restore")
        self._q.put(None)

    # ------------------------------------------------------------ 中身
    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        while True:
            op = self._q.get()
            if op is None:
                return
            try:
                if op == "mute":
                    self._do_mute()
                elif op == "restore":
                    self._do_restore(set(self._muted))
                elif op == "leftover":
                    self._do_leftover()
            except Exception as exc:
                logger.warning("ほかのアプリの音の操作に失敗しました (%s): %s", op, exc)

    @staticmethod
    def _sessions():
        from pycaw.pycaw import AudioUtilities

        return AudioUtilities.GetAllSessions()

    def _do_mute(self) -> None:
        own = os.getpid()
        for s in self._sessions():
            pid = int(s.ProcessId or 0)
            if pid in (0, own) or pid in self._muted:
                continue
            vol = s.SimpleAudioVolume
            if vol.GetMute():
                continue  # もともと消音（戻すときも触らない）
            vol.SetMute(1, None)
            self._muted[pid] = s.Process.name() if s.Process else str(pid)
        self._save()
        if self._muted:
            logger.info("録音中のため音を消しました: %s", ", ".join(sorted(set(self._muted.values()))))

    def _do_restore(self, pids: set) -> None:
        if not pids:
            return
        for s in self._sessions():
            pid = int(s.ProcessId or 0)
            if pid in pids:
                try:
                    s.SimpleAudioVolume.SetMute(0, None)
                except Exception:
                    pass
        for pid in pids:
            self._muted.pop(pid, None)
        self._save()

    def _do_leftover(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            pids = {int(p) for p in data.get("pids", [])}
        except Exception:
            return
        if pids:
            logger.info("前回消したままの音を戻します: %d 個", len(pids))
            self._do_restore(pids)
        try:
            self.state_path.unlink()
        except Exception:
            pass

    def _save(self) -> None:
        try:
            if self._muted:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({"pids": sorted(self._muted)}), encoding="utf-8")
            elif self.state_path.exists():
                self.state_path.unlink()
        except Exception:
            pass
