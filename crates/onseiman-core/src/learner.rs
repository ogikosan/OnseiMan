//! 認識結果の手直しから誤認識→正解の辞書候補を抽出・管理する（`logs/learning_candidates.json`）。

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LearningCandidate {
    pub src: String,
    pub dst: String,
    #[serde(default = "default_one")]
    pub count: u32,
    #[serde(default)]
    pub example_before: String,
    #[serde(default)]
    pub example_after: String,
}

fn default_one() -> u32 {
    1
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct StoreData {
    #[serde(default)]
    candidates: Vec<LearningCandidate>,
    #[serde(default)]
    dismissed: Vec<LearningCandidate>,
}

pub struct CandidateStore {
    path: PathBuf,
    data: StoreData,
}

impl CandidateStore {
    pub fn open(log_dir: &Path) -> Self {
        let _ = fs::create_dir_all(log_dir);
        let path = log_dir.join("learning_candidates.json");
        let data = fs::read_to_string(&path)
            .ok()
            .and_then(|s| {
                serde_json::from_str::<StoreData>(s.strip_prefix('\u{feff}').unwrap_or(&s)).ok()
            })
            .unwrap_or_default();
        Self { path, data }
    }

    fn save(&self) {
        if let Some(p) = self.path.parent() {
            let _ = fs::create_dir_all(p);
        }
        let tmp = self.path.with_extension("json.tmp");
        if let Ok(json) = serde_json::to_string_pretty(&self.data) {
            if fs::write(&tmp, format!("{json}\n")).is_ok() {
                let _ = fs::rename(&tmp, &self.path);
            }
        }
    }

    pub fn candidates(&self) -> &[LearningCandidate] {
        &self.data.candidates
    }

    pub fn dismissed(&self) -> &[LearningCandidate] {
        &self.data.dismissed
    }

    /// 変更前・変更後の文の差分から `(誤認識, 正解)` ペアを抽出して記録する。
    pub fn record_edit(&mut self, before: &str, after: &str) -> usize {
        let pairs = extract_corrections(before, after);
        let mut added = 0;
        for (src, dst) in pairs {
            if self
                .data
                .dismissed
                .iter()
                .any(|c| c.src == src && c.dst == dst)
            {
                continue;
            }
            if let Some(existing) = self
                .data
                .candidates
                .iter_mut()
                .find(|c| c.src == src && c.dst == dst)
            {
                existing.count = existing.count.saturating_add(1);
                existing.example_before = before.chars().take(120).collect();
                existing.example_after = after.chars().take(120).collect();
            } else {
                self.data.candidates.push(LearningCandidate {
                    src,
                    dst,
                    count: 1,
                    example_before: before.chars().take(120).collect(),
                    example_after: after.chars().take(120).collect(),
                });
            }
            added += 1;
        }
        if added > 0 {
            self.data
                .candidates
                .sort_by_key(|a| std::cmp::Reverse(a.count));
            self.save();
        }
        added
    }

    pub fn approve(&mut self, src: &str, dst: &str) {
        self.data
            .candidates
            .retain(|c| !(c.src == src && c.dst == dst));
        self.save();
    }

    pub fn reject(&mut self, src: &str, dst: &str) {
        if let Some(pos) = self
            .data
            .candidates
            .iter()
            .position(|c| c.src == src && c.dst == dst)
        {
            let item = self.data.candidates.remove(pos);
            if !self
                .data
                .dismissed
                .iter()
                .any(|c| c.src == item.src && c.dst == item.dst)
            {
                self.data.dismissed.insert(0, item);
                self.data.dismissed.truncate(100);
            }
            self.save();
        }
    }

    pub fn restore(&mut self, item: LearningCandidate) {
        self.data
            .dismissed
            .retain(|c| !(c.src == item.src && c.dst == item.dst));
        if !self
            .data
            .candidates
            .iter()
            .any(|c| c.src == item.src && c.dst == item.dst)
        {
            self.data.candidates.push(item);
            self.data
                .candidates
                .sort_by_key(|a| std::cmp::Reverse(a.count));
        }
        self.save();
    }
}

/// 共通の前後を除いた差分チャンクから `(src, dst)` を抽出する。
pub fn extract_corrections(before: &str, after: &str) -> Vec<(String, String)> {
    let b: Vec<char> = before.trim().chars().collect();
    let a: Vec<char> = after.trim().chars().collect();
    if b.is_empty() || a.is_empty() || b == a {
        return Vec::new();
    }
    let mut prefix = 0usize;
    while prefix < b.len() && prefix < a.len() && b[prefix] == a[prefix] {
        prefix += 1;
    }
    let mut b_end = b.len();
    let mut a_end = a.len();
    while b_end > prefix && a_end > prefix && b[b_end - 1] == a[a_end - 1] {
        b_end -= 1;
        a_end -= 1;
    }
    let src: String = b[prefix..b_end].iter().collect();
    let dst: String = a[prefix..a_end].iter().collect();
    let src = src.trim().to_string();
    let dst = dst.trim().to_string();
    let slen = src.chars().count();
    let dlen = dst.chars().count();
    if slen == 0 || dlen == 0 || slen > 24 || dlen > 24 || src == dst {
        return Vec::new();
    }
    vec![(src, dst)]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_and_manages_candidates() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = CandidateStore::open(dir.path());
        assert_eq!(store.record_edit("おんせいまんを使う", "OnseiManを使う"), 1);
        assert_eq!(store.candidates().len(), 1);
        assert_eq!(store.candidates()[0].src, "おんせいまん");
        assert_eq!(store.candidates()[0].dst, "OnseiMan");

        store.reject("おんせいまん", "OnseiMan");
        assert!(store.candidates().is_empty());
        assert_eq!(store.dismissed().len(), 1);

        let item = store.dismissed()[0].clone();
        store.restore(item);
        assert_eq!(store.candidates().len(), 1);
    }
}
