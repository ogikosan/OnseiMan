//! Python 版（shared/）と同じ答えになるかを確かめる。
//! 正解は `tests/golden/make_golden.py` が Python 版を動かして書き出したもの（tests/golden/*.json）。

use onseiman_core::acronyms::fix_acronyms;
use onseiman_core::dict_format::clean_data;
use onseiman_core::segmenter::{join_texts, split_on_pauses, SegmentOptions};
use serde_json::Value;

fn load(name: &str) -> Value {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests")
        .join("golden")
        .join(name);
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

/// 整数だけで作る雑音（make_golden.py の noise と同じ）
fn noise(n: usize, seed: u64, amp_milli: i64) -> Vec<f32> {
    let mut x = seed & 0x7FFF_FFFF;
    (0..n)
        .map(|_| {
            x = (x * 1_103_515_245 + 12_345) & 0x7FFF_FFFF;
            (((x >> 8) % 2001) as i64 - 1000) as f32 * amp_milli as f32 / 1_000_000f32
        })
        .collect()
}

#[test]
fn segmenter_cuts_at_the_same_places_as_python() {
    for case in load("segmenter.json").as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let sr = case["sample_rate"].as_u64().unwrap();
        let mut samples = Vec::new();
        for step in case["plan"].as_array().unwrap() {
            let (ms, amp, seed) = (
                step[0].as_u64().unwrap(),
                step[1].as_i64().unwrap(),
                step[2].as_u64().unwrap(),
            );
            samples.extend(noise((ms * sr / 1000) as usize, seed, amp));
        }
        let o = &case["options"];
        let d = SegmentOptions::default();
        let get = |k: &str, default: f64| o.get(k).and_then(Value::as_f64).unwrap_or(default);
        let options = SegmentOptions {
            rms_threshold: get("rms_threshold", d.rms_threshold),
            pause_sec: get("pause_sec", d.pause_sec),
            min_segment_sec: get("min_segment_sec", d.min_segment_sec),
            max_segment_sec: get("max_segment_sec", d.max_segment_sec),
            search_back_sec: get("search_back_sec", d.search_back_sec),
            drop_below_rms: None,
        };
        let got: Vec<u64> = split_on_pauses(&samples, sr as u32, options)
            .iter()
            .map(|p| p.len() as u64)
            .collect();
        let want: Vec<u64> = case["lengths"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_u64().unwrap())
            .collect();
        assert_eq!(got, want, "{name}");
    }
}

#[test]
fn text_helpers_match_python() {
    let g = load("text.json");
    for pair in g["acronyms"].as_array().unwrap() {
        assert_eq!(
            fix_acronyms(pair[0].as_str().unwrap()),
            pair[1].as_str().unwrap()
        );
    }
    for pair in g["join_texts"].as_array().unwrap() {
        let parts: Vec<&str> = pair[0]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(join_texts(&parts), pair[1].as_str().unwrap());
    }
    for pair in g["clean_data"].as_array().unwrap() {
        assert_eq!(
            Value::Object(clean_data(&pair[0], None)),
            pair[1],
            "{}",
            pair[0]
        );
    }
    let fillers = vec!["えーと".to_string(), "あのー".to_string()];
    for pair in g["clean_data_with_default_fillers"].as_array().unwrap() {
        assert_eq!(
            Value::Object(clean_data(&pair[0], Some(&fillers))),
            pair[1],
            "{}",
            pair[0]
        );
    }
}
