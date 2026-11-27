#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
//! OnseiMan クライアント起動入口（Rust 版）。

use std::fs::OpenOptions;
use std::io::Write;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use chrono::Local;

/// 録音開始・終了・キャンセル・エラーの効果音をバックグラウンドで鳴らす（旧 Python 版 `winsound.Beep` と同じ）。
fn play_beep(enabled: bool, freq: u32, duration_ms: u32) {
    if !enabled {
        return;
    }
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        extern "system" {
            fn Beep(dw_freq: u32, dw_duration: u32) -> i32;
        }
        thread::spawn(move || unsafe {
            let _ = Beep(freq, duration_ms);
        });
    }
    #[cfg(not(windows))]
    {
        let _ = (freq, duration_ms);
    }
}

fn append_log(root: &Path, msg: &str) {
    let dir = root.join("logs");
    let _ = std::fs::create_dir_all(&dir);
    if let Ok(mut f) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("onseiman.log"))
    {
        let ts = Local::now().format("%Y-%m-%d %H:%M:%S");
        let _ = writeln!(f, "{ts} {msg}");
    }
}

fn default_root() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            // cargo target/(debug|release) 直下で実行した開発時以外は exe と同じフォルダを基準にする
            let in_cargo_target = dir
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n == "debug" || n == "release")
                && dir
                    .parent()
                    .and_then(|p| p.file_name())
                    .and_then(|n| n.to_str())
                    == Some("target");
            if !in_cargo_target {
                return dir.to_path_buf();
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}
use onseiman_core::audio::{encode_wav, normalize_voice_dynamics};
use onseiman_core::segmenter::AdaptiveSegmenter;
use onseiman_core::voice_commands::is_undo_command;
use onseiman_platform::audio_mute::AudioMuteGuard;
use onseiman_platform::binding::parse_binding;
use onseiman_platform::hotkey::{
    heartbeat as hook_heartbeat, GlobalHotkey, HotkeyConfig, HotkeyEvent,
};
use onseiman_platform::overlay::{OverlayState, RecordingOverlay};
use onseiman_platform::paste::{
    capture_paste_target, paste_text, send_undo_shortcut, target_process_name,
};
use onseiman_platform::recorder::{
    list_input_devices, start_recording_ex, warm_mic, RecordingHandle,
};
use onseiman_platform::tray::{TrayEvent, TrayIcon};
use onseiman_platform::uia_watcher::EditCorrectionWatcher;
use onseiman_platform::watchdog::HookWatchdog;
use tokio::net::TcpListener;
use tokio::sync::Notify;

use onseiman_app::chatgpt_auth;
use onseiman_app::live_session::{LiveTranscription, SegmentRecognition};
use onseiman_app::pipeline::format_utterance_full;
use onseiman_app::web_ui::{
    open_browser, open_or_focus_browser, retranscribe_latest, router, sync_hotkey_from_config,
    AppContext,
};

struct Args {
    root: PathBuf,
    port: u16,
    open_ui: bool,
    check_only: bool,
}

fn parse_args() -> Args {
    let mut root = default_root();
    let mut port: u16 = 18765;
    let mut open_ui = true;
    let mut check_only = false;

    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--root" => {
                if let Some(p) = it.next() {
                    root = PathBuf::from(p);
                }
            }
            "--port" => {
                if let Some(p) = it.next() {
                    if let Ok(v) = p.parse::<u16>() {
                        port = v;
                    }
                }
            }
            "--no-browser" => open_ui = false,
            "--open" => open_ui = true,
            "--check" => check_only = true,
            "--version" | "-V" => {
                println!("onseiman {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            _ => {}
        }
    }

    Args {
        root,
        port,
        open_ui,
        check_only,
    }
}

#[tokio::main]
async fn main() {
    let args = parse_args();
    let ctx = Arc::new(AppContext::new(args.root.clone()));

    if args.check_only {
        let cfg = ctx.config.lock().unwrap().clone();
        let mics = list_input_devices();
        let chosen = ctx.transcriber.check_server_health(&cfg);
        println!("OnseiMan Rust v{}", env!("CARGO_PKG_VERSION"));
        println!("  root: {}", ctx.root_dir.display());
        println!("  hotkey: {} ({})", cfg.hotkey, cfg.hotkey_mode);
        println!("  mics: {}", mics.len());
        println!(
            "  backend: {} ({})",
            ctx.transcriber.last_backend_status(),
            chosen.as_deref().unwrap_or("none")
        );
        return;
    }

    // Web UI サーバーを 127.0.0.1:<port> で起動。すでに別プロセスが占有していれば二重起動を防ぐ
    let listener = match TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], args.port))).await {
        Ok(l) => l,
        Err(_) => {
            // すでに OnseiMan が起動中ならその管理画面を開いて終了（二重起動によるフック競合を防ぐ）
            let url = format!("http://127.0.0.1:{}", args.port);
            if args.open_ui {
                let profile = ctx.root_dir.join("logs").join("browser_profile");
                open_browser(&url, Some(&profile));
            }
            return;
        }
    };
    let actual_port = listener.local_addr().unwrap().port();
    ctx.set_port(actual_port);
    let url = format!("http://127.0.0.1:{actual_port}");
    append_log(
        &ctx.root_dir,
        &format!(
            "起動 v{} root={} url={url}",
            env!("CARGO_PKG_VERSION"),
            ctx.root_dir.display()
        ),
    );

    let shutdown = Arc::new(Notify::new());

    // システムトレイアイコン（左クリックで管理画面、右クリックで終了）
    let (tray_tx, tray_rx) = mpsc::channel();
    let tray = Arc::new(TrayIcon::start(
        &format!("OnseiMan v{}", env!("CARGO_PKG_VERSION")),
        tray_tx,
    ));
    append_log(&ctx.root_dir, &format!("tray_add={}", tray.is_added()));
    {
        let url_tray = url.clone();
        let profile = ctx.root_dir.join("logs").join("browser_profile");
        let shutdown_tray = Arc::clone(&shutdown);
        let ctx_tray = ctx.clone();
        let tray_for_menu = Arc::clone(&tray);
        thread::spawn(move || {
            let open_path = |p: &Path| {
                #[cfg(windows)]
                {
                    let _ = std::process::Command::new("explorer.exe").arg(p).spawn();
                }
                #[cfg(target_os = "macos")]
                {
                    let _ = std::process::Command::new("open").arg(p).spawn();
                }
            };
            while let Ok(ev) = tray_rx.recv() {
                match ev {
                    TrayEvent::OpenUi => {
                        open_or_focus_browser(&ctx_tray, &url_tray, "history", Some(&profile))
                    }
                    TrayEvent::OpenDict => {
                        open_or_focus_browser(&ctx_tray, &url_tray, "dict", Some(&profile))
                    }
                    TrayEvent::OpenSettings | TrayEvent::CheckUpdate => {
                        open_or_focus_browser(&ctx_tray, &url_tray, "settings", Some(&profile))
                    }
                    TrayEvent::OpenHelp => {
                        open_or_focus_browser(&ctx_tray, &url_tray, "help", Some(&profile))
                    }
                    TrayEvent::CopyLast => {
                        if let Ok(h) = ctx_tray.history.lock() {
                            if let Some(first) = h.get_history().into_iter().next() {
                                if !first.text.is_empty() {
                                    let _ = onseiman_platform::clipboard::set_text(&first.text);
                                }
                            }
                        }
                    }
                    TrayEvent::RetranscribeLast => {
                        let c = ctx_tray.clone();
                        thread::spawn(move || {
                            let _ = retranscribe_latest(&c);
                        });
                    }
                    TrayEvent::CaptureHotkey => {
                        let c = ctx_tray.clone();
                        let t = Arc::clone(&tray_for_menu);
                        GlobalHotkey::capture_next(move |captured| {
                            if let Some(name) = captured {
                                let mut cfg =
                                    onseiman_core::config::ClientConfig::load(&c.config_path);
                                cfg.hotkey = name;
                                let _ = cfg.save(&c.config_path);
                                sync_hotkey_from_config(&cfg);
                                if let Ok(mut g) = c.config.lock() {
                                    *g = cfg;
                                }
                                restore_idle_tray(&c, &t);
                            }
                        });
                    }
                    TrayEvent::CaptureAiHotkey => {
                        let c = ctx_tray.clone();
                        GlobalHotkey::capture_next(move |captured| {
                            if let Some(name) = captured {
                                let mut cfg =
                                    onseiman_core::config::ClientConfig::load(&c.config_path);
                                cfg.ai_hotkey = name;
                                let _ = cfg.save(&c.config_path);
                                sync_hotkey_from_config(&cfg);
                                if let Ok(mut g) = c.config.lock() {
                                    *g = cfg;
                                }
                            }
                        });
                    }
                    TrayEvent::OpenLog => {
                        open_path(&ctx_tray.root_dir.join("logs").join("onseiman.log"));
                    }
                    TrayEvent::OpenHistoryTxt => {
                        open_path(&ctx_tray.root_dir.join("logs").join("history.txt"));
                    }
                    TrayEvent::Quit => {
                        shutdown_tray.notify_one();
                        break;
                    }
                }
            }
        });
    }

    // 非アクティブ・最前面カプセルオーバーレイ
    let overlay = Arc::new(RecordingOverlay::start());

    // バックグラウンドでサーバーの生死確認＋トレイ色を状態に合わせる＋マイク待機（keep_mic_open）
    {
        let ctx_health = ctx.clone();
        let tray_health = Arc::clone(&tray);
        thread::spawn(move || {
            let cfg = ctx_health.config.lock().unwrap().clone();
            if cfg.keep_mic_open {
                let mic = cfg.microphone.as_ref().and_then(|v| v.as_str());
                match warm_mic(mic) {
                    Ok(()) => append_log(
                        &ctx_health.root_dir,
                        "マイクを待機状態で開きました（押す直前 0.4 秒の声も録れます）",
                    ),
                    Err(e) => append_log(&ctx_health.root_dir, &format!("マイク待機失敗: {e}")),
                }
            }
            let _ = ctx_health.transcriber.check_server_health(&cfg);
            restore_idle_tray(&ctx_health, &tray_health);
        });
    }

    // グローバルホットキー＋録音・認識・貼り付けループ
    let _hotkey_guard = start_hotkey_loop(ctx.clone(), Arc::clone(&overlay), Arc::clone(&tray));

    // フック詰まり見張り（2.5 秒以上応答が止まったら修飾キーを解放して記録）
    let root_wd = ctx.root_dir.clone();
    let _watchdog_guard = HookWatchdog::start(hook_heartbeat(), move |stale_ms| {
        append_log(
            &root_wd,
            &format!("フック応答遅延を検知 ({stale_ms}ms) → 修飾キーを解放しました"),
        );
    });

    if args.open_ui {
        let url_open = url.clone();
        let profile = ctx.root_dir.join("logs").join("browser_profile");
        let ctx_open = ctx.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(150));
            open_or_focus_browser(&ctx_open, &url_open, "history", Some(&profile));
        });
    }

    let app = router(ctx);
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            tokio::select! {
                _ = tokio::signal::ctrl_c() => {}
                _ = shutdown.notified() => {}
            }
        })
        .await
        .expect("Web UI サーバーでエラーが起きました");
}

struct ActiveSession {
    handle: RecordingHandle,
    _mute_guard: Option<AudioMuteGuard>,
    target: isize,
    target_app: Option<String>,
    use_ai: bool,
    silence_triggered: Arc<AtomicBool>,
    live: Option<LiveTranscription>,
    adaptive_seg: Option<AdaptiveSegmenter>,
    submitted_samples: Vec<f32>,
}

fn restore_idle_tray(ctx: &AppContext, tray: &TrayIcon) {
    let status = ctx.transcriber.last_backend_status();
    let rgb = if status.starts_with("LAN") {
        (0x16, 0xa3, 0x4a) // 緑（LAN 待機）
    } else if status.starts_with("Local") {
        (0xca, 0x8a, 0x04) // 黄（ローカル待機）
    } else {
        (0x6b, 0x72, 0x80) // 灰（未接続）
    };
    let hk = ctx
        .config
        .lock()
        .map(|c| c.hotkey.clone())
        .unwrap_or_else(|_| "ralt".into());
    tray.set_state(rgb, &format!("OnseiMan | 待機中（{status}）| キー: {hk}"));
}

fn start_hotkey_loop(
    ctx: Arc<AppContext>,
    overlay: Arc<RecordingOverlay>,
    tray: Arc<TrayIcon>,
) -> Option<GlobalHotkey> {
    let cfg = ctx.config.lock().unwrap().clone();
    let primary = parse_binding(&cfg.hotkey).ok()?;
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

    let (tx, rx) = mpsc::channel();
    let tx_auto = tx.clone();
    let hk = match GlobalHotkey::start(
        HotkeyConfig {
            primary,
            extra,
            ai,
            cancel,
            suppress: cfg.suppress_hotkey,
        },
        tx,
    ) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("ホットキー監視を開始できませんでした: {e}");
            return None;
        }
    };

    thread::spawn(move || {
        use std::sync::atomic::AtomicU64;

        let mut active: Option<ActiveSession> = None;
        let uia_watcher = Arc::new(EditCorrectionWatcher::new());
        let overlay_gen = Arc::new(AtomicU64::new(0));

        let start_rec = |ctx: &Arc<AppContext>,
                         ov: &Arc<RecordingOverlay>,
                         slot: &mut Option<ActiveSession>,
                         use_ai: bool| {
            uia_watcher.cancel();
            overlay_gen.fetch_add(1, Ordering::SeqCst);
            let cfg = ctx.config.lock().unwrap().clone();
            let target = capture_paste_target();
            let mute_guard = if cfg.mute_other_audio {
                Some(AudioMuteGuard::mute_others())
            } else {
                None
            };
            let mic_name = cfg.microphone.as_ref().and_then(|v| v.as_str());
            let show_ov = cfg.show_overlay;
            let overlay_place = cfg.overlay_place.clone();
            let auto_stop = cfg.auto_stop_on_silence;
            let silence_sec = cfg.silence_stop_sec.max(0.3);
            let silence_rms = cfg.silence_rms_threshold as f32;
            let live_text = Arc::new(Mutex::new(String::new()));
            let live_text_cb = Arc::clone(&live_text);
            let ov_cb = Arc::clone(ov);
            let tx_cb = tx_auto.clone();
            let heard_speech = Arc::new(AtomicBool::new(false));
            let silence_triggered = Arc::new(AtomicBool::new(false));
            let last_sound = Arc::new(Mutex::new(Instant::now()));
            let last_paint = Arc::new(Mutex::new(Instant::now()));
            let silence_triggered_cb = Arc::clone(&silence_triggered);

            let on_level: Arc<dyn Fn(f32) + Send + Sync> = Arc::new(move |rms: f32| {
                if show_ov {
                    if let Ok(mut lp) = last_paint.lock() {
                        if lp.elapsed() >= Duration::from_millis(60) {
                            *lp = Instant::now();
                            let lt = live_text_cb.lock().map(|g| g.clone()).unwrap_or_default();
                            ov_cb.set_state(OverlayState::Recording {
                                ai: use_ai,
                                level: rms,
                                live_text: lt,
                            });
                        }
                    }
                }
                if auto_stop && !silence_triggered_cb.load(Ordering::Relaxed) {
                    if rms >= silence_rms {
                        heard_speech.store(true, Ordering::Relaxed);
                        if let Ok(mut ls) = last_sound.lock() {
                            *ls = Instant::now();
                        }
                    } else if heard_speech.load(Ordering::Relaxed) {
                        if let Ok(ls) = last_sound.lock() {
                            if ls.elapsed().as_secs_f64() >= silence_sec {
                                silence_triggered_cb.store(true, Ordering::Relaxed);
                                let _ = tx_cb.send(HotkeyEvent::PrimaryRelease);
                            }
                        }
                    }
                }
            });

            match start_recording_ex(mic_name, cfg.keep_mic_open, Some(on_level)) {
                Ok(handle) => {
                    if cfg.llm_provider == "chatgpt" && (use_ai || cfg.llm_auto_refine) {
                        let root_ref = ctx.root_dir.clone();
                        thread::spawn(move || chatgpt_auth::refresh_if_needed(&root_ref));
                    }
                    if show_ov {
                        ov.position_for(&overlay_place);
                        ov.set_state(OverlayState::Recording {
                            ai: use_ai,
                            level: 0.0,
                            live_text: String::new(),
                        });
                    }
                    if use_ai {
                        tray.set_state((0x7c, 0x3a, 0xed), "OnseiMan | AI 録音中…");
                    } else {
                        tray.set_state((0xdc, 0x26, 0x26), "OnseiMan | 録音中…");
                    }
                    let live = if cfg.live_segmentation {
                        let ctx_live = Arc::clone(ctx);
                        let cfg_live = cfg.clone();
                        let dict_live = ctx.dict.lock().unwrap().clone();
                        let hw = dict_live.active_hotwords();
                        let rd = dict_live.readings();
                        let lt_prog = Arc::clone(&live_text);
                        let ov_prog = Arc::clone(ov);
                        let rec_fn = Arc::new(move |seg: &[f32]| {
                            let norm = normalize_voice_dynamics(seg, 16_000);
                            let wav = encode_wav(&norm, 16_000);
                            ctx_live
                                .transcriber
                                .transcribe_wav(&cfg_live, &wav, &hw, &rd)
                                .map(|r| SegmentRecognition {
                                    text: r.text,
                                    backend: r.backend,
                                    elapsed_ms: r.elapsed_ms,
                                })
                        });
                        let on_prog: Arc<dyn Fn(&str) + Send + Sync> =
                            Arc::new(move |so_far: &str| {
                                if let Ok(mut g) = lt_prog.lock() {
                                    *g = so_far.to_string();
                                }
                                if show_ov {
                                    ov_prog.set_state(OverlayState::Recording {
                                        ai: use_ai,
                                        level: 0.0,
                                        live_text: so_far.to_string(),
                                    });
                                }
                            });
                        Some(LiveTranscription::start(rec_fn, Some(on_prog)))
                    } else {
                        None
                    };
                    let target_app = target_process_name(target);
                    let adaptive_seg = if live.is_some() {
                        Some(AdaptiveSegmenter::new(16_000, 9.0))
                    } else {
                        None
                    };
                    *slot = Some(ActiveSession {
                        handle,
                        _mute_guard: mute_guard,
                        target,
                        target_app,
                        use_ai,
                        silence_triggered,
                        live,
                        adaptive_seg,
                        submitted_samples: Vec::new(),
                    });
                    play_beep(cfg.beep_enabled, if use_ai { 1040 } else { 880 }, 55);
                    eprintln!("録音開始 (ai={use_ai})");
                }
                Err(e) => {
                    drop(mute_guard);
                    play_beep(cfg.beep_enabled, 300, 150);
                    eprintln!("録音開始エラー: {e}");
                }
            }
        };

        let finish_rec = |ctx: &Arc<AppContext>,
                          ov: &Arc<RecordingOverlay>,
                          slot: &mut Option<ActiveSession>| {
            let Some(mut sess) = slot.take() else {
                return;
            };
            let tail_samples = sess.handle.stop();
            drop(sess._mute_guard); // 録音終了と同時に他アプリの音を戻す
            let (beep_enabled, min_dur) = ctx
                .config
                .lock()
                .map(|c| (c.beep_enabled, c.min_duration_sec.max(0.05)))
                .unwrap_or((true, 0.15));
            let mut all_samples = std::mem::take(&mut sess.submitted_samples);
            all_samples.extend_from_slice(&tail_samples);
            let duration_sec = all_samples.len() as f64 / 16_000.0;
            if duration_sec < min_dur {
                if let Some(mut live) = sess.live.take() {
                    live.cancel();
                }
                play_beep(beep_enabled, 300, 120);
                ov.set_state(OverlayState::Hidden);
                restore_idle_tray(ctx, &tray);
                eprintln!("録音が短すぎるためスキップ ({duration_sec:.2}s)");
                return;
            }
            play_beep(beep_enabled, 660, 50);
            let show_ov = ctx.config.lock().map(|c| c.show_overlay).unwrap_or(true);
            if show_ov {
                ov.set_state(OverlayState::Transcribing);
            }
            tray.set_state((0x7c, 0x3a, 0xed), "OnseiMan | 文字にしています…");
            let ctx_job = ctx.clone();
            let ov_job = Arc::clone(ov);
            let tray_job = Arc::clone(&tray);
            let watcher_job = Arc::clone(&uia_watcher);
            let ov_gen_job = Arc::clone(&overlay_gen);
            let target = sess.target;
            let target_app = sess.target_app.take();
            let use_ai = sess.use_ai;
            let live_opt = sess.live.take();
            let mut adaptive_seg = sess.adaptive_seg.take();
            thread::spawn(move || {
                let ts = Local::now().format("%Y%m%d_%H%M%S").to_string();
                let audio_name = format!("{ts}.wav");

                let cfg = ctx_job.config.lock().unwrap().clone();
                let dict = ctx_job.dict.lock().unwrap().clone();
                let hotwords = dict.active_hotwords();
                let readings = dict.readings();

                let rec_res: Result<(String, String, f64), String> = match live_opt {
                    Some(live) if live.segment_count() > 0 => {
                        let save_dir = ctx_job.audio_dir.clone();
                        let save_name = audio_name.clone();
                        thread::spawn(move || {
                            let norm = normalize_voice_dynamics(&all_samples, 16_000);
                            let wav = encode_wav(&norm, 16_000);
                            let _ = std::fs::create_dir_all(&save_dir);
                            let _ = std::fs::write(save_dir.join(&save_name), &wav);
                        });
                        if let Some(ref mut seg) = adaptive_seg {
                            for c in seg.push(&tail_samples) {
                                live.submit(c);
                            }
                            if let Some(c) = seg.flush() {
                                live.submit(c);
                            }
                        } else if tail_samples.len() >= 1600 {
                            live.submit(tail_samples);
                        }
                        live.finish()
                            .map(|r| (r.text, r.backend, r.wait_ms.max(r.asr_ms_total)))
                    }
                    other_live => {
                        if let Some(mut live) = other_live {
                            live.cancel();
                        }
                        let norm_samples = normalize_voice_dynamics(&all_samples, 16_000);
                        let wav = Arc::new(encode_wav(&norm_samples, 16_000));
                        let save_dir = ctx_job.audio_dir.clone();
                        let save_name = audio_name.clone();
                        let save_wav = Arc::clone(&wav);
                        thread::spawn(move || {
                            let _ = std::fs::create_dir_all(&save_dir);
                            let _ = std::fs::write(save_dir.join(&save_name), save_wav.as_slice());
                        });
                        ctx_job
                            .transcriber
                            .transcribe_wav(&cfg, &wav, &hotwords, &readings)
                            .map(|r| (r.text, r.backend, r.elapsed_ms))
                    }
                };

                let mut showed_done = false;
                match rec_res {
                    Ok((raw_text, backend, elapsed_ms)) => {
                        if cfg.undo_voice_command && is_undo_command(&raw_text) {
                            let ok = send_undo_shortcut(target);
                            play_beep(cfg.beep_enabled, if ok { 520 } else { 300 }, 65);
                            append_log(
                                &ctx_job.root_dir,
                                &format!("音声コマンド: 取り消し (Ctrl+Z ok={ok})"),
                            );
                        } else {
                            let formatted = format_utterance_full(
                                &raw_text,
                                &cfg,
                                &dict,
                                use_ai,
                                Some(&ctx_job.root_dir),
                                target_app.as_deref(),
                            );
                            if !formatted.text.is_empty() {
                                let pr = paste_text(
                                    &formatted.text,
                                    cfg.restore_clipboard,
                                    cfg.paste_delay_ms,
                                    cfg.clipboard_restore_delay_ms,
                                    target,
                                );
                                append_log(
                                    &ctx_job.root_dir,
                                    &format!(
                                        "認識完了 [{backend}] {elapsed_ms:.0}ms ({duration_sec:.2}s) paste={} chars={}",
                                        pr.reason,
                                        formatted.text.chars().count()
                                    ),
                                );
                                if let Ok(mut h) = ctx_job.history.lock() {
                                    h.record_transcription(
                                        &backend,
                                        elapsed_ms,
                                        duration_sec,
                                        &raw_text,
                                        &formatted.text,
                                        &audio_name,
                                        "",
                                    );
                                }
                                // UIA 手直し自動学習監視
                                if cfg.learn_from_edits {
                                    let ctx_learn = Arc::clone(&ctx_job);
                                    let auto_cnt = cfg.auto_learn_after_count;
                                    watcher_job.watch_after_paste(
                                        formatted.text.clone(),
                                        target,
                                        move |before, after| {
                                            let added = if let Ok(mut cands) =
                                                ctx_learn.candidates.lock()
                                            {
                                                cands.record_edit(&before, &after)
                                            } else {
                                                0
                                            };
                                            if added > 0 {
                                                append_log(
                                                    &ctx_learn.root_dir,
                                                    &format!(
                                                        "手直しから学習候補を {added} 件検知"
                                                    ),
                                                );
                                                if auto_cnt > 0 {
                                                    let to_promote: Vec<(String, String)> =
                                                        ctx_learn
                                                            .candidates
                                                            .lock()
                                                            .map(|c| {
                                                                c.candidates()
                                                                    .iter()
                                                                    .filter(|it| {
                                                                        it.count >= auto_cnt
                                                                    })
                                                                    .map(|it| {
                                                                        (
                                                                            it.src.clone(),
                                                                            it.dst.clone(),
                                                                        )
                                                                    })
                                                                    .collect()
                                                            })
                                                            .unwrap_or_default();
                                                    for (src, dst) in to_promote {
                                                        if let Ok(mut c) =
                                                            ctx_learn.candidates.lock()
                                                        {
                                                            c.approve(&src, &dst);
                                                        }
                                                        onseiman_app::web_ui::add_replacement_to_dict(
                                                            &ctx_learn, &src, &dst,
                                                        );
                                                    }
                                                }
                                            }
                                        },
                                    );
                                }
                                if show_ov {
                                    let snippet: String = formatted.text.chars().take(18).collect();
                                    ov_job.set_state(OverlayState::Done { text: snippet });
                                    showed_done = true;
                                    let my_gen = ov_gen_job.load(Ordering::SeqCst);
                                    let ov_hide = Arc::clone(&ov_job);
                                    let gen_check = Arc::clone(&ov_gen_job);
                                    thread::spawn(move || {
                                        thread::sleep(Duration::from_millis(2600));
                                        if gen_check.load(Ordering::SeqCst) == my_gen {
                                            ov_hide.set_state(OverlayState::Hidden);
                                        }
                                    });
                                }
                            } else {
                                append_log(
                                    &ctx_job.root_dir,
                                    &format!("認識結果が空でした ({duration_sec:.2}s)"),
                                );
                            }
                        }
                    }
                    Err(e) => {
                        play_beep(cfg.beep_enabled, 260, 180);
                        append_log(&ctx_job.root_dir, &format!("認識エラー: {e}"));
                        if let Ok(mut h) = ctx_job.history.lock() {
                            h.record_failure(&e, duration_sec, &audio_name, "");
                        }
                    }
                }
                if !showed_done {
                    ov_job.set_state(OverlayState::Hidden);
                }
                restore_idle_tray(&ctx_job, &tray_job);
            });
        };

        loop {
            let poll_ms = if active.is_some() { 100 } else { 250 };
            match rx.recv_timeout(Duration::from_millis(poll_ms)) {
                Ok(ev) => {
                    let is_hold = ctx
                        .config
                        .lock()
                        .map(|c| c.hotkey_mode.eq_ignore_ascii_case("hold"))
                        .unwrap_or(false);
                    match ev {
                        HotkeyEvent::PrimaryPress => {
                            if is_hold {
                                if active.is_none() {
                                    start_rec(&ctx, &overlay, &mut active, false);
                                }
                            } else if active.is_none() {
                                start_rec(&ctx, &overlay, &mut active, false);
                            } else {
                                finish_rec(&ctx, &overlay, &mut active);
                            }
                        }
                        HotkeyEvent::PrimaryRelease => {
                            let triggered = active
                                .as_ref()
                                .map(|s| s.silence_triggered.load(Ordering::Relaxed))
                                .unwrap_or(false);
                            if (is_hold || triggered) && active.is_some() {
                                finish_rec(&ctx, &overlay, &mut active);
                            }
                        }
                        HotkeyEvent::AiPress => {
                            if is_hold {
                                if active.is_none() {
                                    start_rec(&ctx, &overlay, &mut active, true);
                                }
                            } else if active.is_none() {
                                start_rec(&ctx, &overlay, &mut active, true);
                            } else {
                                finish_rec(&ctx, &overlay, &mut active);
                            }
                        }
                        HotkeyEvent::AiRelease => {
                            if is_hold && active.is_some() {
                                finish_rec(&ctx, &overlay, &mut active);
                            }
                        }
                        HotkeyEvent::Cancel => {
                            if let Some(mut sess) = active.take() {
                                if let Some(mut live) = sess.live.take() {
                                    live.cancel();
                                }
                                let _ = sess.handle.stop();
                                drop(sess._mute_guard);
                                let beep_enabled =
                                    ctx.config.lock().map(|c| c.beep_enabled).unwrap_or(true);
                                play_beep(beep_enabled, 400, 90);
                                overlay.set_state(OverlayState::Hidden);
                                restore_idle_tray(&ctx, &tray);
                                eprintln!("録音をキャンセルしました");
                            }
                        }
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if let Some(sess) = active.as_mut() {
                        // 最長録音時間（max_duration_sec）に達したら自動停止
                        let max_dur = ctx
                            .config
                            .lock()
                            .map(|c| c.max_duration_sec.max(5.0))
                            .unwrap_or(180.0);
                        let total_elapsed = (sess.submitted_samples.len() as f64 / 16_000.0)
                            + sess.handle.elapsed_sec();
                        if total_elapsed >= max_dur {
                            append_log(
                                &ctx.root_dir,
                                &format!("最長録音時間 ({max_dur:.0}s) に達したため自動停止"),
                            );
                            finish_rec(&ctx, &overlay, &mut active);
                            continue;
                        }
                        // 適応型ノイズフロア＋末尾 200ms 余白のセグメンタで自然な無音区切りごとに先行文字起こし
                        if sess.live.is_some() && sess.handle.elapsed_sec() >= 0.4 {
                            let chunk = sess.handle.drain_16k();
                            if !chunk.is_empty() {
                                sess.submitted_samples.extend_from_slice(&chunk);
                                if let Some(ref mut seg) = sess.adaptive_seg {
                                    for ready_seg in seg.push(&chunk) {
                                        if let Some(ref live) = sess.live {
                                            live.submit(ready_seg);
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
    });

    Some(hk)
}
