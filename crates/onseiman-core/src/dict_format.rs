//! 辞書を預けるときの形の決まり（認識役とアプリの両方で使う）。Python 版の `shared/dict_format.py` と同じ。

use serde_json::{Map, Value};

pub const LIST_KEYS: [&str; 3] = ["hotwords", "fillers", "builtin_off"];
pub const MAP_KEYS: [&str; 2] = ["replacements", "snippets"];
pub const DEFAULT_NAME: &str = "shared";

/// 預ける辞書の大きさの上限
pub const MAX_ITEMS: usize = 5000;
pub const MAX_WORD_CHARS: usize = 200;
pub const MAX_TEXT_CHARS: usize = 10000;

/// Windows の予約デバイス名（CON / AUX / PRN / NUL / COM1-9 / LPT1-9）は拒否する。
fn is_windows_reserved_name(name: &str) -> bool {
    let u = name.to_ascii_uppercase();
    matches!(
        u.as_str(),
        "CON"
            | "PRN"
            | "AUX"
            | "NUL"
            | "COM1"
            | "COM2"
            | "COM3"
            | "COM4"
            | "COM5"
            | "COM6"
            | "COM7"
            | "COM8"
            | "COM9"
            | "LPT1"
            | "LPT2"
            | "LPT3"
            | "LPT4"
            | "LPT5"
            | "LPT6"
            | "LPT7"
            | "LPT8"
            | "LPT9"
    )
}

/// 預ける辞書の名前: 英数字と - _ で 1〜32 文字（Windows 予約デバイス名は不可）
pub fn valid_name(name: &str) -> bool {
    (1..=32).contains(&name.len())
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
        && !is_windows_reserved_name(name)
}

/// 文字でないものが入っていたときの文字への直し方（Python の `str()` に合わせる）
fn as_text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        other => other.to_string(),
    }
}

fn first_chars(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// 合わせる項目だけを、決まった形に整える。
///
/// `default_fillers`: 言いよどみが書かれていないときに使う標準のもの（認識役は None）。
pub fn clean_data(data: &Value, default_fillers: Option<&[String]>) -> Map<String, Value> {
    let empty = Map::new();
    let data = data.as_object().unwrap_or(&empty);
    let mut out = Map::new();
    for k in LIST_KEYS {
        let filled;
        let mut v = data.get(k);
        if matches!(v, None | Some(Value::Null)) && k == "fillers" {
            if let Some(d) = default_fillers {
                // 書いていないときは標準のものを使っている
                filled = Value::Array(d.iter().cloned().map(Value::String).collect());
                v = Some(&filled);
            }
        }
        match v {
            Some(Value::Array(items)) => {
                let mut seen: Vec<String> = Vec::new();
                for w in items {
                    let w = first_chars(as_text(w).trim(), MAX_WORD_CHARS);
                    if !w.is_empty() && !seen.contains(&w) {
                        seen.push(w);
                    }
                }
                seen.truncate(MAX_ITEMS);
                out.insert(
                    k.to_string(),
                    Value::Array(seen.into_iter().map(Value::String).collect()),
                );
            }
            None | Some(Value::Null) => {
                out.insert(k.to_string(), Value::Array(Vec::new()));
            }
            Some(_) => {} // 一覧でないものは入れない（Python 版と同じ）
        }
    }
    for k in MAP_KEYS {
        let mut m = Map::new();
        if let Some(Value::Object(v)) = data.get(k) {
            for (a, b) in v.iter().take(MAX_ITEMS) {
                if a.trim().is_empty() {
                    continue;
                }
                m.insert(
                    first_chars(a.trim(), MAX_WORD_CHARS),
                    Value::String(first_chars(&as_text(b), MAX_TEXT_CHARS)),
                );
            }
        }
        out.insert(k.to_string(), Value::Object(m));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn names() {
        assert!(valid_name("shared") && valid_name("a-b_C9"));
        assert!(
            !valid_name("")
                && !valid_name("../x")
                && !valid_name("あ")
                && !valid_name(&"a".repeat(33))
        );
    }

    #[test]
    fn cleans_lists_and_maps() {
        let d = json!({
            "hotwords": [" くっきり版 ", "", "くっきり版", 5090, "RTX"],
            "replacements": {" クッキリバン ": "くっきり版", "  ": "x"},
            "snippets": "おかしな形",
            "ほかの項目": 1,
        });
        let out = Value::Object(clean_data(&d, None));
        assert_eq!(
            out,
            json!({
                "hotwords": ["くっきり版", "5090", "RTX"],
                "fillers": [],
                "builtin_off": [],
                "replacements": {"クッキリバン": "くっきり版"},
                "snippets": {},
            })
        );
        // 並びは、決まった順（一覧 3 つ → 対応 2 つ）
        let keys: Vec<&String> = out.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            [
                "hotwords",
                "fillers",
                "builtin_off",
                "replacements",
                "snippets"
            ]
        );
    }

    #[test]
    fn default_fillers_and_limits() {
        let fill = vec!["えーと".to_string()];
        let out = clean_data(&json!({}), Some(&fill));
        assert_eq!(out["fillers"], json!(["えーと"]));
        let long = "あ".repeat(300);
        let out = clean_data(&json!({"hotwords": [long], "fillers": "x"}), None);
        assert_eq!(
            out["hotwords"][0].as_str().unwrap().chars().count(),
            MAX_WORD_CHARS
        );
        assert!(!out.contains_key("fillers"));
    }
}
