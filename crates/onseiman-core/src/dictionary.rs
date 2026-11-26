//! ユーザー辞書（優先語・置き換え・定型文・言いよどみ・標準の変換）。
//! Python 版 `shared/dictionary.py` および文末の「。」削り（`client/app_context.py`）と同じ決まり。

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::fillers::{FillerRemover, DEFAULT_FILLERS};
use crate::kana::{is_hiragana, is_kana_only, replace_katakana_word, to_katakana};
use crate::normalize::TextNormalizer;

pub const MAX_HOTWORDS: usize = 40;

#[derive(Debug, Clone)]
enum ReplaceRule {
    Exact {
        src: String,
        dst: String,
        len: usize,
    },
    KatakanaWord {
        kata: String,
        dst: String,
        len: usize,
    },
}

impl ReplaceRule {
    fn len(&self) -> usize {
        match self {
            Self::Exact { len, .. } | Self::KatakanaWord { len, .. } => *len,
        }
    }
}

#[derive(Debug, Clone)]
pub struct CustomDictionary {
    pub path: Option<PathBuf>,
    hotwords: Vec<String>,
    replacements: Vec<(String, String)>,
    snippets: Vec<(String, String)>,
    rules: Vec<ReplaceRule>,
    filler_remover: FillerRemover,
    normalizer: TextNormalizer,
}

impl Default for CustomDictionary {
    fn default() -> Self {
        Self::from_value(&Value::Object(Map::new()))
    }
}

impl CustomDictionary {
    pub fn load(path: &Path) -> Self {
        let mut dict = if let Ok(raw) = fs::read_to_string(path) {
            let trimmed = raw.strip_prefix('\u{feff}').unwrap_or(&raw);
            let val: Value =
                serde_json::from_str(trimmed).unwrap_or_else(|_| Value::Object(Map::new()));
            Self::from_value(&val)
        } else {
            Self::default()
        };
        dict.path = Some(path.to_path_buf());
        dict
    }

    pub fn from_value(data: &Value) -> Self {
        let obj = data.as_object();
        let hotwords: Vec<String> = obj
            .and_then(|m| m.get("hotwords"))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| match v {
                        Value::String(s) => {
                            let t = s.trim();
                            (!t.is_empty()).then(|| t.to_string())
                        }
                        _ => None,
                    })
                    .collect()
            })
            .unwrap_or_default();

        let mut replacements: Vec<(String, String)> = obj
            .and_then(|m| m.get("replacements"))
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .filter(|(k, _)| !k.is_empty())
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
                    .collect()
            })
            .unwrap_or_default();
        replacements.sort_by_key(|(k, _)| std::cmp::Reverse(k.chars().count()));

        let snippets: Vec<(String, String)> = obj
            .and_then(|m| m.get("snippets"))
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .filter_map(|(k, v)| {
                        let tk = k.trim();
                        let tv = v.as_str().unwrap_or("");
                        (!tk.is_empty() && !tv.is_empty()).then(|| (tk.to_string(), tv.to_string()))
                    })
                    .collect()
            })
            .unwrap_or_default();

        let filler_remover = match obj.and_then(|m| m.get("fillers")).and_then(Value::as_array) {
            Some(arr) => FillerRemover::new(arr.iter().filter_map(Value::as_str)),
            None => FillerRemover::new(DEFAULT_FILLERS.iter().copied()),
        };

        let disabled: Vec<String> = obj
            .and_then(|m| m.get("builtin_off"))
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(String::from)
                    .collect()
            })
            .unwrap_or_default();
        let normalizer = TextNormalizer::new(disabled);

        let rules = compile_rules(&replacements);
        Self {
            path: None,
            hotwords,
            replacements,
            snippets,
            rules,
            filler_remover,
            normalizer,
        }
    }

    pub fn hotwords(&self) -> &[String] {
        &self.hotwords
    }

    pub fn replacements(&self) -> &[(String, String)] {
        &self.replacements
    }

    /// 音声認識へ渡す優先語（新しく登録したものから上限 `MAX_HOTWORDS` 件）。
    /// ひらがなだけ・1 文字の言葉は枠を使わない。
    pub fn active_hotwords(&self) -> Vec<String> {
        let mut words: Vec<String> = Vec::new();
        let iter = self
            .hotwords
            .iter()
            .map(String::as_str)
            .chain(self.replacements.iter().map(|(_, dst)| dst.as_str()));
        for w in iter {
            let w = w.trim();
            if w.chars().count() < 2 || w.chars().all(|c| ('\u{3041}'..='\u{309F}').contains(&c)) {
                continue;
            }
            if !words.iter().any(|x| x == w) {
                words.push(w.to_string());
            }
        }
        let start = words.len().saturating_sub(MAX_HOTWORDS);
        words.split_off(start)
    }

    pub fn hotwords_csv(&self) -> String {
        self.active_hotwords().join(",")
    }

    /// 優先語の読み（`{正しい言葉: [読み, ...]}`）。speech.cpp の keywords へ渡す。
    pub fn readings(&self) -> HashMap<String, Vec<String>> {
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for (src, dst) in &self.replacements {
            let s = src.trim();
            let d = dst.trim();
            if !s.is_empty() && !d.is_empty() && is_kana_only(s) {
                let list = map.entry(d.to_string()).or_default();
                if list.len() < 5 && !list.iter().any(|x| x == s) {
                    list.push(s.to_string());
                }
            }
        }
        map
    }

    pub fn remove_fillers(&self, text: &str) -> String {
        self.filler_remover.remove(text)
    }

    pub fn normalize(&self, text: &str) -> String {
        self.normalizer.normalize(text)
    }

    pub fn apply_replacements(&self, text: &str) -> String {
        if text.is_empty() {
            return String::new();
        }
        let mut result = text.to_string();
        for rule in &self.rules {
            match rule {
                ReplaceRule::Exact { src, dst, .. } => {
                    result = result.replace(src, dst);
                }
                ReplaceRule::KatakanaWord { kata, dst, .. } => {
                    result = replace_katakana_word(&result, kata, dst);
                }
            }
        }
        result
    }

    /// 発話全体が定型文の合言葉と一致したら展開して `(展開後, true)` を返す。
    pub fn apply_snippets(&self, text: &str) -> (String, bool) {
        if text.is_empty() || self.snippets.is_empty() {
            return (text.to_string(), false);
        }
        let trim_set = |c: char| {
            matches!(
                c,
                '。' | '、'
                    | '.'
                    | ','
                    | '！'
                    | '？'
                    | '!'
                    | '?'
                    | '「'
                    | '」'
                    | '『'
                    | '』'
                    | ' '
                    | '\t'
                    | '\n'
                    | '\r'
            )
        };
        let key = text.trim_matches(trim_set);
        if key.is_empty() {
            return (text.to_string(), false);
        }
        let key_lower = key.to_lowercase();
        let key_kata = is_kana_only(key).then(|| to_katakana(key));
        for (trig, body) in &self.snippets {
            let t = trig.trim_matches(trim_set);
            if t.is_empty() {
                continue;
            }
            if t == key || t.to_lowercase() == key_lower {
                return (body.clone(), true);
            }
            if let Some(ref kk) = key_kata {
                if is_kana_only(t) && to_katakana(t) == *kk {
                    return (body.clone(), true);
                }
            }
        }
        (text.to_string(), false)
    }
}

fn compile_rules(replacements: &[(String, String)]) -> Vec<ReplaceRule> {
    let mut rules = Vec::new();
    for (src, dst) in replacements {
        let has_hira = src.chars().any(is_hiragana);
        let char_len = src.chars().count();
        if is_kana_only(src) && has_hira {
            let kata = to_katakana(src);
            let klen = kata.chars().count();
            rules.push(ReplaceRule::KatakanaWord {
                kata,
                dst: dst.clone(),
                len: klen,
            });
            if char_len >= 3 {
                rules.push(ReplaceRule::Exact {
                    src: src.clone(),
                    dst: dst.clone(),
                    len: char_len,
                });
            }
        } else {
            rules.push(ReplaceRule::Exact {
                src: src.clone(),
                dst: dst.clone(),
                len: char_len,
            });
        }
    }
    rules.sort_by_key(|r| std::cmp::Reverse(r.len()));
    rules
}

/// 各行の末尾の単独の「。」だけを落とす（「。。」「？」「！」は残す）。
pub fn strip_trailing_period(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    text.split('\n')
        .map(|line| {
            let trimmed_end = line.trim_end();
            let ws_suffix = &line[trimmed_end.len()..];
            if let Some(before) = trimmed_end.strip_suffix('。') {
                if !before.ends_with('。') {
                    return format!("{before}{ws_suffix}");
                }
            }
            line.to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}
