//! Python 版（shared/）と同じ答えになるかを確かめる。
//! 正解は `tests/golden/make_golden.py` が Python 版を動かして書き出したもの（tests/golden/*.json）。

use onseiman_core::acronyms::fix_acronyms;
use onseiman_core::dict_format::clean_data;
use onseiman_core::dictionary::{strip_trailing_period, CustomDictionary};
use onseiman_core::fillers::FillerRemover;
use onseiman_core::kana::{is_kana_only, kana_variants, to_hiragana, to_katakana};
use onseiman_core::normalize::TextNormalizer;
use onseiman_core::segmenter::{join_texts, split_on_pauses, SegmentOptions};
use onseiman_core::voice_commands::{apply_voice_commands, is_undo_command};
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

#[test]
fn stage2_text_and_dictionary_match_python() {
    let g = load("stage2.json");

    for row in g["kana"].as_array().unwrap() {
        let s = row[0].as_str().unwrap();
        assert_eq!(to_katakana(s), row[1].as_str().unwrap(), "to_katakana({s})");
        assert_eq!(to_hiragana(s), row[2].as_str().unwrap(), "to_hiragana({s})");
        assert_eq!(
            is_kana_only(s),
            row[3].as_bool().unwrap(),
            "is_kana_only({s})"
        );
        let want_v: Vec<String> = row[4]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(kana_variants(s), want_v, "kana_variants({s})");
    }

    let fr = FillerRemover::default();
    for pair in g["fillers"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(fr.remove(inp), pair[1].as_str().unwrap(), "filler({inp})");
    }

    let tn = TextNormalizer::default();
    for pair in g["normalize"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(tn.normalize(inp), pair[1].as_str().unwrap(), "norm({inp})");
    }

    let tn_off = TextNormalizer::new(["エーアイ", "ピーディーエフ"]);
    for pair in g["normalize_disabled"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            tn_off.normalize(inp),
            pair[1].as_str().unwrap(),
            "norm_off({inp})"
        );
    }

    for pair in g["voice_commands"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            apply_voice_commands(inp),
            pair[1].as_str().unwrap(),
            "vc({inp})"
        );
    }

    for pair in g["undo_command"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            is_undo_command(inp),
            pair[1].as_bool().unwrap(),
            "undo({inp})"
        );
    }

    for pair in g["strip_trailing_period"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            strip_trailing_period(inp),
            pair[1].as_str().unwrap(),
            "period({inp})"
        );
    }

    let d = &g["dictionary"];
    let dic = CustomDictionary::from_value(&d["data"]);
    let want_hw: Vec<String> = d["active_hotwords"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert_eq!(dic.active_hotwords(), want_hw);
    for pair in d["replacements"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            dic.apply_replacements(inp),
            pair[1].as_str().unwrap(),
            "rep({inp})"
        );
    }
    for pair in d["snippets"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        let want_txt = pair[1][0].as_str().unwrap();
        let want_ok = pair[1][1].as_bool().unwrap();
        assert_eq!(
            dic.apply_snippets(inp),
            (want_txt.to_string(), want_ok),
            "snip({inp})"
        );
    }
    for pair in d["normalize"].as_array().unwrap() {
        let inp = pair[0].as_str().unwrap();
        assert_eq!(
            dic.normalize(inp),
            pair[1].as_str().unwrap(),
            "dic_norm({inp})"
        );
    }
}
