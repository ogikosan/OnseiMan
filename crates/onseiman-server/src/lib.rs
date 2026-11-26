//! OnseiMan の認識役（ほかのパソコンから届いた声を文字にして返す）。Python 版の `server/app.py` と同じ受け口。
//!
//! - `GET /health`・`POST /transcribe`・`GET|PUT /dict/{name}`
//! - 合言葉なしで使えるので、信頼できるネットワーク（家の Wi-Fi・Tailscale）の中でだけ動かす
//! - 守り: Host の確認（DNS リバインディング）・送信の大きさの上限・WAV と M4A だけ・長さの上限・
//!   同時の聞き取りの数・預かる辞書の数。話した内容と辞書の中身はログに残さない

use std::collections::HashMap;
use std::net::IpAddr;
use std::path::{Path as FsPath, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::body::Bytes;
use axum::extract::rejection::BytesRejection;
use axum::extract::{DefaultBodyLimit, Multipart, Path, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use onseiman_core::audio::{decode_wav, AudioError};
use onseiman_core::dict_format::{clean_data, valid_name};
use onseiman_core::segmenter::{join_texts, split_on_pauses, SegmentOptions};
use onseiman_speech::{SpeechEngine, MAX_SEGMENT_SEC};
use serde_json::{json, Map, Value};
use tokio::sync::Semaphore;

#[macro_export]
macro_rules! log {
    ($($arg:tt)*) => {
        eprintln!("{} {}", chrono::Local::now().format("%Y-%m-%d %H:%M:%S"), format!($($arg)*))
    };
}

/// 受け付ける量の上限
#[derive(Debug, Clone)]
pub struct Limits {
    /// 16kHz・16 ビットなら 30MB で約 15 分。OnseiMan は区切って送るので十分
    pub max_audio_bytes: usize,
    /// OnseiMan は区切って送る（iPhone のショートカットなどからは長いものも来る）
    pub max_audio_sec: f64,
    pub max_hotwords_chars: usize,
    pub convert_timeout: Duration,
    pub max_concurrent_transcribe: usize,
    pub max_dict_bytes: usize,
    pub max_dict_files: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_audio_bytes: 30 * 1024 * 1024,
            max_audio_sec: 300.0,
            max_hotwords_chars: 4000,
            convert_timeout: Duration::from_secs(15),
            max_concurrent_transcribe: 4,
            max_dict_bytes: 2 * 1024 * 1024,
            max_dict_files: 50,
        }
    }
}

pub struct Config {
    /// 預かった辞書を置く場所（`<これ>/<名前>.json`）。利用者のデータなので、更新しても消さない
    pub dict_dir: PathBuf,
    /// このパソコンの名前（/health で返す。Host の確認にも使う）
    pub server_name: String,
    pub limits: Limits,
}

struct AppState {
    engine: Arc<SpeechEngine>,
    cfg: Config,
    /// 起動ごとの番号（さがす側が「同じパソコンが 2 つの行き方で見つかった」と分かるように）
    server_id: String,
    transcribe_slots: Semaphore,
    dict_lock: Mutex<()>,
}

type Shared = Arc<AppState>;

fn fail(status: StatusCode, detail: &str) -> Response {
    (status, Json(json!({ "detail": detail }))).into_response()
}

/// ほかと重ならない 32 文字の番号（識別に使うだけで、秘密ではない）
fn new_id() -> String {
    use std::hash::{BuildHasher, Hasher};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut out = String::new();
    for salt in 0..2u64 {
        // RandomState は、作るたびに OS の乱数から鍵を取る
        let mut h = std::collections::hash_map::RandomState::new().build_hasher();
        h.write_u128(nanos);
        h.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed) + salt);
        h.write_u32(std::process::id());
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

/// Host が、IP アドレス・localhost・このパソコンの名前のときだけ受け付ける。
///
/// ブラウザで開いたよそのサイトが、自分の名前をこのパソコンの IP に向け直して（DNS リバインディング）
/// 預かった辞書を読み書きするのを防ぐ。OnseiMan のアプリと iPhone のショートカットは IP アドレスでつなぐ。
pub fn host_allowed(host: &str, server_name: &str) -> bool {
    let h = host.trim().to_lowercase();
    if h.is_empty() {
        return true; // ブラウザは必ず Host を付ける。付けないのは自作の道具だけ
    }
    let name = if let Some(rest) = h.strip_prefix('[') {
        rest.split(']').next().unwrap_or("")
    } else if h.matches(':').count() == 1 {
        h.rsplit_once(':').map(|(n, _)| n).unwrap_or(&h)
    } else {
        &h
    };
    if name.parse::<IpAddr>().is_ok() || name == "localhost" {
        return true;
    }
    let own = server_name.trim().to_lowercase();
    let own_label = own.split('.').next().unwrap_or("");
    if own_label.is_empty() {
        return false;
    }
    if name == own || name == own_label || name == format!("{own_label}.local") {
        return true;
    }
    if let Some(rest) = name.strip_prefix(&format!("{own_label}.")) {
        if let Some(tailnet) = rest.strip_suffix(".ts.net") {
            return !tailnet.is_empty() && !tailnet.contains('.');
        }
    }
    false
}

async fn guard_host(State(st): State<Shared>, req: Request, next: Next) -> Response {
    let host = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !host_allowed(host, &st.cfg.server_name) {
        return fail(StatusCode::FORBIDDEN, "Forbidden host");
    }
    if let Some(sfs) = req
        .headers()
        .get("sec-fetch-site")
        .and_then(|v| v.to_str().ok())
    {
        if sfs != "same-origin" && sfs != "none" {
            return fail(StatusCode::FORBIDDEN, "Forbidden cross-site request");
        }
    }
    if let Some(origin) = req
        .headers()
        .get(header::ORIGIN)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        let origin_host = origin
            .strip_prefix("http://")
            .or_else(|| origin.strip_prefix("https://"))
            .map(|s| s.split('/').next().unwrap_or(""));
        let req_host = host.trim();
        match origin_host {
            Some(oh)
                if !oh.is_empty() && !req_host.is_empty() && oh.eq_ignore_ascii_case(req_host) => {}
            _ => return fail(StatusCode::FORBIDDEN, "Forbidden origin"),
        }
    }
    next.run(req).await
}

async fn health(State(st): State<Shared>) -> Json<Value> {
    Json(json!({
        "status": "ok",
        "model": st.engine.model_name(),
        "provider": st.engine.active_provider(),
        "engine": "haas",
        // つなぐ側が、一度に送る長さを決めるのに使う
        "max_segment_sec": MAX_SEGMENT_SEC,
        "id": st.server_id,
        "name": st.cfg.server_name,
        // Python 版のアプリは、版が自分より古いときだけ「認識役が古い」と知らせる
        "version": onseiman_core::VERSION,
        "fingerprint": format!("rust-{}", onseiman_core::VERSION),
    }))
}

/// 優先語の読み（JSON の {書き方: [読み, ...]}）。おかしなものや大きすぎるものは使わない。
pub fn parse_readings(raw: &str, max_hotwords_chars: usize) -> HashMap<String, Vec<String>> {
    let mut out = HashMap::new();
    if raw.is_empty() || raw.chars().count() > max_hotwords_chars * 2 {
        return out;
    }
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(raw) else {
        return out;
    };
    for (k, v) in data.iter().take(200) {
        let n = k.chars().count();
        let Value::Array(items) = v else { continue };
        if n == 0 || n > 60 {
            continue;
        }
        let rs: Vec<String> = items
            .iter()
            .filter_map(Value::as_str)
            .filter(|r| (1..=60).contains(&r.chars().count()))
            .take(5)
            .map(str::to_string)
            .collect();
        if !rs.is_empty() {
            out.insert(k.clone(), rs);
        }
    }
    out
}

/// M4A（iPhone のショートカットで録音した音声など）。先頭 4〜8 バイトが ftyp。
pub fn is_m4a(raw: &[u8]) -> bool {
    raw.len() >= 12 && &raw[4..8] == b"ftyp"
}

fn which(name: &str) -> Option<PathBuf> {
    let exe = if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    };
    std::env::var_os("PATH").and_then(|paths| {
        std::env::split_paths(&paths)
            .map(|d| d.join(&exe))
            .find(|p| p.is_file())
    })
}

/// M4A を 16kHz・モノラルの WAV にする命令。Mac 標準の afconvert、無ければ ffmpeg。どちらも無ければ None。
fn m4a_command(src: &FsPath, dst: &FsPath, max_audio_sec: f64) -> Option<Command> {
    if let Some(af) = which("afconvert") {
        let mut c = Command::new(af);
        c.args(["-f", "WAVE", "-d", "LEI16@16000", "-c", "1"])
            .arg(src)
            .arg(dst);
        return Some(c);
    }
    let ff = which("ffmpeg")?;
    let mut c = Command::new(ff);
    // 外からのデータなので、読むのは手元のファイルだけ・mp4 の形として読むよう決めておく
    c.args([
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        "mov",
        "-i",
    ])
    .arg(src)
    .args([
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-t",
    ])
    .arg(((max_audio_sec as u64) + 1).to_string())
    .args(["-f", "wav", "-y"])
    .arg(dst);
    Some(c)
}

type Failure = (StatusCode, &'static str);

/// M4A を WAV にして返す。変換中も大きさを見て、上限を超えたら止める（小さな M4A が巨大な WAV になる対策）。
fn convert_m4a(raw: &[u8], limits: &Limits) -> Result<Vec<u8>, Failure> {
    let broken = (StatusCode::INTERNAL_SERVER_ERROR, "Audio conversion failed");
    let tmp = tempfile::Builder::new()
        .prefix("onseiman-")
        .tempdir()
        .map_err(|_| broken)?;
    let (src, dst) = (tmp.path().join("in.m4a"), tmp.path().join("out.wav"));
    std::fs::write(&src, raw).map_err(|_| broken)?;
    let mut cmd = m4a_command(&src, &dst, limits.max_audio_sec).ok_or((
        StatusCode::UNSUPPORTED_MEDIA_TYPE,
        "M4A is not supported on this server",
    ))?;
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| broken)?;
    let deadline = Instant::now() + limits.convert_timeout;
    let size = |p: &FsPath| std::fs::metadata(p).map(|m| m.len() as usize).unwrap_or(0);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => {}
            Err(_) => return Err(broken),
        }
        let over = size(&dst) > limits.max_audio_bytes;
        if over || Instant::now() > deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err((
                StatusCode::PAYLOAD_TOO_LARGE,
                if over {
                    "Audio too long"
                } else {
                    "Audio conversion took too long"
                },
            ));
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    if !status.success() || !dst.is_file() {
        return Err((StatusCode::BAD_REQUEST, "Invalid M4A audio"));
    }
    if size(&dst) > limits.max_audio_bytes {
        return Err((StatusCode::PAYLOAD_TOO_LARGE, "Audio too long"));
    }
    std::fs::read(&dst).map_err(|_| broken)
}

/// 届いた音声を文字にする（止まって待つ処理なので、別の係の上で動かす）。
fn recognize(
    st: &AppState,
    mut raw: Vec<u8>,
    hotwords: &str,
    readings: &HashMap<String, Vec<String>>,
) -> Result<Value, Failure> {
    let limits = &st.cfg.limits;
    // WAV と M4A だけを受け付ける（ほかの形式の読み取り処理に、外からのデータを渡さない）
    if is_m4a(&raw) {
        raw = convert_m4a(&raw, limits)?;
    }
    if raw.len() < 44 || &raw[..4] != b"RIFF" || &raw[8..12] != b"WAVE" {
        return Err((StatusCode::BAD_REQUEST, "Only WAV or M4A audio is accepted"));
    }
    let (samples, sr) = match decode_wav(&raw, Some(limits.max_audio_sec)) {
        Ok(v) => v,
        Err(AudioError::TooLong) => return Err((StatusCode::PAYLOAD_TOO_LARGE, "Audio too long")),
        Err(AudioError::Invalid) => return Err((StatusCode::BAD_REQUEST, "Invalid WAV audio")),
    };
    drop(raw);
    if !(8000..=48000).contains(&sr) {
        return Err((StatusCode::BAD_REQUEST, "Unsupported sample rate"));
    }
    let provider = st.engine.active_provider();
    if samples.is_empty() {
        return Ok(json!({"text": "", "elapsed_ms": 0.0, "provider": provider}));
    }
    let seconds = samples.len() as f64 / sr as f64;
    // OnseiMan のアプリは区切って送る。iPhone のショートカットなどから長い音声が来たら、ここで話の切れ目で区切る
    let parts = if seconds > MAX_SEGMENT_SEC {
        let options = SegmentOptions {
            max_segment_sec: MAX_SEGMENT_SEC,
            min_segment_sec: (MAX_SEGMENT_SEC * 0.5).min(4.0),
            ..SegmentOptions::default()
        };
        split_on_pauses(&samples, sr, options)
    } else {
        vec![samples]
    };
    let mut texts = Vec::with_capacity(parts.len());
    let mut elapsed_ms = 0.0;
    for part in &parts {
        let (text, ms) = st
            .engine
            .transcribe(part, sr, hotwords, readings)
            .map_err(|e| {
                log!("聞き取れませんでした: {e}");
                (StatusCode::INTERNAL_SERVER_ERROR, "Recognition failed")
            })?;
        texts.push(text);
        elapsed_ms += ms;
    }
    let text = join_texts(&texts);
    // 話した内容はログに残さない（文字数だけ）
    log!(
        "認識完了 ({elapsed_ms:.0} ms, provider={provider}, {seconds:.2}s audio, {}文字)",
        text.chars().count()
    );
    Ok(
        json!({"text": text, "elapsed_ms": (elapsed_ms * 10.0).round() / 10.0, "provider": provider}),
    )
}

async fn transcribe(State(st): State<Shared>, mut form: Multipart) -> Response {
    let (mut audio, mut hotwords, mut keywords) = (None::<Bytes>, String::new(), String::new());
    loop {
        let field = match form.next_field().await {
            Ok(Some(f)) => f,
            Ok(None) => break,
            Err(e) => return fail(e.status(), "Invalid form"),
        };
        let name = field.name().unwrap_or("").to_string();
        // language と itn は、前の版のアプリが送ってくる（Haas-ja は日本語専用なので使わない）
        let data = match field.bytes().await {
            Ok(b) => b,
            Err(e) => {
                return fail(
                    e.status(),
                    if e.status() == StatusCode::PAYLOAD_TOO_LARGE {
                        "Request too large"
                    } else {
                        "Invalid form"
                    },
                )
            }
        };
        match name.as_str() {
            "audio" => audio = Some(data),
            "hotwords" => hotwords = String::from_utf8_lossy(&data).into_owned(),
            "keywords" => keywords = String::from_utf8_lossy(&data).into_owned(),
            _ => {}
        }
    }
    let Some(audio) = audio.filter(|a| !a.is_empty()) else {
        return fail(StatusCode::BAD_REQUEST, "Empty audio file");
    };
    let limits = &st.cfg.limits;
    if audio.len() > limits.max_audio_bytes {
        return fail(StatusCode::PAYLOAD_TOO_LARGE, "Audio too large");
    }
    if hotwords.chars().count() > limits.max_hotwords_chars {
        hotwords = hotwords.chars().take(limits.max_hotwords_chars).collect();
    }
    let readings = parse_readings(&keywords, limits.max_hotwords_chars);
    let Ok(_slot) = st.transcribe_slots.acquire().await else {
        return fail(StatusCode::SERVICE_UNAVAILABLE, "Server is shutting down");
    };
    let state = st.clone();
    let done = tokio::task::spawn_blocking(move || {
        recognize(&state, audio.to_vec(), &hotwords, &readings)
    })
    .await;
    match done {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err((status, detail))) => fail(status, detail),
        Err(_) => fail(StatusCode::INTERNAL_SERVER_ERROR, "Recognition failed"),
    }
}

// ---- 辞書を預かる（各パソコンの OnseiMan が、ここを通して辞書を合わせる） ----

struct Stored {
    store: String,
    version: i64,
    data: Value,
}

fn read_dict(path: &FsPath) -> Result<Stored, Failure> {
    if !path.exists() {
        return Ok(Stored {
            store: String::new(),
            version: 0,
            data: json!({}),
        });
    }
    let broken = (
        StatusCode::INTERNAL_SERVER_ERROR,
        "Stored dictionary is broken",
    );
    let text = std::fs::read_to_string(path).map_err(|_| broken)?;
    let j: Value = serde_json::from_str(text.trim_start_matches('\u{feff}')).map_err(|_| broken)?;
    let obj = j.as_object().ok_or(broken)?;
    Ok(Stored {
        store: obj
            .get("store")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        version: obj
            .get("version")
            .map(|v| v.as_i64().ok_or(broken))
            .transpose()?
            .unwrap_or(0),
        data: match obj.get("data") {
            Some(Value::Object(m)) if !m.is_empty() => Value::Object(m.clone()),
            _ => json!({}),
        },
    })
}

fn dict_path(st: &AppState, name: &str) -> Result<PathBuf, Failure> {
    if !valid_name(name) {
        return Err((StatusCode::BAD_REQUEST, "Invalid dictionary name"));
    }
    Ok(st.cfg.dict_dir.join(format!("{name}.json")))
}

async fn get_dict(State(st): State<Shared>, Path(name): Path<String>) -> Response {
    let path = match dict_path(&st, &name) {
        Ok(p) => p,
        Err((s, d)) => return fail(s, d),
    };
    let _guard = st.dict_lock.lock().unwrap_or_else(|e| e.into_inner());
    match read_dict(&path) {
        Ok(d) => {
            Json(json!({"store": d.store, "version": d.version, "data": d.data})).into_response()
        }
        Err((s, d)) => {
            log!("預かった辞書を読めませんでした: {name}.json");
            fail(s, d)
        }
    }
}

fn as_version(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().filter(|f| f.fract() == 0.0).map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

fn store_dict(
    st: &AppState,
    name: &str,
    path: &FsPath,
    version: i64,
    data: &Map<String, Value>,
) -> Result<Value, Failure> {
    let broken = (
        StatusCode::INTERNAL_SERVER_ERROR,
        "Could not store the dictionary",
    );
    let dir = &st.cfg.dict_dir;
    let _guard = st.dict_lock.lock().unwrap_or_else(|e| e.into_inner());
    if !path.exists() {
        let count = std::fs::read_dir(dir)
            .map(|rd| {
                rd.flatten()
                    .filter(|e| e.path().extension().is_some_and(|x| x == "json"))
                    .count()
            })
            .unwrap_or(0);
        if count >= st.cfg.limits.max_dict_files {
            return Err((StatusCode::TOO_MANY_REQUESTS, "Too many dictionaries"));
        }
    }
    let cur = read_dict(path)?;
    if cur.version != version {
        // 読んだあとに、ほかのパソコンが書き換えた。読み直して合わせ直してもらう
        return Err((
            StatusCode::CONFLICT,
            "Dictionary was changed by another computer",
        ));
    }
    let store = if cur.store.is_empty() {
        new_id()
    } else {
        cur.store
    };
    let new = json!({"store": store, "version": version + 1, "data": data});
    std::fs::create_dir_all(dir).map_err(|_| broken)?;
    let tmp = dir.join(format!("{name}.json.tmp"));
    std::fs::write(
        &tmp,
        serde_json::to_string_pretty(&new).map_err(|_| broken)?,
    )
    .map_err(|_| broken)?;
    std::fs::rename(&tmp, path).map_err(|_| broken)?;
    // 中身はログに残さない（件数だけ）
    let items: usize = data
        .values()
        .map(|v| {
            v.as_array()
                .map(Vec::len)
                .or_else(|| v.as_object().map(Map::len))
                .unwrap_or(0)
        })
        .sum();
    log!(
        "辞書を預かりました: {name} 版 {}（{items} 件）",
        version + 1
    );
    Ok(json!({"store": store, "version": version + 1}))
}

async fn put_dict(
    State(st): State<Shared>,
    Path(name): Path<String>,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    let path = match dict_path(&st, &name) {
        Ok(p) => p,
        Err((s, d)) => return fail(s, d),
    };
    let raw = match body {
        Ok(b) => b,
        Err(e) => return fail(e.status(), "Request too large"),
    };
    let invalid = || fail(StatusCode::BAD_REQUEST, "Invalid dictionary");
    let Ok(Value::Object(body)) = serde_json::from_slice::<Value>(&raw) else {
        return invalid();
    };
    let (Some(version), Some(data @ Value::Object(_))) =
        (body.get("version").and_then(as_version), body.get("data"))
    else {
        return invalid();
    };
    let data = clean_data(data, None);
    match store_dict(&st, &name, &path, version, &data) {
        Ok(v) => Json(v).into_response(),
        Err((s, d)) => fail(s, d),
    }
}

async fn not_found() -> Response {
    fail(StatusCode::NOT_FOUND, "Not Found")
}

/// 受け口を組み立てる。
pub fn app(engine: Arc<SpeechEngine>, cfg: Config) -> Router {
    let limits = cfg.limits.clone();
    let state: Shared = Arc::new(AppState {
        engine,
        server_id: new_id(),
        transcribe_slots: Semaphore::new(limits.max_concurrent_transcribe),
        dict_lock: Mutex::new(()),
        cfg,
    });
    Router::new()
        .route("/health", get(health))
        // 長さを言わない送り方（chunked）でも、流れてきた量が上限を超えたところで受け取りをやめる
        .route(
            "/transcribe",
            post(transcribe).layer(DefaultBodyLimit::max(limits.max_audio_bytes + 64 * 1024)),
        )
        .route(
            "/dict/{name}",
            get(get_dict)
                .put(put_dict)
                .layer(DefaultBodyLimit::max(limits.max_dict_bytes)),
        )
        .fallback(not_found)
        .layer(middleware::from_fn_with_state(state.clone(), guard_host))
        .with_state(state)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hosts() {
        for ok in [
            "192.168.1.50:8765",
            "100.64.0.1:8765",
            "[fe80::1]:8765",
            "localhost:8765",
            "",
            "mac",
            "MAC.local:8765",
            "mac.tail1234.ts.net:8765",
        ] {
            assert!(host_allowed(ok, "Mac"), "{ok}");
        }
        for ng in [
            "evil.example.com:8765",
            "evil.example.com",
            "mac.evil.example.com:8765",
            "mac.evil.example.com",
            "mac.a.b.ts.net",
            "macx.local",
            "192.168.1.50.evil.com",
        ] {
            assert!(!host_allowed(ng, "Mac"), "{ng}");
        }
    }

    #[test]
    fn readings() {
        let r = parse_readings(
            r#"{"くっきり版": ["クッキリバン", "", 5], "x": "y", "": ["a"]}"#,
            4000,
        );
        assert_eq!(r.len(), 1);
        assert_eq!(r["くっきり版"], vec!["クッキリバン".to_string()]);
        assert!(parse_readings("[1,2]", 4000).is_empty());
        assert!(parse_readings("こわれた", 4000).is_empty());
        assert!(parse_readings(&format!("{{\"a\": [\"{}\"]}}", "x".repeat(9000)), 4000).is_empty());
    }

    #[test]
    fn m4a_head() {
        assert!(is_m4a(b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00"));
        assert!(!is_m4a(b"RIFF\x00\x00\x00\x00WAVEfmt "));
    }

    #[test]
    fn ids_differ() {
        let (a, b) = (new_id(), new_id());
        assert_eq!(a.len(), 32);
        assert_ne!(a, b);
    }
}
