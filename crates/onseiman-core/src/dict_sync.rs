//! 辞書の三方向マージ（Python 版 `shared/dict_sync.py` の `merge3` と同じ決まり）。

use std::collections::HashSet;

use serde_json::{Map, Value};

use crate::dict_format::{LIST_KEYS, MAP_KEYS};

fn as_str_list(obj: &Map<String, Value>, key: &str) -> Vec<String> {
    obj.get(key)
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(String::from)
                .collect()
        })
        .unwrap_or_default()
}

fn as_str_map(obj: &Map<String, Value>, key: &str) -> Map<String, Value> {
    obj.get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn merge_list(base: &[String], local: &[String], remote: &[String]) -> Vec<Value> {
    let local_set: HashSet<&str> = local.iter().map(String::as_str).collect();
    let base_set: HashSet<&str> = base.iter().map(String::as_str).collect();
    let removed: HashSet<&str> = base_set.difference(&local_set).copied().collect();

    let mut out: Vec<String> = remote
        .iter()
        .filter(|w| !removed.contains(w.as_str()))
        .cloned()
        .collect();
    for w in local {
        if !base_set.contains(w.as_str()) && !out.contains(w) {
            out.push(w.clone());
        }
    }
    out.into_iter().map(Value::String).collect()
}

fn merge_map(
    base: &Map<String, Value>,
    local: &Map<String, Value>,
    remote: &Map<String, Value>,
) -> Map<String, Value> {
    let mut out = remote.clone();
    for k in base.keys() {
        if !local.contains_key(k) {
            out.remove(k);
        }
    }
    for (k, v) in local {
        if base.get(k) != Some(v) {
            out.insert(k.clone(), v.clone());
        }
    }
    out
}

/// 三方向の合わせ込み。`base` が `None`（初めて・預け先が変わった）なら、消さずに足し合わせる。
pub fn merge3(
    base: Option<&Map<String, Value>>,
    local: &Map<String, Value>,
    remote: &Map<String, Value>,
) -> Map<String, Value> {
    let empty = Map::new();
    let b = base.unwrap_or(&empty);
    let mut out = Map::new();
    for k in LIST_KEYS {
        let base_list = if base.is_some() {
            as_str_list(b, k)
        } else {
            Vec::new()
        };
        let local_list = as_str_list(local, k);
        let remote_list = as_str_list(remote, k);
        out.insert(
            k.to_string(),
            Value::Array(merge_list(&base_list, &local_list, &remote_list)),
        );
    }
    for k in MAP_KEYS {
        let base_map = if base.is_some() {
            as_str_map(b, k)
        } else {
            Map::new()
        };
        let local_map = as_str_map(local, k);
        let remote_map = as_str_map(remote, k);
        out.insert(
            k.to_string(),
            Value::Object(merge_map(&base_map, &local_map, &remote_map)),
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn merge3_preserves_remote_adds_and_local_deletes() {
        let base = json!({
            "hotwords": ["A", "B"],
            "fillers": [],
            "builtin_off": [],
            "replacements": {"x": "1", "y": "2"},
            "snippets": {}
        });
        let local = json!({
            "hotwords": ["A", "C"],
            "fillers": [],
            "builtin_off": [],
            "replacements": {"x": "1-local"},
            "snippets": {}
        });
        let remote = json!({
            "hotwords": ["A", "B", "D"],
            "fillers": [],
            "builtin_off": [],
            "replacements": {"x": "1", "y": "2", "z": "3"},
            "snippets": {}
        });
        let merged = merge3(
            base.as_object(),
            local.as_object().unwrap(),
            remote.as_object().unwrap(),
        );
        // B は local で消したので消える、C と D は両方残る
        assert_eq!(merged["hotwords"], json!(["A", "D", "C"]));
        // y は local で消したので消える、x は local が勝ち、z は remote から入る
        assert_eq!(merged["replacements"], json!({"x": "1-local", "z": "3"}));
    }
}
