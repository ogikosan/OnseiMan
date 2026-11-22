from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List


@dataclass
class HistoryEntry:
    timestamp: str
    backend: str
    elapsed_ms: float
    duration_sec: float
    raw_text: str
    text: str
    audio: str = ""     # 録音のファイル名（logs/audio/ の中）。再試行に使う
    failed: str = ""    # 失敗したときの理由（成功なら空）


class AppLogger:
    """Manages rotating application log and searchable transcription history."""

    def __init__(self, log_dir: Path, max_memory_entries: int = 200) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / "onseiman.log"
        self.history_txt = self.log_dir / "history.txt"
        self.history_jsonl = self.log_dir / "history.jsonl"
        self.max_memory_entries = max_memory_entries
        self._entries: List[HistoryEntry] = []

        self._setup_logging()
        self._load_recent_history()

    def _setup_logging(self) -> None:
        root = logging.getLogger("onseiman")
        root.setLevel(logging.INFO)
        if root.handlers:
            return

        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

        file_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    def _load_recent_history(self) -> None:
        if not self.history_jsonl.exists():
            return
        try:
            lines = self.history_jsonl.read_text(encoding="utf-8").splitlines()
            for line in lines[-self.max_memory_entries :]:
                if not line.strip():
                    continue
                data = json.loads(line)
                known = HistoryEntry.__dataclass_fields__
                self._entries.append(HistoryEntry(**{k: v for k, v in data.items() if k in known}))
        except Exception:
            pass

    def record_transcription(
        self,
        *,
        backend: str,
        elapsed_ms: float,
        duration_sec: float,
        raw_text: str,
        text: str,
        audio: str = "",
        failed: str = "",
    ) -> HistoryEntry:
        entry = HistoryEntry(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            backend=backend,
            elapsed_ms=round(elapsed_ms, 1),
            duration_sec=round(duration_sec, 2),
            raw_text=raw_text,
            text=text,
            audio=audio,
            failed=failed,
        )
        self._entries.append(entry)
        if len(self._entries) > self.max_memory_entries:
            self._entries = self._entries[-self.max_memory_entries :]

        try:
            with self.history_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            with self.history_txt.open("a", encoding="utf-8") as f:
                f.write(
                    f"[{entry.timestamp}] ({entry.backend}, {entry.elapsed_ms:.0f}ms, {entry.duration_sec:.1f}s)\n"
                    f"{entry.text or '（失敗: ' + entry.failed + '）'}\n\n"
                )
        except OSError:
            pass

        return entry

    def record_failure(self, *, reason: str, duration_sec: float, audio: str = "", backend: str = "") -> HistoryEntry:
        """認識できなかった録音も履歴に残す（あとで「再試行」できるように）。"""
        return self.record_transcription(
            backend=backend or "-", elapsed_ms=0.0, duration_sec=duration_sec, raw_text="", text="",
            audio=audio, failed=reason,
        )

    def find(self, timestamp: str, audio: str = "") -> HistoryEntry | None:
        for e in reversed(self._entries):
            if e.timestamp == timestamp and (not audio or e.audio == audio):
                return e
        return None

    def update_entry(self, entry: HistoryEntry, **fields) -> None:
        """再試行で認識できたとき、その履歴を書き換える（history.jsonl も書き直す）。"""
        for k, v in fields.items():
            setattr(entry, k, v)
        try:
            known = HistoryEntry.__dataclass_fields__
            lines = self.history_jsonl.read_text(encoding="utf-8").splitlines() if self.history_jsonl.exists() else []
            out = []
            for line in lines:
                try:
                    d = json.loads(line)
                except Exception:
                    out.append(line)
                    continue
                if d.get("timestamp") == entry.timestamp and d.get("audio", "") == entry.audio:
                    d = {k: v for k, v in asdict(entry).items() if k in known}
                out.append(json.dumps(d, ensure_ascii=False))
            tmp = self.history_jsonl.with_suffix(".tmp")
            tmp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
            tmp.replace(self.history_jsonl)
        except OSError:
            pass

    # キーボードで日本語を打つ速さの目安（1 分あたりの文字数）。「節約した時間」の計算に使う
    TYPING_CHARS_PER_MIN = 60.0

    def stats(self) -> dict:
        """これまでの使用量（Typeless の「インサイト」に相当）。history.jsonl 全体から数える。"""
        chars = count = 0
        speak_sec = 0.0
        days: set = set()
        try:
            lines = self.history_jsonl.read_text(encoding="utf-8").splitlines() if self.history_jsonl.exists() else []
        except OSError:
            lines = []
        for line in lines:
            try:
                d = json.loads(line)
            except Exception:
                continue
            text = d.get("text") or ""
            if not text:
                continue
            count += 1
            chars += len(text.replace("\n", ""))
            speak_sec += float(d.get("duration_sec") or 0.0)
            days.add(str(d.get("timestamp", ""))[:10])
        typing_sec = chars / self.TYPING_CHARS_PER_MIN * 60.0
        saved_sec = max(0.0, typing_sec - speak_sec)
        # 今日（なければ昨日）から続いている日数
        from datetime import date, timedelta

        streak, day = 0, date.today()
        if day.isoformat() not in days:
            day -= timedelta(days=1)
        while day.isoformat() in days:
            streak += 1
            day -= timedelta(days=1)
        return {
            "chars": chars,
            "count": count,
            "speak_min": round(speak_sec / 60.0, 1),
            "saved_min": round(saved_sec / 60.0),
            "chars_per_min": round(chars / (speak_sec / 60.0)) if speak_sec > 1 else 0,
            "active_days": len(days),
            "streak": streak,
        }

    def get_history(self) -> List[HistoryEntry]:
        return list(reversed(self._entries))

    @property
    def last_text(self) -> str:
        for e in reversed(self._entries):
            if e.text:
                return e.text
        return ""

    def close(self) -> None:
        root = logging.getLogger("onseiman")
        for handler in list(root.handlers):
            handler.close()
            root.removeHandler(handler)
