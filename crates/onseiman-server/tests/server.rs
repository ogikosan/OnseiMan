//! 認識役を本当に起動して、外から確かめる（聞き取りは、worker のふりをするものに任せる）。
//! Python 版の `tests/test_server_guard.py`・`test_dict_sync.py`（認識役の部分）と同じことを確かめる。

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::Arc;
use std::time::Duration;

use onseiman_core::audio::encode_wav;
use onseiman_server::{app, Config, Limits};
use onseiman_speech::SpeechEngine;
use serde_json::{json, Value};

struct TestServer {
    addr: SocketAddr,
    dir: tempfile::TempDir,
    _rt: tokio::runtime::Runtime,
}

fn start_with(mode: &[&str], limits: Limits) -> TestServer {
    let dir = tempfile::tempdir().unwrap();
    let mut cmd = vec![env!("CARGO_BIN_EXE_fake-speech-worker").to_string()];
    cmd.extend(
        mode.iter()
            .map(|m| m.replace("{dir}", &dir.path().to_string_lossy())),
    );
    let engine = Arc::new(SpeechEngine::new(dir.path(), "cpu", 0).with_command(cmd));
    let cfg = Config {
        dict_dir: dir.path().join("dicts"),
        server_name: "TestMac.local".into(),
        limits,
    };
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let listener = rt
        .block_on(tokio::net::TcpListener::bind("127.0.0.1:0"))
        .unwrap();
    let addr = listener.local_addr().unwrap();
    let router = app(engine, cfg);
    rt.spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    TestServer { addr, dir, _rt: rt }
}

fn start() -> TestServer {
    start_with(&[], Limits::default())
}

impl TestServer {
    fn url(&self, path: &str) -> String {
        format!("http://{}{}", self.addr, path)
    }

    fn finish(resp: Result<ureq::Response, ureq::Error>) -> (u16, Value) {
        let resp = match resp {
            Ok(r) => r,
            Err(ureq::Error::Status(_, r)) => r,
            Err(e) => panic!("つながりません: {e}"),
        };
        let status = resp.status();
        let text = resp.into_string().unwrap_or_default();
        (
            status,
            serde_json::from_str(&text).unwrap_or(Value::String(text)),
        )
    }

    fn get(&self, path: &str) -> (u16, Value) {
        Self::finish(ureq::get(&self.url(path)).call())
    }

    fn put(&self, path: &str, body: &Value) -> (u16, Value) {
        Self::finish(
            ureq::put(&self.url(path))
                .set("Content-Type", "application/json")
                .send_string(&body.to_string()),
        )
    }

    /// アプリと同じ形（multipart）で音声を送る
    fn post_audio(&self, audio: &[u8], fields: &[(&str, &str)]) -> (u16, Value) {
        let b = "onseimanTESTboundary";
        let mut body = Vec::new();
        body.extend_from_slice(
            format!("--{b}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"audio.wav\"\r\nContent-Type: audio/wav\r\n\r\n").as_bytes(),
        );
        body.extend_from_slice(audio);
        for (k, v) in fields {
            body.extend_from_slice(
                format!("\r\n--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}")
                    .as_bytes(),
            );
        }
        body.extend_from_slice(format!("\r\n--{b}--\r\n").as_bytes());
        Self::finish(
            ureq::post(&self.url("/transcribe"))
                .set(
                    "Content-Type",
                    &format!("multipart/form-data; boundary={b}"),
                )
                .send_bytes(&body),
        )
    }

    /// 生の HTTP を送って、返事の番号を返す（Host を変える・長さを言わない送り方をする、の確かめ用）
    fn raw(&self, request: &[u8]) -> u16 {
        let mut s = TcpStream::connect(self.addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let _ = s.write_all(request); // 途中で切られることがある（それも正しい動き）
        let mut resp = Vec::new();
        let _ = s.read_to_end(&mut resp);
        let text = String::from_utf8_lossy(&resp);
        text.split_whitespace()
            .nth(1)
            .and_then(|c| c.parse().ok())
            .unwrap_or(0)
    }
}

fn tone(sec: f64, sr: u32) -> Vec<f32> {
    (0..(sec * sr as f64) as usize)
        .map(|i| (i as f32 * 0.05).sin() * 0.3)
        .collect()
}

fn wav(sec: f64) -> Vec<u8> {
    encode_wav(&tone(sec, 16000), 16000)
}

#[test]
fn health_and_unknown_paths() {
    let s = start();
    let (status, h) = s.get("/health");
    assert_eq!(status, 200);
    assert_eq!(
        (
            h["status"].as_str(),
            h["engine"].as_str(),
            h["model"].as_str()
        ),
        (Some("ok"), Some("haas"), Some("Haas-ja-v1.0"))
    );
    assert_eq!(h["provider"], "speech.cpp-cpu");
    assert_eq!(h["max_segment_sec"], 9.0);
    assert_eq!(h["name"], "TestMac.local");
    assert_eq!(h["id"].as_str().unwrap().len(), 32);
    assert!(!h["version"].as_str().unwrap().is_empty());
    // 説明用のページなどは出さない
    for path in ["/docs", "/redoc", "/openapi.json", "/"] {
        assert_eq!(s.get(path).0, 404, "{path}");
    }
}

#[test]
fn normal_audio_is_recognized() {
    let s = start();
    let (status, body) = s.post_audio(
        &wav(1.0),
        &[
            ("hotwords", ""),
            ("keywords", "{}"),
            ("language", ""),
            ("itn", "true"),
        ],
    );
    assert_eq!(status, 200, "{body}");
    // 略語は大文字に直る（cpu → CPU）
    assert_eq!(body["text"], "こんにちは CPU 1秒");
    assert_eq!(body["provider"], "speech.cpp-cpu");
    assert!(body["elapsed_ms"].is_number());
}

#[test]
fn hotwords_and_readings_reach_the_worker() {
    let s = start();
    let (_, body) = s.post_audio(
        &wav(1.0),
        &[
            ("hotwords", "くっきり版,RTX"),
            ("keywords", r#"{"くっきり版": ["クッキリバン"]}"#),
        ],
    );
    assert_eq!(body["text"], "こんにちは CPU 1秒 優先語あり");
    // 優先語が原因で断られたら、優先語なしでやり直す
    let s = start_with(&["refuse-keywords"], Limits::default());
    let (status, body) = s.post_audio(&wav(1.0), &[("hotwords", "くっきり版")]);
    assert_eq!(
        (status, body["text"].as_str()),
        (200, Some("こんにちは CPU 1秒"))
    );
}

#[test]
fn worker_that_dies_is_restarted_once() {
    let s = start_with(&["die-first", "{dir}/died"], Limits::default());
    let (status, body) = s.post_audio(&wav(1.0), &[]);
    assert_eq!(
        (status, body["text"].as_str()),
        (200, Some("こんにちは CPU 1秒"))
    );
    assert!(s.dir.path().join("died").exists());
}

#[test]
fn only_wav_and_m4a_are_accepted() {
    let s = start();
    for bad in [
        &b"fLaC\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0"[..],
        &b"OggS\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0"[..],
        &wav(1.0)[..30],
    ] {
        let (status, body) = s.post_audio(bad, &[]);
        assert_eq!(status, 400, "{body}");
        assert!(!body.to_string().contains("panicked"));
    }
    assert_eq!(s.post_audio(b"", &[]).0, 400);
    // WAV の頭だけ本物で、中身がこわれているもの
    let mut broken = wav(1.0);
    broken.truncate(60);
    broken[20] = 0x55; // 縮めた形式の番号に変える
    assert_eq!(s.post_audio(&broken, &[]).0, 400);
}

#[test]
fn too_long_and_too_big_are_refused() {
    let s = start_with(
        &[],
        Limits {
            max_audio_sec: 2.0,
            ..Limits::default()
        },
    );
    assert_eq!(s.post_audio(&wav(3.0), &[]).0, 413);
    assert_eq!(s.post_audio(&wav(1.5), &[]).0, 200);
    let s = start_with(
        &[],
        Limits {
            max_audio_bytes: 1000,
            ..Limits::default()
        },
    );
    assert_eq!(s.post_audio(&wav(1.0), &[]).0, 413);
}

#[test]
fn chunked_upload_is_stopped_at_the_limit() {
    // 長さを言わない送り方（chunked）でも、上限を超えたところで受け取りをやめる（ため込み続けない）
    let s = start_with(
        &[],
        Limits {
            max_audio_bytes: 100_000,
            ..Limits::default()
        },
    );
    let mut req = Vec::new();
    req.extend_from_slice(
        b"POST /transcribe HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: multipart/form-data; boundary=b\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
    );
    let head = b"--b\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n";
    req.extend_from_slice(format!("{:x}\r\n", head.len()).as_bytes());
    req.extend_from_slice(head);
    req.extend_from_slice(b"\r\n");
    let block = vec![0u8; 65536];
    for _ in 0..64 {
        // 4MB（上限の 40 倍）
        req.extend_from_slice(format!("{:x}\r\n", block.len()).as_bytes());
        req.extend_from_slice(&block);
        req.extend_from_slice(b"\r\n");
    }
    let tail = b"\r\n--b--\r\n";
    req.extend_from_slice(format!("{:x}\r\n", tail.len()).as_bytes());
    req.extend_from_slice(tail);
    req.extend_from_slice(b"\r\n0\r\n\r\n");
    assert_eq!(s.raw(&req), 413);
    // ふつうの送り方は、そのあとも通る
    assert_eq!(s.post_audio(&wav(1.0), &[]).0, 200);
}

#[test]
fn long_audio_is_split_at_pauses() {
    // 長い音声（iPhone から 40 秒など）は、話の切れ目で区切ってから聞き取ってつなぐ
    let s = start();
    let mut samples = Vec::new();
    for _ in 0..4 {
        samples.extend(tone(6.0, 16000));
        samples.extend(vec![0.0f32; 16000]); // 1 秒の無音
    }
    let (status, body) = s.post_audio(&encode_wav(&samples, 16000), &[]);
    assert_eq!(status, 200, "{body}");
    let text = body["text"].as_str().unwrap();
    assert_eq!(text.matches("こんにちは").count(), 4, "{text}");
    assert!(text.contains("7秒"), "{text}");
    assert!(text.contains("7秒。こんにちは"), "{text}");
}

#[test]
fn only_ip_or_own_name_is_accepted_as_host() {
    // よそのサイトの名前（DNS リバインディング）で来た要求は断る。IP・localhost・自分の名前は通す
    let s = start();
    let ask = |path: &str, host: &str| {
        s.raw(
            format!("GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n").as_bytes(),
        )
    };
    assert_eq!(ask("/health", "evil.example.com:8765"), 403);
    assert_eq!(ask("/dict/shared", "evil.example.com"), 403);
    for host in [
        "192.168.1.50:8765",
        "100.64.0.1:8765",
        "[fe80::1]:8765",
        "localhost:8765",
        "testmac",
        "TESTMAC.local:8765",
        "testmac.tail1234.ts.net:8765",
    ] {
        assert_eq!(ask("/health", host), 200, "{host}");
    }
}

#[test]
fn dictionary_is_kept_and_versions_are_checked() {
    let s = start();
    // まだ無い辞書
    assert_eq!(
        s.get("/dict/shared"),
        (200, json!({"store": "", "version": 0, "data": {}}))
    );
    // 預ける
    let (status, r) = s.put(
        "/dict/shared",
        &json!({"version": 0, "data": {"hotwords": [" くっきり版 ", "くっきり版"], "ほか": 1}}),
    );
    assert_eq!((status, r["version"].as_i64()), (200, Some(1)));
    let store = r["store"].as_str().unwrap().to_string();
    assert_eq!(store.len(), 32);
    // 読む（決まった形に整えてある）
    let (_, d) = s.get("/dict/shared");
    assert_eq!(d["store"], store.as_str());
    assert_eq!(
        d["data"],
        json!({"hotwords": ["くっきり版"], "fillers": [], "builtin_off": [], "replacements": {}, "snippets": {}})
    );
    // 古い版の番号で書こうとしたら断る（ほかのパソコンが先に書いた）
    assert_eq!(
        s.put("/dict/shared", &json!({"version": 0, "data": {}})).0,
        409
    );
    // 正しい版なら通り、預け先の番号は変わらない
    let (status, r) = s.put(
        "/dict/shared",
        &json!({"version": 1, "data": {"snippets": {"じゅうしょ": "東京都"}}}),
    );
    assert_eq!(
        (status, r["version"].as_i64(), r["store"].as_str()),
        (200, Some(2), Some(store.as_str()))
    );
    // Python 版と同じ形のファイル
    let file: Value = serde_json::from_str(
        &std::fs::read_to_string(s.dir.path().join("dicts").join("shared.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(file["version"], 2);
    assert_eq!(file["data"]["snippets"]["じゅうしょ"], "東京都");
    // おかしな名前・おかしな中身
    assert_eq!(s.get("/dict/..%2Fx").0, 400);
    assert_eq!(s.get("/dict/a.b").0, 400);
    assert_eq!(s.put("/dict/x", &json!({"version": 0})).0, 400);
    assert_eq!(
        s.put("/dict/x", &json!({"version": "abc", "data": {}})).0,
        400
    );
    assert_eq!(s.put("/dict/x", &json!([1, 2])).0, 400);
}

#[test]
fn dictionary_size_and_count_are_limited() {
    let s = start_with(
        &[],
        Limits {
            max_dict_bytes: 100,
            max_dict_files: 2,
            ..Limits::default()
        },
    );
    let big = json!({"version": 0, "data": {"hotwords": ["a".repeat(200)]}});
    assert_eq!(s.put("/dict/oversize", &big).0, 413);
    // 長さを言わない送り方でも同じ
    let body = big.to_string();
    let req = format!(
        "PUT /dict/oversize HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n{:x}\r\n{}\r\n0\r\n\r\n",
        body.len(),
        body
    );
    assert_eq!(s.raw(req.as_bytes()), 413);
    // 預かる辞書の数の上限（すでにある辞書の更新は通る）
    let small = |v: i64| json!({"version": v, "data": {"hotwords": ["a"]}});
    assert_eq!(s.put("/dict/d1", &small(0)).0, 200);
    assert_eq!(s.put("/dict/d2", &small(0)).0, 200);
    assert_eq!(s.put("/dict/d3", &small(0)).0, 429);
    assert_eq!(s.put("/dict/d1", &small(1)).0, 200);
}
