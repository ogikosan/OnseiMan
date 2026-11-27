//! ローカル管理画面（`client/web/`）と REST API を `127.0.0.1` で提供する Axum サーバー。
//!
//! Python 版 `client/web_ui.py` と同じセキュリティ防御（Host / Origin / Sec-Fetch-Site /
//! JSON 限定 POST / CSP / クリックジャッキング防止）と JSON 契約を保つ。

use std::collections::HashMap;
use std::path::{Path as FsPath, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::extract::{DefaultBodyLimit, Path, Request, State};
use axum::http::{header, HeaderValue, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use onseiman_core::config::ClientConfig;
use onseiman_core::dict_format::valid_name;
use onseiman_core::dictionary::CustomDictionary;
use onseiman_core::fillers::DEFAULT_FILLERS;
use onseiman_core::history::HistoryStore;
use onseiman_core::learner::{CandidateStore, LearningCandidate};
use onseiman_core::llm_refine::{LlmRefiner, TEST_TEXT};
use onseiman_core::normalize::BUILTIN_TERMS;
use onseiman_platform::binding::parse_binding;
use onseiman_platform::clipboard;
use onseiman_platform::hotkey::{GlobalHotkey, HotkeyConfig};
use onseiman_platform::recorder::list_input_devices;
use onseiman_platform::secret_store;
use onseiman_platform::startup;
use serde_json::{json, Map, Value};

fn providers_json() -> Value {
    json!({
        "openai":    { "label": "OpenAI",               "default_model": "gpt-5.4-mini",           "needs_key": true,  "needs_url": false },
        "anthropic": { "label": "Anthropic (Claude)",   "default_model": "claude-haiku-5-5",       "needs_key": true,  "needs_url": false },
        "gemini":    { "label": "Google Gemini",        "default_model": "gemini-3.5-flash-lite",  "needs_key": true,  "needs_url": false },
        "ollama":    { "label": "ローカル AI (Ollama)", "default_model": "",                       "needs_key": false, "needs_url": true  }
    })
}

fn read_json_obj(path: &FsPath) -> Map<String, Value> {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(s.strip_prefix('\u{feff}').unwrap_or(&s)).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn write_json_obj(path: &FsPath, obj: &Map<String, Value>) -> Result<(), String> {
    if let Some(p) = path.parent() {
        let _ = std::fs::create_dir_all(p);
    }
    let tmp = path.with_extension("json.tmp");
    let text = serde_json::to_string_pretty(&Value::Object(obj.clone()))
        .map_err(|e| e.to_string())?
        + "\n";
    std::fs::write(&tmp, text).map_err(|e| e.to_string())?;
    std::fs::rename(&tmp, path).map_err(|e| e.to_string())
}

use crate::chatgpt_auth;
use crate::discovery::find_servers;
use crate::pipeline::format_utterance_with_root;
use crate::transcriber::HybridTranscriber;

pub const DEFAULT_MIC_LABEL: &str = "既定のマイク";
const CSP_HEADER: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'";

#[derive(Debug, Clone, serde::Serialize)]
pub struct MicTestState {
    pub state: String,
    pub level: f32,
    pub peak: f32,
    pub left: f32,
    pub text: String,
    pub verdict: String,
    pub verdict_kind: String,
    pub backend: String,
}

impl Default for MicTestState {
    fn default() -> Self {
        Self {
            state: "idle".into(),
            level: 0.0,
            peak: 0.0,
            left: 0.0,
            text: String::new(),
            verdict: String::new(),
            verdict_kind: String::new(),
            backend: String::new(),
        }
    }
}

pub fn mic_verdict(peak: f32) -> (&'static str, &'static str) {
    if peak < 0.01 {
        (
            "音がほとんど入っていません。マイクの選び方と、Windows の「設定」→「プライバシー」→「マイク」でアプリがマイクを使えるか、マイクの音量を確かめてください",
            "ng",
        )
    } else if peak < 0.06 {
        (
            "声が小さいです。マイクに近づくか、Windows のマイクの音量を上げてください",
            "warn",
        )
    } else if peak > 0.98 {
        (
            "音が大きすぎて割れています。Windows のマイクの音量を下げてください",
            "warn",
        )
    } else {
        ("ちょうどよい大きさです", "ok")
    }
}

pub struct AppContext {
    pub root_dir: PathBuf,
    pub web_dir: PathBuf,
    pub config_path: PathBuf,
    pub dict_path: PathBuf,
    pub audio_dir: PathBuf,
    pub port: Mutex<u16>,
    pub config: Mutex<ClientConfig>,
    pub dict: Mutex<CustomDictionary>,
    pub history: Mutex<HistoryStore>,
    pub candidates: Mutex<CandidateStore>,
    pub transcriber: Arc<HybridTranscriber>,
    pub pending_tab: Mutex<String>,
    pub latest_update: Mutex<Option<Value>>,
    pub mic_test: Mutex<MicTestState>,
    pub chatgpt_signing_in: Mutex<bool>,
    pub chatgpt_error: Mutex<String>,
    pub dict_sync_status: Mutex<Value>,
}

impl AppContext {
    pub fn new(root_dir: PathBuf) -> Self {
        let config_path = root_dir.join("config.json");
        let dict_path = root_dir.join("dict.json");
        let logs_dir = root_dir.join("logs");
        let audio_dir = logs_dir.join("audio");
        let web_dir = root_dir.join("client").join("web");
        let cfg = ClientConfig::load(&config_path);
        let dict = CustomDictionary::load(&dict_path);
        let history = HistoryStore::open(&logs_dir, 200);
        let candidates = CandidateStore::open(&logs_dir);
        let transcriber = Arc::new(HybridTranscriber::new(root_dir.clone()));
        Self {
            root_dir,
            web_dir,
            config_path,
            dict_path,
            audio_dir,
            port: Mutex::new(18765),
            config: Mutex::new(cfg),
            dict: Mutex::new(dict),
            history: Mutex::new(history),
            candidates: Mutex::new(candidates),
            transcriber,
            pending_tab: Mutex::new(String::new()),
            latest_update: Mutex::new(None),
            mic_test: Mutex::new(MicTestState::default()),
            chatgpt_signing_in: Mutex::new(false),
            chatgpt_error: Mutex::new(String::new()),
            dict_sync_status: Mutex::new(json!({})),
        }
    }

    pub fn set_port(&self, port: u16) {
        if let Ok(mut p) = self.port.lock() {
            *p = port;
        }
    }

    pub fn port(&self) -> u16 {
        self.port.lock().map(|p| *p).unwrap_or(18765)
    }

    pub fn reload_dict(&self) {
        let fresh = CustomDictionary::load(&self.dict_path);
        if let Ok(mut d) = self.dict.lock() {
            *d = fresh;
        }
    }
}

pub fn router(ctx: Arc<AppContext>) -> Router {
    Router::new()
        .route("/api/state", get(get_state))
        .route("/api/dict", get(get_dict).post(post_dict))
        .route("/api/settings", get(get_settings).post(post_settings))
        .route("/api/ui-request", get(get_ui_request))
        .route("/api/dict-sync", get(get_dict_sync))
        .route("/api/mic_test", get(get_mic_test))
        .route("/api/audio/{name}", get(get_audio))
        .route("/api/copy", post(post_copy))
        .route("/api/quick_dict", post(post_quick_dict))
        .route("/api/llm/test", post(post_llm_test))
        .route("/api/llm/models", post(post_llm_models))
        .route("/api/retranscribe", post(post_retranscribe))
        .route("/api/retry", post(post_retry))
        .route("/api/find_servers", post(post_find_servers))
        .route("/api/candidates/approve", post(post_candidates_approve))
        .route("/api/candidates/reject", post(post_candidates_reject))
        .route("/api/candidates/restore", post(post_candidates_restore))
        .route("/api/learn_manual", post(post_learn_manual))
        .route("/api/capture_hotkey", post(post_capture_hotkey))
        .route("/api/capture_extra_hotkey", post(post_capture_extra_hotkey))
        .route("/api/clear_extra_hotkey", post(post_clear_extra_hotkey))
        .route("/api/capture_ai_hotkey", post(post_capture_ai_hotkey))
        .route("/api/clear_ai_hotkey", post(post_clear_ai_hotkey))
        .route("/api/dict-sync/now", post(post_dict_sync_now))
        .route("/api/update/check", post(post_update_check))
        .route("/api/update/run", post(post_update_run))
        .route("/api/theme", post(post_theme))
        .route("/api/mic_test/start", post(post_mic_test_start))
        .route("/api/chatgpt/status", get(get_chatgpt_status))
        .route("/api/chatgpt/signin", post(post_chatgpt_signin))
        .route("/api/chatgpt/signout", post(post_chatgpt_signout))
        .route("/", get(get_index))
        .route("/{*path}", get(get_static))
        .layer(DefaultBodyLimit::max(2_000_000))
        .layer(middleware::from_fn_with_state(ctx.clone(), security_guard))
        .with_state(ctx)
}

fn host_allowed(host: &str, port: u16) -> bool {
    let h = host.trim().to_lowercase();
    if h.is_empty() {
        return true;
    }
    let (name, hport) = if h.matches(':').count() == 1 {
        let mut sp = h.splitn(2, ':');
        (sp.next().unwrap_or(""), sp.next().unwrap_or(""))
    } else {
        (h.as_str(), "")
    };
    matches!(name, "127.0.0.1" | "localhost") && (hport.is_empty() || hport == port.to_string())
}

fn origin_allowed(origin: &str, port: u16) -> bool {
    let o = origin.trim().to_lowercase();
    if o.is_empty() {
        return true;
    }
    o == format!("http://127.0.0.1:{port}") || o == format!("http://localhost:{port}")
}

fn fetch_site_allowed(site: &str) -> bool {
    matches!(
        site.trim().to_lowercase().as_str(),
        "" | "same-origin" | "none"
    )
}

async fn security_guard(State(ctx): State<Arc<AppContext>>, req: Request, next: Next) -> Response {
    let port = ctx.port();
    let host = req
        .headers()
        .get(header::HOST)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    let site = req
        .headers()
        .get("sec-fetch-site")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");

    if !host_allowed(host, port) || !fetch_site_allowed(site) {
        return with_security_headers(StatusCode::FORBIDDEN.into_response());
    }

    if req.method() == Method::POST {
        let origin = req
            .headers()
            .get(header::ORIGIN)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("");
        let ctype = req
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .to_lowercase();
        let clen: usize = req
            .headers()
            .get(header::CONTENT_LENGTH)
            .and_then(|v| v.to_str().ok())
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);
        if !origin_allowed(origin, port)
            || (!ctype.is_empty() && ctype != "application/json")
            || (ctype.is_empty() && clen > 0)
        {
            return with_security_headers(StatusCode::FORBIDDEN.into_response());
        }
    }

    let resp = next.run(req).await;
    with_security_headers(resp)
}

fn with_security_headers(mut resp: Response) -> Response {
    let h = resp.headers_mut();
    h.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    h.insert(header::X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    h.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    h.insert(
        "cross-origin-resource-policy",
        HeaderValue::from_static("same-origin"),
    );
    h.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static(CSP_HEADER),
    );
    resp
}

fn json_no_store(status: StatusCode, val: Value) -> Response {
    let mut resp = (status, Json(val)).into_response();
    resp.headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    resp
}

// -------------------- GET Handlers --------------------

async fn get_state(State(ctx): State<Arc<AppContext>>) -> Response {
    let cfg = ctx.config.lock().unwrap().clone();
    let backend = ctx.transcriber.last_backend_status();
    let server_name = ctx.transcriber.preferred_server_name();
    let (history_entries, stats) = {
        let hist = ctx.history.lock().unwrap();
        let entries = hist
            .get_history()
            .into_iter()
            .take(200)
            .map(|e| {
                let has_audio = !e.audio.is_empty() && ctx.audio_dir.join(&e.audio).is_file();
                let raw_text = if e.raw_text.is_empty() {
                    e.text.clone()
                } else {
                    e.raw_text.clone()
                };
                json!({
                    "timestamp": e.timestamp,
                    "backend": e.backend,
                    "elapsed_ms": (e.elapsed_ms * 10.0).round() / 10.0,
                    "duration_sec": (e.duration_sec * 100.0).round() / 100.0,
                    "raw_text": raw_text,
                    "text": e.text,
                    "audio": e.audio,
                    "has_audio": has_audio,
                    "failed": e.failed,
                })
            })
            .collect::<Vec<_>>();
        (entries, hist.stats())
    };

    let mode_label = if cfg.hotkey_mode.eq_ignore_ascii_case("hold") {
        "長押し"
    } else {
        "切替"
    };
    let hotkey_info = format!("{} ({})", cfg.hotkey, mode_label);

    let (cands, dism) = {
        let cs = ctx.candidates.lock().unwrap();
        (cs.candidates().to_vec(), cs.dismissed().to_vec())
    };
    json_no_store(
        StatusCode::OK,
        json!({
            "backend": backend,
            "server_name": server_name,
            "hotkey_info": hotkey_info,
            "hotkey": cfg.hotkey,
            "ai_hotkey": cfg.ai_hotkey,
            "hotkey_extra": cfg.hotkey_extra,
            "history": history_entries,
            "stats": stats,
            "candidates": cands,
            "dismissed": dism,
            "update": ctx.latest_update.lock().ok().and_then(|g| g.clone()).unwrap_or(Value::Null),
            "version": env!("CARGO_PKG_VERSION"),
            "ui_theme": cfg.ui_theme,
            "server_outdated": [],
        }),
    )
}

async fn get_dict(State(ctx): State<Arc<AppContext>>) -> Response {
    let data = read_json_obj(&ctx.dict_path);
    let fillers: Value = match data.get("fillers") {
        Some(Value::Array(arr)) => Value::Array(arr.clone()),
        _ => json!(DEFAULT_FILLERS),
    };
    let builtin_terms: HashMap<&str, &str> = BUILTIN_TERMS.iter().copied().collect();
    json_no_store(
        StatusCode::OK,
        json!({
            "replacements": data.get("replacements").cloned().unwrap_or_else(|| json!({})),
            "snippets": data.get("snippets").cloned().unwrap_or_else(|| json!({})),
            "hotwords": data.get("hotwords").cloned().unwrap_or_else(|| json!([])),
            "fillers": fillers,
            "builtin_off": data.get("builtin_off").cloned().unwrap_or_else(|| json!([])),
            "default_fillers": DEFAULT_FILLERS,
            "builtin_terms": builtin_terms,
        }),
    )
}

async fn get_settings(State(ctx): State<Arc<AppContext>>) -> Response {
    let cfg = ClientConfig::load(&ctx.config_path);
    let mut mics = vec![DEFAULT_MIC_LABEL.to_string()];
    mics.extend(list_input_devices());
    let current_mic = cfg
        .microphone
        .as_ref()
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| DEFAULT_MIC_LABEL.to_string());
    if !mics.contains(&current_mic) {
        mics.push(current_mic.clone());
    }
    let llm_key_set = json!({
        "openai": !cfg.llm_api_keys.get("openai").map(|s| s.is_empty()).unwrap_or(true),
        "anthropic": !cfg.llm_api_keys.get("anthropic").map(|s| s.is_empty()).unwrap_or(true),
        "gemini": !cfg.llm_api_keys.get("gemini").map(|s| s.is_empty()).unwrap_or(true),
        "ollama": false,
    });
    let place = if cfg.recognition_place.is_empty() {
        if cfg.server_urls.is_empty() {
            "local"
        } else {
            "server"
        }
    } else {
        cfg.recognition_place.as_str()
    };

    json_no_store(
        StatusCode::OK,
        json!({
            "hotkey": cfg.hotkey,
            "ai_hotkey": cfg.ai_hotkey,
            "hotkey_extra": cfg.hotkey_extra,
            "hotkey_mode": if cfg.hotkey_mode.eq_ignore_ascii_case("hold") { "hold" } else { "toggle" },
            "auto_stop_on_silence": cfg.auto_stop_on_silence,
            "silence_stop_sec": cfg.silence_stop_sec,
            "cancel_key": cfg.cancel_key,
            "max_duration_sec": cfg.max_duration_sec as i64,
            "microphone": current_mic,
            "microphones": mics,
            "startup_enabled": startup::is_enabled("OnseiMan"),
            "remove_fillers": cfg.remove_fillers,
            "normalize_text": cfg.normalize_text,
            "voice_commands": cfg.voice_commands,
            "undo_voice_command": cfg.undo_voice_command,
            "keep_mic_open": cfg.keep_mic_open,
            "overlay_pinned": cfg.overlay_position.is_some(),
            "recording_indicator": cfg.recording_indicator,
            "mute_other_audio": cfg.mute_other_audio,
            "overlay_place": cfg.overlay_place,
            "live_segmentation": cfg.live_segmentation,
            "strip_trailing_period": cfg.strip_trailing_period,
            "app_specific_rules": cfg.app_specific_rules,
            "learn_from_edits": cfg.learn_from_edits,
            "auto_learn_after_count": cfg.auto_learn_after_count,
            "restore_clipboard": cfg.restore_clipboard,
            "beep_enabled": cfg.beep_enabled,
            "show_overlay": cfg.show_overlay,
            "server_urls": cfg.server_urls,
            "server_names": cfg.server_names,
            "preferred_server": cfg.preferred_server,
            "recognition_place": place,
            "local_fallback": cfg.local_fallback,
            "server_info": ctx.transcriber.server_info_snapshot(),
            "dict_share": if matches!(cfg.dict_share.as_str(), "server" | "folder") { cfg.dict_share.as_str() } else { "" },
            "dict_share_folder": cfg.dict_share_folder,
            "dict_share_name": cfg.dict_share_name,
            "llm_provider": cfg.llm_provider,
            "llm_auto_refine": cfg.llm_auto_refine,
            "llm_base_url": cfg.llm_base_url,
            "llm_model": cfg.llm_model,
            "llm_key_set": llm_key_set,
            "llm_providers": providers_json(),
        }),
    )
}

async fn get_ui_request(State(ctx): State<Arc<AppContext>>) -> Response {
    let tab = {
        let mut p = ctx.pending_tab.lock().unwrap();
        std::mem::take(&mut *p)
    };
    json_no_store(StatusCode::OK, json!({ "tab": tab }))
}

async fn get_dict_sync(State(ctx): State<Arc<AppContext>>) -> Response {
    let status = ctx
        .dict_sync_status
        .lock()
        .map(|g| g.clone())
        .unwrap_or_else(|_| json!({}));
    json_no_store(
        StatusCode::OK,
        json!({ "status": status, "folders": detect_sync_folders() }),
    )
}

async fn get_mic_test(State(ctx): State<Arc<AppContext>>) -> Response {
    let st = ctx.mic_test.lock().unwrap().clone();
    json_no_store(StatusCode::OK, serde_json::to_value(st).unwrap_or_default())
}

async fn post_mic_test_start(State(ctx): State<Arc<AppContext>>) -> Response {
    {
        let mut st = ctx.mic_test.lock().unwrap();
        if st.state == "recording" || st.state == "recognizing" {
            return json_no_store(StatusCode::OK, json!({ "ok": true }));
        }
        *st = MicTestState {
            state: "recording".into(),
            left: 5.0,
            ..Default::default()
        };
    }

    let ctx_bg = Arc::clone(&ctx);
    std::thread::spawn(move || {
        use std::sync::atomic::{AtomicU32, Ordering};
        use std::time::{Duration, Instant};

        let cfg = ctx_bg.config.lock().unwrap().clone();
        let mic_name = cfg
            .microphone
            .as_ref()
            .and_then(|v| v.as_str())
            .map(String::from);
        let peak_bits = Arc::new(AtomicU32::new(0u32));
        let level_bits = Arc::new(AtomicU32::new(0u32));
        let pb = Arc::clone(&peak_bits);
        let lb = Arc::clone(&level_bits);
        let on_level: Arc<dyn Fn(f32) + Send + Sync> = Arc::new(move |lvl: f32| {
            lb.store(lvl.to_bits(), Ordering::Relaxed);
            let raw_est = (lvl / 10.0).min(1.0);
            let mut cur = pb.load(Ordering::Relaxed);
            while raw_est > f32::from_bits(cur) {
                match pb.compare_exchange_weak(
                    cur,
                    raw_est.to_bits(),
                    Ordering::Relaxed,
                    Ordering::Relaxed,
                ) {
                    Ok(_) => break,
                    Err(v) => cur = v,
                }
            }
        });

        let handle =
            match onseiman_platform::recorder::start_recording(mic_name.as_deref(), Some(on_level))
            {
                Ok(h) => h,
                Err(e) => {
                    if let Ok(mut st) = ctx_bg.mic_test.lock() {
                        st.state = "error".into();
                        st.verdict = format!(
                            "マイクを開けませんでした（{e}）。マイクの選び方を確かめてください"
                        );
                        st.verdict_kind = "ng".into();
                    }
                    return;
                }
            };

        let t0 = Instant::now();
        while t0.elapsed() < Duration::from_secs(5) {
            let left = (5.0 - t0.elapsed().as_secs_f32()).max(0.0);
            if let Ok(mut st) = ctx_bg.mic_test.lock() {
                st.level = f32::from_bits(level_bits.load(Ordering::Relaxed));
                st.peak = f32::from_bits(peak_bits.load(Ordering::Relaxed));
                st.left = (left * 10.0).round() / 10.0;
            }
            std::thread::sleep(Duration::from_millis(100));
        }

        let samples = handle.stop();
        let peak = samples
            .iter()
            .fold(0.0f32, |acc, &x| acc.max(x.abs()))
            .max(f32::from_bits(peak_bits.load(Ordering::Relaxed)));
        let (verdict, kind) = mic_verdict(peak);
        if let Ok(mut st) = ctx_bg.mic_test.lock() {
            st.state = "recognizing".into();
            st.level = 0.0;
            st.peak = peak;
            st.left = 0.0;
            st.verdict = verdict.into();
            st.verdict_kind = kind.into();
        }
        if kind == "ng" || samples.is_empty() {
            if let Ok(mut st) = ctx_bg.mic_test.lock() {
                st.state = "done".into();
            }
            return;
        }
        let wav = onseiman_core::audio::encode_wav(&samples, 16_000);
        let dict = ctx_bg.dict.lock().unwrap().clone();
        let hotwords = dict.active_hotwords();
        let readings = dict.readings();
        match ctx_bg
            .transcriber
            .transcribe_wav(&cfg, &wav, &hotwords, &readings)
        {
            Ok(res) => {
                if let Ok(mut st) = ctx_bg.mic_test.lock() {
                    st.state = "done".into();
                    st.text = res.text.trim().to_string();
                    st.backend = res.backend;
                }
            }
            Err(e) => {
                if let Ok(mut st) = ctx_bg.mic_test.lock() {
                    st.state = "done".into();
                    st.text = String::new();
                    st.backend = format!("文字にできませんでした（{e}）");
                }
            }
        }
    });

    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

/// `audio_dir` 配下の単一 `.wav` ファイル名だけを許可し、正規化済みパスを返す。
fn resolve_safe_wav_path(audio_dir: &FsPath, name: &str) -> Result<PathBuf, StatusCode> {
    if name.is_empty() || name.contains('/') || name.contains('\\') || name.contains("..") {
        return Err(StatusCode::NOT_FOUND);
    }
    let mut comps = FsPath::new(name).components();
    match (comps.next(), comps.next()) {
        (Some(std::path::Component::Normal(_)), None) => {}
        _ => return Err(StatusCode::NOT_FOUND),
    }
    let base = audio_dir
        .canonicalize()
        .map_err(|_| StatusCode::NOT_FOUND)?;
    let target = base
        .join(name)
        .canonicalize()
        .map_err(|_| StatusCode::NOT_FOUND)?;
    if !target.starts_with(&base) {
        return Err(StatusCode::FORBIDDEN);
    }
    if target
        .extension()
        .and_then(|e| e.to_str())
        .map(|s| s.to_lowercase())
        != Some("wav".into())
        || !target.is_file()
    {
        return Err(StatusCode::NOT_FOUND);
    }
    Ok(target)
}

async fn get_audio(
    State(ctx): State<Arc<AppContext>>,
    Path(name): Path<String>,
    req: Request,
) -> Response {
    let target = match resolve_safe_wav_path(&ctx.audio_dir, &name) {
        Ok(p) => p,
        Err(st) => return st.into_response(),
    };
    let Ok(data) = std::fs::read(&target) else {
        return StatusCode::NOT_FOUND.into_response();
    };
    let total = data.len();
    if total > 0 {
        if let Some(rng) = req
            .headers()
            .get(header::RANGE)
            .and_then(|v| v.to_str().ok())
        {
            if let Some(rest) = rng.strip_prefix("bytes=") {
                let part = rest.split(',').next().unwrap_or("").trim();
                if let Some((s_str, e_str)) = part.split_once('-') {
                    let start = s_str
                        .parse::<usize>()
                        .unwrap_or(0)
                        .min(total.saturating_sub(1));
                    let end = e_str
                        .parse::<usize>()
                        .unwrap_or(total.saturating_sub(1))
                        .clamp(start, total.saturating_sub(1));
                    let chunk = data[start..=end].to_vec();
                    return Response::builder()
                        .status(StatusCode::PARTIAL_CONTENT)
                        .header(header::CONTENT_TYPE, "audio/wav")
                        .header(header::ACCEPT_RANGES, "bytes")
                        .header(header::CACHE_CONTROL, "no-store")
                        .header(
                            header::CONTENT_RANGE,
                            format!("bytes {start}-{end}/{total}"),
                        )
                        .header(header::CONTENT_LENGTH, chunk.len())
                        .body(Body::from(chunk))
                        .unwrap();
                }
            }
        }
    }
    Response::builder()
        .status(StatusCode::OK)
        .header(header::CONTENT_TYPE, "audio/wav")
        .header(header::ACCEPT_RANGES, "bytes")
        .header(header::CACHE_CONTROL, "no-store")
        .header(header::CONTENT_LENGTH, total)
        .body(Body::from(data))
        .unwrap()
}

async fn get_index(State(ctx): State<Arc<AppContext>>) -> Response {
    serve_static(&ctx.web_dir, "index.html")
}

async fn get_static(State(ctx): State<Arc<AppContext>>, Path(path): Path<String>) -> Response {
    serve_static(&ctx.web_dir, &path)
}

fn embedded_web_asset(rel: &str) -> Option<(&'static [u8], &'static str)> {
    match rel {
        "index.html" => Some((
            include_bytes!("../../../client/web/index.html"),
            "text/html; charset=utf-8",
        )),
        "style.css" => Some((
            include_bytes!("../../../client/web/style.css"),
            "text/css; charset=utf-8",
        )),
        "app.js" => Some((
            include_bytes!("../../../client/web/app.js"),
            "application/javascript; charset=utf-8",
        )),
        "theme.js" => Some((
            include_bytes!("../../../client/web/theme.js"),
            "application/javascript; charset=utf-8",
        )),
        "bg.jpg" => Some((include_bytes!("../../../client/web/bg.jpg"), "image/jpeg")),
        "bg-dark.jpg" => Some((
            include_bytes!("../../../client/web/bg-dark.jpg"),
            "image/jpeg",
        )),
        "img/pill_rec.png" => Some((
            include_bytes!("../../../client/web/img/pill_rec.png"),
            "image/png",
        )),
        "img/pill_rec_dark.png" => Some((
            include_bytes!("../../../client/web/img/pill_rec_dark.png"),
            "image/png",
        )),
        "img/pill_busy.png" => Some((
            include_bytes!("../../../client/web/img/pill_busy.png"),
            "image/png",
        )),
        "img/pill_busy_dark.png" => Some((
            include_bytes!("../../../client/web/img/pill_busy_dark.png"),
            "image/png",
        )),
        "img/pill_done.png" => Some((
            include_bytes!("../../../client/web/img/pill_done.png"),
            "image/png",
        )),
        "img/pill_done_dark.png" => Some((
            include_bytes!("../../../client/web/img/pill_done_dark.png"),
            "image/png",
        )),
        _ => None,
    }
}

fn serve_static(web_dir: &FsPath, rel: &str) -> Response {
    let rel = rel.trim_start_matches('/');
    let rel = if rel.is_empty() { "index.html" } else { rel };
    if rel.contains('\\')
        || rel.contains('\0')
        || !FsPath::new(rel)
            .components()
            .all(|c| matches!(c, std::path::Component::Normal(_)))
    {
        return StatusCode::FORBIDDEN.into_response();
    }
    let cache = if rel.ends_with(".jpg") {
        "public, max-age=86400"
    } else {
        "no-cache"
    };
    if let Ok(base) = web_dir.canonicalize() {
        if let Ok(target) = base.join(rel).canonicalize() {
            if !target.starts_with(&base) {
                return StatusCode::FORBIDDEN.into_response();
            }
            if target.is_file() {
                if let Ok(data) = std::fs::read(&target) {
                    let ctype = match target.extension().and_then(|e| e.to_str()).unwrap_or("") {
                        "html" => "text/html; charset=utf-8",
                        "css" => "text/css; charset=utf-8",
                        "js" => "application/javascript; charset=utf-8",
                        "json" => "application/json; charset=utf-8",
                        "svg" => "image/svg+xml",
                        "png" => "image/png",
                        "jpg" | "jpeg" => "image/jpeg",
                        "ico" => "image/x-icon",
                        _ => "application/octet-stream",
                    };
                    return Response::builder()
                        .status(StatusCode::OK)
                        .header(header::CONTENT_TYPE, ctype)
                        .header(header::CACHE_CONTROL, cache)
                        .header(header::CONTENT_LENGTH, data.len())
                        .body(Body::from(data))
                        .unwrap();
                }
            }
        }
    }
    if let Some((bytes, ctype)) = embedded_web_asset(rel) {
        return Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, ctype)
            .header(header::CACHE_CONTROL, cache)
            .header(header::CONTENT_LENGTH, bytes.len())
            .body(Body::from(bytes))
            .unwrap();
    }
    StatusCode::NOT_FOUND.into_response()
}

// -------------------- POST Handlers --------------------

async fn post_copy(Json(body): Json<Value>) -> Response {
    if let Some(text) = body.get("text").and_then(|v| v.as_str()) {
        if !text.is_empty() {
            let _ = clipboard::set_text(text);
        }
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_quick_dict(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let src = body
        .get("src")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let dst = body
        .get("dst")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let also_hw = body
        .get("also_hotword")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    if src.is_empty() || dst.is_empty() {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "誤認識語と正しい言葉を入力してください" }),
        );
    }
    let mut data = read_json_obj(&ctx.dict_path);
    let reps = data
        .entry("replacements".to_string())
        .or_insert_with(|| Value::Object(Map::new()));
    if let Some(m) = reps.as_object_mut() {
        m.insert(src, Value::String(dst.clone()));
    }
    if also_hw {
        let hws = data
            .entry("hotwords".to_string())
            .or_insert_with(|| Value::Array(Vec::new()));
        if let Some(a) = hws.as_array_mut() {
            if !a.iter().any(|v| v.as_str() == Some(dst.as_str())) {
                a.push(Value::String(dst));
            }
        }
    }
    if let Err(e) = write_json_obj(&ctx.dict_path, &data) {
        return json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e }),
        );
    }
    ctx.reload_dict();
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_dict(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let mut data = read_json_obj(&ctx.dict_path);
    if let Some(obj) = body.get("replacements").and_then(|v| v.as_object()) {
        let mut clean = Map::new();
        for (k, v) in obj {
            let k = k.trim();
            if !k.is_empty() {
                clean.insert(
                    k.to_string(),
                    Value::String(v.as_str().unwrap_or("").to_string()),
                );
            }
        }
        data.insert("replacements".to_string(), Value::Object(clean));
    }
    if let Some(obj) = body.get("snippets").and_then(|v| v.as_object()) {
        let mut clean = Map::new();
        for (k, v) in obj {
            let k = k.trim();
            let vs = v.as_str().unwrap_or("");
            if !k.is_empty() && !vs.is_empty() {
                clean.insert(k.to_string(), Value::String(vs.to_string()));
            }
        }
        data.insert("snippets".to_string(), Value::Object(clean));
    }
    for key in ["hotwords", "fillers", "builtin_off"] {
        if let Some(arr) = body.get(key).and_then(|v| v.as_array()) {
            let clean: Vec<Value> = arr
                .iter()
                .filter_map(|v| v.as_str().map(str::trim))
                .filter(|s| !s.is_empty())
                .map(|s| Value::String(s.to_string()))
                .collect();
            data.insert(key.to_string(), Value::Array(clean));
        }
    }
    if let Err(e) = write_json_obj(&ctx.dict_path, &data) {
        return json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e }),
        );
    }
    ctx.reload_dict();
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_settings(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let cancel = body
        .get("cancel_key")
        .and_then(|v| v.as_str())
        .unwrap_or("esc")
        .trim()
        .to_string();
    let cancel = if cancel.is_empty() {
        "esc".to_string()
    } else {
        cancel
    };
    if let Err(e) = parse_binding(&cancel) {
        return json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e }));
    }
    let max_sec = body
        .get("max_duration_sec")
        .and_then(|v| v.as_f64())
        .unwrap_or(120.0);
    if !(5.0..=3600.0).contains(&max_sec) {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "最長の録音は 5〜3600 秒にしてください" }),
        );
    }
    let silence_sec = body
        .get("silence_stop_sec")
        .and_then(|v| v.as_f64())
        .unwrap_or(1.8);
    if !(0.3..=30.0).contains(&silence_sec) {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "無音自動確定は 0.3〜30 秒にしてください" }),
        );
    }
    let auto_cnt = body
        .get("auto_learn_after_count")
        .and_then(|v| v.as_i64())
        .unwrap_or(2);
    if !(0..=20).contains(&auto_cnt) {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "自動辞書登録の回数は 0〜20 の範囲にしてください" }),
        );
    }

    let urls: Vec<String> = match body.get("server_urls") {
        Some(Value::String(s)) => s
            .lines()
            .map(|l| l.trim().to_string())
            .filter(|l| !l.is_empty())
            .collect(),
        Some(Value::Array(arr)) => arr
            .iter()
            .filter_map(|v| v.as_str().map(|s| s.trim().to_string()))
            .filter(|s| !s.is_empty())
            .collect(),
        _ => Vec::new(),
    };
    for u in &urls {
        if !(u.starts_with("http://") || u.starts_with("https://")) {
            return json_no_store(
                StatusCode::BAD_REQUEST,
                json!({ "ok": false, "error": format!("サーバーの書き方が正しくありません: {u}") }),
            );
        }
    }

    let llm_url = body
        .get("llm_base_url")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if !llm_url.is_empty() && !(llm_url.starts_with("http://") || llm_url.starts_with("https://")) {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": format!("AI のアドレスの書き方が正しくありません（http:// から）: {llm_url}") }),
        );
    }
    let llm_provider = body
        .get("llm_provider")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    if !llm_provider.is_empty()
        && !matches!(
            llm_provider.as_str(),
            "chatgpt" | "openai" | "anthropic" | "gemini" | "ollama"
        )
    {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "AI の選び方が正しくありません" }),
        );
    }

    let mut cfg = ClientConfig::load(&ctx.config_path);
    cfg.hotkey_mode = if body
        .get("hotkey_mode")
        .and_then(|v| v.as_str())
        .unwrap_or("toggle")
        .eq_ignore_ascii_case("hold")
    {
        "hold".into()
    } else {
        "toggle".into()
    };
    cfg.auto_stop_on_silence = body
        .get("auto_stop_on_silence")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.silence_stop_sec = silence_sec;
    cfg.cancel_key = cancel;
    cfg.max_duration_sec = max_sec;
    let mic = body
        .get("microphone")
        .and_then(|v| v.as_str())
        .unwrap_or(DEFAULT_MIC_LABEL);
    cfg.microphone = if mic == DEFAULT_MIC_LABEL || mic == "Windows の既定のマイク" {
        None
    } else {
        Some(Value::String(mic.to_string()))
    };
    cfg.remove_fillers = body
        .get("remove_fillers")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.normalize_text = body
        .get("normalize_text")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.voice_commands = body
        .get("voice_commands")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.undo_voice_command = body
        .get("undo_voice_command")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.keep_mic_open = body
        .get("keep_mic_open")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.mute_other_audio = body
        .get("mute_other_audio")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.recording_indicator =
        if body.get("recording_indicator").and_then(|v| v.as_str()) == Some("text") {
            "text".into()
        } else {
            "pill".into()
        };
    cfg.overlay_place = if body.get("overlay_place").and_then(|v| v.as_str()) == Some("caret") {
        "caret".into()
    } else {
        "bottom".into()
    };
    if body.get("overlay_pinned").and_then(|v| v.as_bool()) == Some(false) {
        cfg.overlay_position = None;
    }
    cfg.live_segmentation = body
        .get("live_segmentation")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.strip_trailing_period = body
        .get("strip_trailing_period")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.app_specific_rules = body
        .get("app_specific_rules")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.learn_from_edits = body
        .get("learn_from_edits")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.auto_learn_after_count = auto_cnt as u32;
    cfg.restore_clipboard = body
        .get("restore_clipboard")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.beep_enabled = body
        .get("beep_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.show_overlay = body
        .get("show_overlay")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    cfg.server_urls = urls.clone();
    if let Some(names) = body.get("server_names").and_then(|v| v.as_object()) {
        for (k, v) in names {
            if urls.contains(k) {
                if let Some(s) = v.as_str() {
                    if !s.trim().is_empty() {
                        cfg.server_names
                            .insert(k.clone(), s.chars().take(64).collect());
                    }
                }
            }
        }
        cfg.server_names.retain(|k, _| urls.contains(k));
    }
    if let Some(want) = body.get("preferred_server").and_then(|v| v.as_str()) {
        let w = want.trim().to_string();
        cfg.preferred_server = if urls.contains(&w) { w } else { String::new() };
    } else if !urls.contains(&cfg.preferred_server) {
        cfg.preferred_server.clear();
    }
    cfg.recognition_place =
        if body.get("recognition_place").and_then(|v| v.as_str()) == Some("server") {
            "server".into()
        } else {
            "local".into()
        };
    if cfg.recognition_place == "server" && urls.is_empty() {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "使うパソコンが選ばれていません" }),
        );
    }
    cfg.local_fallback = body
        .get("local_fallback")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);

    if body.get("dict_share").is_some() {
        let share = body
            .get("dict_share")
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let share = if matches!(share, "server" | "folder") {
            share.to_string()
        } else {
            String::new()
        };
        let folder = body
            .get("dict_share_folder")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .trim()
            .trim_matches('"')
            .to_string();
        let name = body
            .get("dict_share_name")
            .and_then(|v| v.as_str())
            .unwrap_or("shared")
            .trim()
            .to_string();
        let name = if name.is_empty() {
            "shared".to_string()
        } else {
            name
        };
        if !valid_name(&name) {
            return json_no_store(
                StatusCode::BAD_REQUEST,
                json!({ "ok": false, "error": "辞書の名前は、英数字と - _ で 32 文字までにしてください" }),
            );
        }
        cfg.dict_share = share;
        cfg.dict_share_folder = folder;
        cfg.dict_share_name = name;
    }

    let prev_llm_url = cfg.llm_base_url.trim().trim_end_matches('/').to_string();
    let next_llm_url = llm_url.trim().trim_end_matches('/');
    let llm_url_changed = prev_llm_url != next_llm_url;
    cfg.llm_provider = llm_provider.clone();
    cfg.llm_auto_refine = body
        .get("llm_auto_refine")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    cfg.llm_base_url = llm_url;
    if llm_url_changed {
        cfg.llm_api_keys.remove("ollama");
    }
    cfg.llm_model = body
        .get("llm_model")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();

    if !llm_provider.is_empty() {
        if let Some(new_key) = body
            .get("llm_api_key")
            .and_then(|v| v.as_str())
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            match secret_store::protect_for(&llm_provider, new_key) {
                Ok(enc) => {
                    cfg.llm_api_keys.insert(llm_provider.clone(), enc);
                }
                Err(e) => {
                    return json_no_store(
                        StatusCode::INTERNAL_SERVER_ERROR,
                        json!({ "ok": false, "error": e }),
                    );
                }
            }
        }
        if body
            .get("llm_api_key_clear")
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            cfg.llm_api_keys.remove(&llm_provider);
        }
    }

    if let Some(want_startup) = body.get("startup_enabled").and_then(|v| v.as_bool()) {
        if let Ok(exe) = std::env::current_exe() {
            if let Err(e) = startup::set_enabled(want_startup, &exe, "OnseiMan") {
                return json_no_store(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({ "ok": false, "error": e }),
                );
            }
        }
    }

    if let Err(e) = cfg.save(&ctx.config_path) {
        return json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        );
    }
    if let Ok(mut guard) = ctx.config.lock() {
        *guard = cfg;
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

fn refiner_from_body(ctx: &AppContext, body: &Value) -> Result<LlmRefiner, String> {
    let cfg = ClientConfig::load(&ctx.config_path);
    let provider = body
        .get("provider")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let inline_key = body
        .get("api_key")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let model = body.get("model").and_then(|v| v.as_str()).unwrap_or("");
    let base_url = body.get("base_url").and_then(|v| v.as_str()).unwrap_or("");
    let key = if !inline_key.is_empty() {
        inline_key
    } else if provider == "ollama"
        && base_url.trim().trim_end_matches('/') != cfg.llm_base_url.trim().trim_end_matches('/')
    {
        // 保存済みの Ollama/独自互換 API キーは、保存済みの URL と一致するときだけ送る
        String::new()
    } else {
        let stored = cfg.llm_api_keys.get(&provider).cloned().unwrap_or_default();
        secret_store::unprotect(&stored)
    };
    LlmRefiner::new(&provider, model, &key, base_url, 20.0)
}

async fn get_chatgpt_status(State(ctx): State<Arc<AppContext>>) -> Response {
    let st = chatgpt_auth::status(&ctx.root_dir);
    let signing_in = *ctx.chatgpt_signing_in.lock().unwrap();
    let error = ctx.chatgpt_error.lock().unwrap().clone();
    json_no_store(
        StatusCode::OK,
        json!({
            "signed_in": st.signed_in,
            "email": st.email,
            "plan_usage": st.plan_usage,
            "signing_in": signing_in,
            "error": error,
        }),
    )
}

async fn post_chatgpt_signin(State(ctx): State<Arc<AppContext>>) -> Response {
    {
        let mut flag = ctx.chatgpt_signing_in.lock().unwrap();
        if *flag {
            return json_no_store(StatusCode::OK, json!({ "ok": true }));
        }
        *flag = true;
    }
    if let Ok(mut err) = ctx.chatgpt_error.lock() {
        err.clear();
    }
    let ctx_bg = Arc::clone(&ctx);
    std::thread::spawn(move || {
        let res = chatgpt_auth::sign_in(&ctx_bg.root_dir);
        if let Err(e) = res {
            if let Ok(mut err) = ctx_bg.chatgpt_error.lock() {
                *err = e;
            }
        }
        if let Ok(mut flag) = ctx_bg.chatgpt_signing_in.lock() {
            *flag = false;
        }
    });
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_chatgpt_signout(State(ctx): State<Arc<AppContext>>) -> Response {
    let root = ctx.root_dir.clone();
    match tokio::task::spawn_blocking(move || chatgpt_auth::sign_out(&root)).await {
        Ok(Ok(())) => {
            if let Ok(mut err) = ctx.chatgpt_error.lock() {
                err.clear();
            }
            json_no_store(StatusCode::OK, json!({ "ok": true }))
        }
        Ok(Err(e)) => json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e })),
        Err(e) => json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        ),
    }
}

async fn post_llm_test(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let provider = body.get("provider").and_then(|v| v.as_str()).unwrap_or("");
    if provider == "chatgpt" {
        let root = ctx.root_dir.clone();
        let model = body
            .get("model")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        return match tokio::task::spawn_blocking(move || {
            chatgpt_auth::refine_dictation(&root, &model, TEST_TEXT)
        })
        .await
        {
            Ok(Ok(res)) if !res.text.is_empty() => json_no_store(
                StatusCode::OK,
                json!({
                    "ok": true,
                    "before": TEST_TEXT,
                    "text": res.text,
                    "model": res.model,
                    "ms": res.elapsed_ms.round() as i64,
                }),
            ),
            Ok(Ok(_)) => json_no_store(
                StatusCode::BAD_REQUEST,
                json!({ "ok": false, "error": "ChatGPT から返事がありませんでした" }),
            ),
            Ok(Err(e)) => {
                json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e }))
            }
            Err(e) => json_no_store(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "ok": false, "error": e.to_string() }),
            ),
        };
    }
    let refiner = match refiner_from_body(&ctx, &body) {
        Ok(r) => r,
        Err(e) => {
            return json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e }))
        }
    };
    match refiner.refine_dictation(TEST_TEXT) {
        Ok(res) if !res.text.is_empty() => json_no_store(
            StatusCode::OK,
            json!({
                "ok": true,
                "before": TEST_TEXT,
                "text": res.text,
                "model": res.model,
                "ms": res.elapsed_ms.round() as i64,
            }),
        ),
        Ok(_) => json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "AI から返事がありませんでした（モデル名を確かめてください）" }),
        ),
        Err(e) => json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e })),
    }
}

async fn post_llm_models(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let provider = body.get("provider").and_then(|v| v.as_str()).unwrap_or("");
    if provider == "chatgpt" {
        let root = ctx.root_dir.clone();
        return match tokio::task::spawn_blocking(move || chatgpt_auth::list_models(&root)).await {
            Ok(Ok(models)) => {
                json_no_store(StatusCode::OK, json!({ "ok": true, "models": models }))
            }
            Ok(Err(e)) => {
                json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e }))
            }
            Err(e) => json_no_store(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({ "ok": false, "error": e.to_string() }),
            ),
        };
    }
    let refiner = match refiner_from_body(&ctx, &body) {
        Ok(r) => r,
        Err(e) => {
            return json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e }))
        }
    };
    let models = if refiner.model.is_empty() {
        Vec::new()
    } else {
        vec![refiner.model]
    };
    json_no_store(StatusCode::OK, json!({ "ok": true, "models": models }))
}

async fn post_find_servers(State(ctx): State<Arc<AppContext>>) -> Response {
    let urls = ctx.config.lock().unwrap().server_urls.clone();
    let servers = tokio::task::spawn_blocking(move || find_servers(&urls))
        .await
        .unwrap_or_default();
    json_no_store(StatusCode::OK, json!({ "servers": servers }))
}

fn retranscribe_audio_file(ctx: &Arc<AppContext>, audio_name: &str) -> Result<String, String> {
    let path = resolve_safe_wav_path(&ctx.audio_dir, audio_name)
        .map_err(|_| "音声ファイル名が不正、または見つかりませんでした".to_string())?;
    let wav = std::fs::read(&path).map_err(|_| "音声ファイルが見つかりませんでした".to_string())?;
    let cfg = ctx.config.lock().unwrap().clone();
    let dict = ctx.dict.lock().unwrap().clone();
    let hotwords = dict.active_hotwords();
    let readings = dict.readings();
    let res = ctx
        .transcriber
        .transcribe_wav(&cfg, &wav, &hotwords, &readings)?;
    let formatted = format_utterance_with_root(&res.text, &cfg, &dict, false, Some(&ctx.root_dir));
    if !formatted.text.is_empty() {
        let _ = clipboard::set_text(&formatted.text);
        let dur_sec = (wav.len().saturating_sub(44) as f64) / (16_000.0 * 2.0);
        if let Ok(mut h) = ctx.history.lock() {
            h.record_transcription(
                &res.backend,
                res.elapsed_ms,
                dur_sec,
                &res.text,
                &formatted.text,
                audio_name,
                "",
            );
        }
    }
    Ok(formatted.text)
}

async fn post_retranscribe(State(ctx): State<Arc<AppContext>>) -> Response {
    let latest_audio = {
        let h = ctx.history.lock().unwrap();
        h.get_history()
            .into_iter()
            .find(|e| !e.audio.is_empty())
            .map(|e| e.audio)
    };
    let Some(name) = latest_audio else {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "再認識できる録音がありません" }),
        );
    };
    let ctx_bg = ctx.clone();
    match tokio::task::spawn_blocking(move || retranscribe_audio_file(&ctx_bg, &name)).await {
        Ok(Ok(text)) => json_no_store(StatusCode::OK, json!({ "ok": true, "text": text })),
        Ok(Err(e)) => json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e })),
        Err(e) => json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        ),
    }
}

async fn post_retry(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let name = body
        .get("audio")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let ctx_bg = ctx.clone();
    match tokio::task::spawn_blocking(move || retranscribe_audio_file(&ctx_bg, &name)).await {
        Ok(Ok(text)) => json_no_store(StatusCode::OK, json!({ "ok": true, "text": text })),
        Ok(Err(e)) => json_no_store(StatusCode::BAD_REQUEST, json!({ "ok": false, "error": e })),
        Err(e) => json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        ),
    }
}

/// 自動学習で昇格した `(src, dst)` を `dict.json` の `replacements` / `hotwords` に登録して再読込する。
pub fn add_replacement_to_dict(ctx: &AppContext, src: &str, dst: &str) {
    if src.is_empty() || dst.is_empty() {
        return;
    }
    let mut data = read_json_obj(&ctx.dict_path);
    let rep = data
        .entry("replacements".to_string())
        .or_insert_with(|| Value::Object(serde_json::Map::new()));
    if let Some(m) = rep.as_object_mut() {
        m.insert(src.to_string(), Value::String(dst.to_string()));
    }
    let hw = data
        .entry("hotwords".to_string())
        .or_insert_with(|| Value::Array(Vec::new()));
    if let Some(arr) = hw.as_array_mut() {
        if !arr.iter().any(|v| v.as_str() == Some(dst)) {
            arr.push(Value::String(dst.to_string()));
        }
    }
    if write_json_obj(&ctx.dict_path, &data).is_ok() {
        ctx.reload_dict();
    }
}

pub fn retranscribe_latest(ctx: &Arc<AppContext>) -> Result<String, String> {
    let latest_audio = {
        let h = ctx.history.lock().unwrap();
        h.get_history()
            .into_iter()
            .find(|e| !e.audio.is_empty())
            .map(|e| e.audio)
    };
    let Some(name) = latest_audio else {
        return Err("再認識できる録音がありません".into());
    };
    retranscribe_audio_file(ctx, &name)
}

pub fn sync_hotkey_from_config(cfg: &ClientConfig) {
    if let Ok(primary) = parse_binding(&cfg.hotkey) {
        let extra = if cfg.hotkey_extra.trim().is_empty() {
            None
        } else {
            parse_binding(&cfg.hotkey_extra).ok()
        };
        let ai = if cfg.ai_hotkey.trim().is_empty() {
            None
        } else {
            parse_binding(&cfg.ai_hotkey).ok()
        };
        let cancel = if cfg.cancel_key.trim().is_empty() {
            None
        } else {
            parse_binding(&cfg.cancel_key).ok()
        };
        GlobalHotkey::update_config(HotkeyConfig {
            primary,
            extra,
            ai,
            cancel,
            suppress: cfg.suppress_hotkey,
        });
    }
}

async fn post_learn_manual(
    State(ctx): State<Arc<AppContext>>,
    Json(body): Json<Value>,
) -> Response {
    let before = body.get("before").and_then(|v| v.as_str()).unwrap_or("");
    let after = body.get("after").and_then(|v| v.as_str()).unwrap_or("");
    let added = ctx.candidates.lock().unwrap().record_edit(before, after);
    json_no_store(StatusCode::OK, json!({ "ok": true, "added": added }))
}

async fn post_candidates_approve(
    State(ctx): State<Arc<AppContext>>,
    Json(body): Json<Value>,
) -> Response {
    let src = body
        .get("src")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let dst = body
        .get("dst")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if !src.is_empty() && !dst.is_empty() {
        let mut data = read_json_obj(&ctx.dict_path);
        let reps = data
            .entry("replacements".to_string())
            .or_insert_with(|| Value::Object(Map::new()));
        if let Some(m) = reps.as_object_mut() {
            m.insert(src.clone(), Value::String(dst.clone()));
        }
        let hws = data
            .entry("hotwords".to_string())
            .or_insert_with(|| Value::Array(Vec::new()));
        if let Some(a) = hws.as_array_mut() {
            if !a.iter().any(|v| v.as_str() == Some(dst.as_str())) {
                a.push(Value::String(dst.clone()));
            }
        }
        let _ = write_json_obj(&ctx.dict_path, &data);
        ctx.reload_dict();
        ctx.candidates.lock().unwrap().approve(&src, &dst);
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_candidates_reject(
    State(ctx): State<Arc<AppContext>>,
    Json(body): Json<Value>,
) -> Response {
    let src = body
        .get("src")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let dst = body
        .get("dst")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    if !src.is_empty() && !dst.is_empty() {
        ctx.candidates.lock().unwrap().reject(src, dst);
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_candidates_restore(
    State(ctx): State<Arc<AppContext>>,
    Json(body): Json<Value>,
) -> Response {
    let src = body
        .get("src")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let dst = body
        .get("dst")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    if !src.is_empty() && !dst.is_empty() {
        let item = LearningCandidate {
            src,
            dst,
            count: body.get("count").and_then(|v| v.as_u64()).unwrap_or(1) as u32,
            example_before: body
                .get("example_before")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            example_after: body
                .get("example_after")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        };
        ctx.candidates.lock().unwrap().restore(item);
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

fn start_capture_for(ctx: Arc<AppContext>, field: &'static str) {
    GlobalHotkey::capture_next(move |captured| {
        let Some(key) = captured else {
            return;
        };
        if parse_binding(&key).is_err() {
            return;
        }
        let mut cfg = ClientConfig::load(&ctx.config_path);
        match field {
            "hotkey" => cfg.hotkey = key,
            "hotkey_extra" => cfg.hotkey_extra = key,
            "ai_hotkey" => cfg.ai_hotkey = key,
            _ => return,
        }
        if cfg.save(&ctx.config_path).is_ok() {
            sync_hotkey_from_config(&cfg);
            if let Ok(mut g) = ctx.config.lock() {
                *g = cfg;
            }
        }
    });
}

async fn post_capture_hotkey(State(ctx): State<Arc<AppContext>>) -> Response {
    start_capture_for(ctx, "hotkey");
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_capture_extra_hotkey(State(ctx): State<Arc<AppContext>>) -> Response {
    start_capture_for(ctx, "hotkey_extra");
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_clear_extra_hotkey(State(ctx): State<Arc<AppContext>>) -> Response {
    let mut cfg = ClientConfig::load(&ctx.config_path);
    cfg.hotkey_extra.clear();
    let _ = cfg.save(&ctx.config_path);
    sync_hotkey_from_config(&cfg);
    if let Ok(mut g) = ctx.config.lock() {
        *g = cfg;
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_capture_ai_hotkey(State(ctx): State<Arc<AppContext>>) -> Response {
    start_capture_for(ctx, "ai_hotkey");
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

async fn post_clear_ai_hotkey(State(ctx): State<Arc<AppContext>>) -> Response {
    let mut cfg = ClientConfig::load(&ctx.config_path);
    cfg.ai_hotkey.clear();
    let _ = cfg.save(&ctx.config_path);
    sync_hotkey_from_config(&cfg);
    if let Ok(mut g) = ctx.config.lock() {
        *g = cfg;
    }
    json_no_store(StatusCode::OK, json!({ "ok": true }))
}

fn detect_sync_folders() -> Vec<Value> {
    let mut out = Vec::new();
    let home = std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .unwrap_or_default();
    if home.is_empty() {
        return out;
    }
    let h = PathBuf::from(&home);
    let cands: [(&str, PathBuf); 4] = [
        ("OneDrive", h.join("OneDrive")),
        ("Dropbox", h.join("Dropbox")),
        ("Google Drive", h.join("Google Drive")),
        ("iCloud Drive", h.join("iCloudDrive")),
    ];
    for (label, p) in cands {
        if p.is_dir() {
            out.push(json!({ "label": label, "path": p.to_string_lossy() }));
        }
    }
    out
}

fn dict_sync_base_path(ctx: &AppContext) -> PathBuf {
    ctx.root_dir.join("logs").join("dict_sync_base.json")
}

fn run_dict_sync_blocking(ctx: &AppContext) -> Result<&'static str, String> {
    use onseiman_core::dict_format::clean_data;
    use onseiman_core::dict_sync::merge3;

    let cfg = ctx.config.lock().unwrap().clone();
    if cfg.dict_share.is_empty() {
        return Ok("off");
    }
    let name = if valid_name(&cfg.dict_share_name) {
        cfg.dict_share_name.clone()
    } else {
        "shared".to_string()
    };
    let local_val = Value::Object(read_json_obj(&ctx.dict_path));
    let local_clean = clean_data(&local_val, None);

    let base_path = dict_sync_base_path(ctx);
    let base_val = std::fs::read_to_string(&base_path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok());
    let base_target_key = format!("{}:{}:{}", cfg.dict_share, cfg.dict_share_folder, name);
    let base_map = base_val.as_ref().and_then(|v| {
        if v.get("target").and_then(Value::as_str) == Some(base_target_key.as_str()) {
            v.get("data").and_then(Value::as_object).cloned()
        } else {
            None
        }
    });

    let merged = match cfg.dict_share.as_str() {
        "server" => {
            let base_url = if !cfg.preferred_server.is_empty() {
                cfg.preferred_server.clone()
            } else {
                cfg.server_urls
                    .first()
                    .cloned()
                    .ok_or_else(|| "認識役のパソコンが登録されていません".to_string())?
            };
            let url = format!("{}/dict/{name}", base_url.trim_end_matches('/'));
            let agent = ureq::AgentBuilder::new()
                .timeout(std::time::Duration::from_secs(10))
                .redirects(0)
                .build();
            let get_resp: Value = agent
                .get(&url)
                .call()
                .map_err(|e| format!("認識役につながりませんでした: {e}"))?
                .into_json()
                .map_err(|e| format!("辞書の読み取りに失敗しました: {e}"))?;
            let remote_ver = get_resp.get("version").and_then(Value::as_i64).unwrap_or(0);
            let remote_data = get_resp
                .get("data")
                .map(|v| clean_data(v, None))
                .unwrap_or_default();
            let merged = merge3(base_map.as_ref(), &local_clean, &remote_data);
            let merged_clean = clean_data(&Value::Object(merged), None);
            agent
                .put(&url)
                .send_json(json!({ "version": remote_ver, "data": merged_clean }))
                .map_err(|e| format!("認識役への保存に失敗しました: {e}"))?;
            merged_clean
        }
        "folder" => {
            let folder = cfg.dict_share_folder.trim();
            if folder.is_empty() {
                return Err("共有フォルダが指定されていません".into());
            }
            let dir = PathBuf::from(folder);
            if !dir.is_dir() {
                return Err("共有フォルダが見つかりません".into());
            }
            let file_path = dir.join(format!("{name}.json"));
            let remote_data = if file_path.is_file() {
                let raw = std::fs::read_to_string(&file_path)
                    .map_err(|e| format!("共有辞書を読めません: {e}"))?;
                let v: Value = serde_json::from_str(&raw).unwrap_or_else(|_| json!({}));
                clean_data(&v, None)
            } else {
                Map::new()
            };
            let merged = merge3(base_map.as_ref(), &local_clean, &remote_data);
            let merged_clean = clean_data(&Value::Object(merged), None);
            write_json_obj(&file_path, &merged_clean)
                .map_err(|e| format!("共有辞書を書き込めません: {e}"))?;
            merged_clean
        }
        _ => return Ok("off"),
    };

    write_json_obj(&ctx.dict_path, &merged)
        .map_err(|e| format!("ローカル辞書を保存できません: {e}"))?;
    ctx.reload_dict();
    let _ = std::fs::create_dir_all(ctx.root_dir.join("logs"));
    let snap = json!({ "target": base_target_key, "data": merged });
    if let Ok(s) = serde_json::to_string_pretty(&snap) {
        let _ = std::fs::write(&base_path, s + "\n");
    }
    Ok("ok")
}

async fn post_dict_sync_now(State(ctx): State<Arc<AppContext>>) -> Response {
    let ctx_job = ctx.clone();
    let res = tokio::task::spawn_blocking(move || run_dict_sync_blocking(&ctx_job)).await;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    match res {
        Ok(Ok(tag)) => {
            let st = if tag == "off" {
                json!({})
            } else {
                json!({ "last_ok": now, "last_error": "" })
            };
            if let Ok(mut g) = ctx.dict_sync_status.lock() {
                *g = st.clone();
            }
            json_no_store(
                StatusCode::OK,
                json!({ "ok": true, "result": tag, "status": st, "folders": detect_sync_folders() }),
            )
        }
        Ok(Err(e)) => {
            let st = json!({ "last_error": e });
            if let Ok(mut g) = ctx.dict_sync_status.lock() {
                *g = st.clone();
            }
            json_no_store(
                StatusCode::BAD_REQUEST,
                json!({ "ok": false, "error": e, "status": st, "folders": detect_sync_folders() }),
            )
        }
        Err(e) => json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        ),
    }
}

fn parse_semver(s: &str) -> Vec<u32> {
    s.trim()
        .trim_start_matches(['v', 'V'])
        .split('.')
        .filter_map(|p| p.parse::<u32>().ok())
        .collect()
}

fn check_github_release(is_dev: bool) -> Result<Option<Value>, String> {
    let current = env!("CARGO_PKG_VERSION");
    let agent = ureq::AgentBuilder::new()
        .timeout(std::time::Duration::from_secs(10))
        .build();
    let resp = agent
        .get("https://api.github.com/repos/ogikosan/OnseiMan/releases/latest")
        .set("User-Agent", &format!("OnseiMan/{current}"))
        .set("Accept", "application/vnd.github+json")
        .call()
        .map_err(|e| format!("更新の確認に失敗しました: {e}"))?;
    let data: Value = resp
        .into_json()
        .map_err(|e| format!("更新情報の解析に失敗しました: {e}"))?;
    let tag = data
        .get("tag_name")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let latest = tag.trim_start_matches(['v', 'V']);
    if latest.is_empty() || parse_semver(latest) <= parse_semver(current) {
        return Ok(None);
    }
    let notes = data
        .get("body")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .chars()
        .take(4000)
        .collect::<String>();
    Ok(Some(json!({
        "current": current,
        "version": latest,
        "notes": notes,
        "dev": is_dev,
        "can_run": !is_dev && cfg!(windows),
    })))
}

async fn post_update_check(State(ctx): State<Arc<AppContext>>) -> Response {
    let is_dev = ctx.root_dir.join(".git").exists();
    match tokio::task::spawn_blocking(move || check_github_release(is_dev)).await {
        Ok(Ok(upd)) => {
            if let Ok(mut g) = ctx.latest_update.lock() {
                *g = upd.clone();
            }
            json_no_store(
                StatusCode::OK,
                json!({ "ok": true, "update": upd.unwrap_or(Value::Null) }),
            )
        }
        Ok(Err(e)) => json_no_store(StatusCode::BAD_GATEWAY, json!({ "ok": false, "error": e })),
        Err(e) => json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        ),
    }
}

async fn post_update_run(State(ctx): State<Arc<AppContext>>) -> Response {
    if ctx.root_dir.join(".git").exists() {
        return json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "開発フォルダでは自動更新できません" }),
        );
    }
    #[cfg(windows)]
    {
        let root = ctx.root_dir.clone();
        let pid = std::process::id().to_string();
        let script = "$root=$env:ONSEIMAN_ROOT; $exe=Join-Path $root 'onseiman.exe'; \
             $base='https://github.com/ogikosan/OnseiMan/releases/latest/download'; \
             [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; \
             Invoke-WebRequest -Uri \"$base/onseiman-windows-x64.exe\" -OutFile \"$exe.new\" -UseBasicParsing; \
             Invoke-WebRequest -Uri \"$base/SHA256SUMS-windows-x64.txt\" -OutFile \"$exe.sha256\" -UseBasicParsing; \
             $m=Select-String -Path \"$exe.sha256\" -Pattern '^([0-9a-fA-F]{64})\\s+\\*?onseiman-windows-x64\\.exe$' | Select-Object -First 1; \
             $exp=if ($m) { $m.Matches[0].Groups[1].Value.ToLower() } else { '' }; \
             $act=(Get-FileHash -LiteralPath \"$exe.new\" -Algorithm SHA256).Hash.ToLower(); \
             Remove-Item -LiteralPath \"$exe.sha256\" -Force -ErrorAction SilentlyContinue; \
             if ($exp -and $exp -eq $act) { \
               Stop-Process -Id ([int]$env:ONSEIMAN_PID) -Force -ErrorAction SilentlyContinue; \
               Start-Sleep -Milliseconds 600; \
               Move-Item -LiteralPath \"$exe.new\" -Destination $exe -Force; \
               Start-Process -FilePath $exe -WorkingDirectory $root; \
             } else { Remove-Item -LiteralPath \"$exe.new\" -Force -ErrorAction SilentlyContinue }";
        let sys_root = std::env::var("SystemRoot").unwrap_or_else(|_| r"C:\Windows".to_string());
        let ps_exe = PathBuf::from(sys_root)
            .join("System32")
            .join("WindowsPowerShell")
            .join("v1.0")
            .join("powershell.exe");
        let _ = Command::new(ps_exe)
            .env("ONSEIMAN_ROOT", &root)
            .env("ONSEIMAN_PID", &pid)
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-Command",
                script,
            ])
            .spawn();
        json_no_store(StatusCode::OK, json!({ "ok": true }))
    }
    #[cfg(not(windows))]
    {
        json_no_store(
            StatusCode::BAD_REQUEST,
            json!({ "ok": false, "error": "自動更新は Windows のみ対応しています" }),
        )
    }
}

async fn post_theme(State(ctx): State<Arc<AppContext>>, Json(body): Json<Value>) -> Response {
    let theme = match body.get("theme").and_then(|v| v.as_str()).unwrap_or("") {
        "dark" => "dark",
        "light" => "light",
        _ => "",
    };
    let mut cfg = ClientConfig::load(&ctx.config_path);
    cfg.ui_theme = theme.to_string();
    if let Err(e) = cfg.save(&ctx.config_path) {
        return json_no_store(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({ "ok": false, "error": e.to_string() }),
        );
    }
    if let Ok(mut g) = ctx.config.lock() {
        g.ui_theme = theme.to_string();
    }
    json_no_store(StatusCode::OK, json!({ "ok": true, "theme": theme }))
}

/// すでに OnseiMan 画面が開いていれば前面に出し、無ければブラウザで開く。
pub fn open_or_focus_browser(
    ctx: &AppContext,
    base_url: &str,
    tab: &str,
    profile_dir: Option<&FsPath>,
) {
    if onseiman_platform::window::bring_existing_app_window_to_front("OnseiMan") {
        if !tab.is_empty() {
            if let Ok(mut p) = ctx.pending_tab.lock() {
                *p = tab.to_string();
            }
        }
        return;
    }
    let target = if tab.is_empty() {
        base_url.to_string()
    } else {
        format!("{base_url}/#{tab}")
    };
    open_browser(&target, profile_dir);
}

/// 管理画面をブラウザで開く（Windows: Edge --app、macOS: `open`）。
pub fn open_browser(url: &str, profile_dir: Option<&FsPath>) {
    let valid_local = (url.starts_with("http://127.0.0.1:")
        || url.starts_with("http://localhost:"))
        && !url.chars().any(|c| {
            c.is_control()
                || c.is_whitespace()
                || matches!(
                    c,
                    '&' | '|' | '^' | '%' | '<' | '>' | '"' | '\'' | '`' | ';'
                )
        });
    if !valid_local {
        return;
    }
    #[cfg(windows)]
    {
        let candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ];
        if let Some(browser) = candidates.iter().find(|p| FsPath::new(p).is_file()) {
            let mut cmd = Command::new(browser);
            cmd.arg(format!("--app={url}"))
                .arg("--window-size=1040,740");
            if let Some(dir) = profile_dir {
                let _ = std::fs::create_dir_all(dir);
                cmd.arg(format!("--user-data-dir={}", dir.display())).args([
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-extensions",
                    "--disable-sync",
                    "--disable-background-networking",
                    "--disable-features=msImplicitSignin,msEdgeSidebarV2,EdgeCollections,msEdgeShopping",
                ]);
            }
            let _ = cmd.spawn();
            return;
        }
        let _ = Command::new("cmd").args(["/C", "start", "", url]).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = profile_dir;
        let _ = Command::new("open").arg(url).spawn();
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = (url, profile_dir);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tower::ServiceExt;

    #[tokio::test]
    async fn security_guards_block_bad_host_and_origin() {
        let dir = tempfile::tempdir().unwrap();
        let ctx = Arc::new(AppContext::new(dir.path().to_path_buf()));
        ctx.set_port(18765);
        let app = router(ctx);

        // 正常な GET /api/state
        let ok = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/state")
                    .header(header::HOST, "127.0.0.1:18765")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(ok.status(), StatusCode::OK);
        assert_eq!(ok.headers().get(header::X_FRAME_OPTIONS).unwrap(), "DENY");

        // DNS リバインディング拒否
        let bad_host = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/state")
                    .header(header::HOST, "evil.example:18765")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(bad_host.status(), StatusCode::FORBIDDEN);

        // 外部 Origin からの POST 拒否
        let bad_origin = app
            .clone()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/copy")
                    .header(header::HOST, "127.0.0.1:18765")
                    .header(header::ORIGIN, "https://evil.example")
                    .header(header::CONTENT_TYPE, "application/json")
                    .body(Body::from(r#"{"text":"hi"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(bad_origin.status(), StatusCode::FORBIDDEN);

        // form POST（非 JSON）拒否
        let bad_ctype = app
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/copy")
                    .header(header::HOST, "127.0.0.1:18765")
                    .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
                    .body(Body::from("text=hi"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(bad_ctype.status(), StatusCode::FORBIDDEN);
    }
}
