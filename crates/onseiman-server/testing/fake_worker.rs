//! 試験用: speech.cpp の worker のふりをする（JSON Lines、protocol 4 のうち OnseiMan が使う分だけ）。
//!
//! 引数で動きを変える:
//! - （なし）          : 「こんにちは」を返す。返す文の後ろに、受け取った秒数と優先語の数を付ける
//! - `die-first`       : 最初の聞き取りの途中で止まる（起動し直しの確かめ）。印のファイルを 2 番目の引数で受け取る
//! - `refuse-keywords` : 優先語が付いていたら断る（優先語なしでのやり直しの確かめ）
//! - `slow`            : 返事の前に 0.3 秒待つ（同時の数の確かめ）

use std::io::{BufRead, Write};

fn field<'a>(line: &'a str, key: &str) -> Option<&'a str> {
    let at = line.find(&format!("\"{key}\":"))? + key.len() + 3;
    let rest = line[at..].trim_start();
    if let Some(r) = rest.strip_prefix('"') {
        r.split('"').next()
    } else {
        rest.split([',', '}']).next().map(str::trim)
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = args.first().map(String::as_str).unwrap_or("");
    let out = std::io::stdout();
    let say = |s: String| {
        let mut o = out.lock();
        let _ = writeln!(o, "{s}");
        let _ = o.flush();
    };
    say(r#"{"type":"ready","protocol":4}"#.to_string());
    let mut pcm_bytes = 0usize;
    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let id = field(&line, "id").unwrap_or("").to_string();
        match field(&line, "type") {
            Some("chunk") => {
                // base64 の長さから、元のバイト数をおおよそ数える
                pcm_bytes += field(&line, "pcm").map(|p| p.len() * 3 / 4).unwrap_or(0);
            }
            Some("transcribe") => {
                if mode == "die-first" {
                    let mark = std::path::Path::new(&args[1]);
                    if !mark.exists() {
                        std::fs::write(mark, b"x").unwrap();
                        std::process::exit(3);
                    }
                }
                let has_keywords = line.contains("\"keywords\":");
                if mode == "refuse-keywords" && has_keywords {
                    say(format!(
                        r#"{{"type":"error","id":"{id}","error":{{"option":"keywords","message":"bad keyword"}}}}"#
                    ));
                    pcm_bytes = 0;
                    continue;
                }
                if mode == "slow" {
                    std::thread::sleep(std::time::Duration::from_millis(300));
                }
                let rate: usize = field(&line, "sample_rate")
                    .and_then(|v| v.parse().ok())
                    .unwrap_or(16000);
                let secs = pcm_bytes as f64 / 2.0 / rate as f64;
                say(format!(r#"{{"type":"progress","id":"{id}","done":1}}"#));
                say(format!(
                    r#"{{"type":"end","id":"{id}","text":"こんにちは cpu {:.0}秒{}"}}"#,
                    secs,
                    if has_keywords { " 優先語あり" } else { "" }
                ));
                pcm_bytes = 0;
            }
            _ => {}
        }
    }
}
