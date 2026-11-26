//! 認識履歴（`logs/history.jsonl`・`logs/history.txt`）と使用量の集計。
//! Python 版 `client/logger.py` と同じ形で読み書きする。

use std::collections::HashSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::{Duration, Local, NaiveDate};
use serde::{Deserialize, Serialize};

pub const TYPING_CHARS_PER_MIN: f64 = 60.0;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct HistoryEntry {
    pub timestamp: String,
    pub backend: String,
    pub elapsed_ms: f64,
    pub duration_sec: f64,
    #[serde(default)]
    pub raw_text: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub audio: String,
    #[serde(default)]
    pub failed: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct UsageStats {
    pub chars: usize,
    pub count: usize,
    pub speak_min: f64,
    pub saved_min: i64,
    pub chars_per_min: i64,
    pub active_days: usize,
    pub streak: usize,
}

pub struct HistoryStore {
    pub log_dir: PathBuf,
    pub history_txt: PathBuf,
    pub history_jsonl: PathBuf,
    max_memory_entries: usize,
    entries: Vec<HistoryEntry>,
}

impl HistoryStore {
    pub fn open(log_dir: &Path, max_memory_entries: usize) -> Self {
        let _ = fs::create_dir_all(log_dir);
        let mut store = Self {
            log_dir: log_dir.to_path_buf(),
            history_txt: log_dir.join("history.txt"),
            history_jsonl: log_dir.join("history.jsonl"),
            max_memory_entries,
            entries: Vec::new(),
        };
        store.load_recent();
        store
    }

    fn load_recent(&mut self) {
        let Ok(raw) = fs::read_to_string(&self.history_jsonl) else {
            return;
        };
        let lines: Vec<&str> = raw.lines().filter(|l| !l.trim().is_empty()).collect();
        let start = lines.len().saturating_sub(self.max_memory_entries);
        for line in &lines[start..] {
            if let Ok(entry) = serde_json::from_str::<HistoryEntry>(line) {
                self.entries.push(entry);
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn record_transcription(
        &mut self,
        backend: &str,
        elapsed_ms: f64,
        duration_sec: f64,
        raw_text: &str,
        text: &str,
        audio: &str,
        failed: &str,
    ) -> HistoryEntry {
        let entry = HistoryEntry {
            timestamp: Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
            backend: backend.to_string(),
            elapsed_ms: (elapsed_ms * 10.0).round() / 10.0,
            duration_sec: (duration_sec * 100.0).round() / 100.0,
            raw_text: raw_text.to_string(),
            text: text.to_string(),
            audio: audio.to_string(),
            failed: failed.to_string(),
        };
        self.entries.push(entry.clone());
        if self.entries.len() > self.max_memory_entries {
            let drop_n = self.entries.len() - self.max_memory_entries;
            self.entries.drain(0..drop_n);
        }
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.history_jsonl)
        {
            if let Ok(line) = serde_json::to_string(&entry) {
                let _ = writeln!(f, "{line}");
            }
        }
        if let Ok(mut f) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.history_txt)
        {
            let body = if entry.text.is_empty() {
                format!("（失敗: {}）", entry.failed)
            } else {
                entry.text.clone()
            };
            let _ = write!(
                f,
                "[{}] ({}, {:.0}ms, {:.1}s)\n{}\n\n",
                entry.timestamp, entry.backend, entry.elapsed_ms, entry.duration_sec, body
            );
        }
        entry
    }

    pub fn record_failure(
        &mut self,
        reason: &str,
        duration_sec: f64,
        audio: &str,
        backend: &str,
    ) -> HistoryEntry {
        let b = if backend.is_empty() { "-" } else { backend };
        self.record_transcription(b, 0.0, duration_sec, "", "", audio, reason)
    }

    /// 新しい順に履歴を返す。
    pub fn get_history(&self) -> Vec<HistoryEntry> {
        self.entries.iter().rev().cloned().collect()
    }

    pub fn last_text(&self) -> Option<&str> {
        self.entries
            .iter()
            .rev()
            .find(|e| !e.text.is_empty())
            .map(|e| e.text.as_str())
    }

    pub fn stats(&self) -> UsageStats {
        self.stats_as_of(Local::now().date_naive())
    }

    pub fn stats_as_of(&self, today: NaiveDate) -> UsageStats {
        let raw = fs::read_to_string(&self.history_jsonl).unwrap_or_default();
        let mut chars = 0usize;
        let mut count = 0usize;
        let mut speak_sec = 0.0f64;
        let mut days: HashSet<NaiveDate> = HashSet::new();

        for line in raw.lines() {
            let Ok(entry) = serde_json::from_str::<HistoryEntry>(line) else {
                continue;
            };
            if entry.text.is_empty() {
                continue;
            }
            count += 1;
            chars += entry.text.chars().filter(|&c| c != '\n').count();
            speak_sec += entry.duration_sec;
            if let Some(day_str) = entry.timestamp.get(..10) {
                if let Ok(d) = NaiveDate::parse_from_str(day_str, "%Y-%m-%d") {
                    days.insert(d);
                }
            }
        }

        let typing_sec = chars as f64 / TYPING_CHARS_PER_MIN * 60.0;
        let saved_sec = (typing_sec - speak_sec).max(0.0);
        let mut streak = 0usize;
        let mut day = today;
        if !days.contains(&day) {
            day -= Duration::days(1);
        }
        while days.contains(&day) {
            streak += 1;
            day -= Duration::days(1);
        }

        UsageStats {
            chars,
            count,
            speak_min: ((speak_sec / 60.0) * 10.0).round() / 10.0,
            saved_min: (saved_sec / 60.0).round() as i64,
            chars_per_min: if speak_sec > 1.0 {
                (chars as f64 / (speak_sec / 60.0)).round() as i64
            } else {
                0
            },
            active_days: days.len(),
            streak,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_and_stats() {
        let tmp = tempfile::tempdir().unwrap();
        let mut st = HistoryStore::open(tmp.path(), 50);
        st.record_transcription(
            "LAN-cpu",
            250.0,
            10.0,
            "こんにちは",
            "こんにちは",
            "a.wav",
            "",
        );
        st.record_failure("無音", 1.0, "b.wav", "");
        assert_eq!(st.last_text(), Some("こんにちは"));
        assert_eq!(st.get_history().len(), 2);

        let s = st.stats();
        assert_eq!(s.count, 1);
        assert_eq!(s.chars, 5);
        assert_eq!(s.active_days, 1);
        assert_eq!(s.streak, 1);

        // 再読み込みしても復元される
        let st2 = HistoryStore::open(tmp.path(), 50);
        assert_eq!(st2.get_history().len(), 2);
        assert_eq!(st2.last_text(), Some("こんにちは"));
    }
}
