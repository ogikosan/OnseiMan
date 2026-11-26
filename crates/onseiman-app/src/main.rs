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
            if dir.join("config.json").is_file() || dir.join("client").join("web").is_dir() {
                return dir.to_path_buf();
            }
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}
use onseiman_core::audio::encode_wav;
use onseiman_platform::audio_mute::AudioMuteGuard;
use onseiman_platform::binding::parse_binding;
use onseiman_platform::hotkey::{GlobalHotkey, HotkeyConfig, HotkeyEvent};
use onseiman_platform::overlay::{OverlayState, RecordingOverlay};
use onseiman_platform::paste::{capture_paste_target, paste_text};
use onseiman_platform::recorder::{list_input_devices, start_recording, RecordingHandle};
use onseiman_platform::tray::{TrayEvent, TrayIcon};
use tokio::net::TcpListener;
use tokio::sync::Notify;

use onseiman_app::pipeline::format_utterance;
use onseiman_app::web_ui::{
    open_browser, retranscribe_latest, router, sync_hotkey_from_config, AppContext,
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
                        open_browser(&format!("{url_tray}/#history"), Some(&profile))
                    }
                    TrayEvent::OpenDict => {
                        open_browser(&format!("{url_tray}/#dict"), Some(&profile))
                    }
                    TrayEvent::OpenSettings | TrayEvent::CheckUpdate => {
                        open_browser(&format!("{url_tray}/#settings"), Some(&profile))
                    }
                    TrayEvent::OpenHelp => {
                        open_browser(&format!("{url_tray}/#help"), Some(&profile))
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

    // バックグラウンドでサーバーの生死確認＋トレイ色を状態に合わせる
    {
        let ctx_health = ctx.clone();
        let tray_health = Arc::clone(&tray);
        thread::spawn(move || {
            let cfg = ctx_health.config.lock().unwrap().clone();
            let _ = ctx_health.transcriber.check_server_health(&cfg);
            restore_idle_tray(&ctx_health, &tray_health);
        });
    }

    // グローバルホットキー＋録音・認識・貼り付けループ
    let _hotkey_guard = start_hotkey_loop(ctx.clone(), Arc::clone(&overlay), Arc::clone(&tray));

    if args.open_ui {
        let url_open = url.clone();
        let profile = ctx.root_dir.join("logs").join("browser_profile");
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(150));
            open_browser(&url_open, Some(&profile));
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
    use_ai: bool,
    silence_triggered: Arc<AtomicBool>,
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
        let mut active: Option<ActiveSession> = None;

        let start_rec = |ctx: &Arc<AppContext>,
                         ov: &Arc<RecordingOverlay>,
                         slot: &mut Option<ActiveSession>,
                         use_ai: bool| {
            let cfg = ctx.config.lock().unwrap().clone();
            let target = capture_paste_target();
            let mute_guard = if cfg.mute_other_audio {
                Some(AudioMuteGuard::mute_others())
            } else {
                None
            };
            let mic_name = cfg.microphone.as_ref().and_then(|v| v.as_str());
            let show_ov = cfg.show_overlay;
            let auto_stop = cfg.auto_stop_on_silence;
            let silence_sec = cfg.silence_stop_sec.max(0.3);
            let silence_rms = cfg.silence_rms_threshold as f32;
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
                            ov_cb.set_state(OverlayState::Recording {
                                ai: use_ai,
                                level: rms,
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

            match start_recording(mic_name, Some(on_level)) {
                Ok(handle) => {
                    if show_ov {
                        ov.set_state(OverlayState::Recording {
                            ai: use_ai,
                            level: 0.0,
                        });
                    }
                    if use_ai {
                        tray.set_state((0x7c, 0x3a, 0xed), "OnseiMan | AI 録音中…");
                    } else {
                        tray.set_state((0xdc, 0x26, 0x26), "OnseiMan | 録音中…");
                    }
                    *slot = Some(ActiveSession {
                        handle,
                        _mute_guard: mute_guard,
                        target,
                        use_ai,
                        silence_triggered,
                    });
                    eprintln!("録音開始 (ai={use_ai})");
                }
                Err(e) => {
                    drop(mute_guard);
                    eprintln!("録音開始エラー: {e}");
                }
            }
        };

        let finish_rec = |ctx: &Arc<AppContext>,
                          ov: &Arc<RecordingOverlay>,
                          slot: &mut Option<ActiveSession>| {
            let Some(sess) = slot.take() else {
                return;
            };
            let samples = sess.handle.stop();
            drop(sess._mute_guard); // 録音終了と同時に他アプリの音を戻す
            let duration_sec = samples.len() as f64 / 16_000.0;
            if duration_sec < 0.15 {
                ov.set_state(OverlayState::Hidden);
                restore_idle_tray(ctx, &tray);
                eprintln!("録音が短すぎるためスキップ ({duration_sec:.2}s)");
                return;
            }
            let show_ov = ctx.config.lock().map(|c| c.show_overlay).unwrap_or(true);
            if show_ov {
                ov.set_state(OverlayState::Transcribing);
            }
            tray.set_state((0x7c, 0x3a, 0xed), "OnseiMan | 文字にしています…");
            let ctx_job = ctx.clone();
            let ov_job = Arc::clone(ov);
            let tray_job = Arc::clone(&tray);
            let target = sess.target;
            let use_ai = sess.use_ai;
            thread::spawn(move || {
                let wav = encode_wav(&samples, 16_000);
                let ts = Local::now().format("%Y%m%d_%H%M%S").to_string();
                let audio_name = format!("{ts}.wav");
                let _ = std::fs::create_dir_all(&ctx_job.audio_dir);
                let _ = std::fs::write(ctx_job.audio_dir.join(&audio_name), &wav);

                let cfg = ctx_job.config.lock().unwrap().clone();
                let dict = ctx_job.dict.lock().unwrap().clone();
                let hotwords = dict.active_hotwords();
                let readings = dict.readings();

                match ctx_job
                    .transcriber
                    .transcribe_wav(&cfg, &wav, &hotwords, &readings)
                {
                    Ok(res) => {
                        let formatted = format_utterance(&res.text, &cfg, &dict, use_ai);
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
                                    "認識完了 [{}] {:.0}ms ({:.2}s) paste={} chars={}",
                                    res.backend,
                                    res.elapsed_ms,
                                    duration_sec,
                                    pr.reason,
                                    formatted.text.chars().count()
                                ),
                            );
                            if let Ok(mut h) = ctx_job.history.lock() {
                                h.record_transcription(
                                    &res.backend,
                                    res.elapsed_ms,
                                    duration_sec,
                                    &res.text,
                                    &formatted.text,
                                    &audio_name,
                                    "",
                                );
                            }
                        } else {
                            append_log(
                                &ctx_job.root_dir,
                                &format!("認識結果が空でした ({duration_sec:.2}s)"),
                            );
                        }
                    }
                    Err(e) => {
                        append_log(&ctx_job.root_dir, &format!("認識エラー: {e}"));
                        if let Ok(mut h) = ctx_job.history.lock() {
                            h.record_failure(&e, duration_sec, &audio_name, "");
                        }
                    }
                }
                ov_job.set_state(OverlayState::Hidden);
                restore_idle_tray(&ctx_job, &tray_job);
            });
        };

        while let Ok(ev) = rx.recv() {
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
                    if let Some(sess) = active.take() {
                        let _ = sess.handle.stop();
                        drop(sess._mute_guard);
                        overlay.set_state(OverlayState::Hidden);
                        restore_idle_tray(&ctx, &tray);
                        eprintln!("録音をキャンセルしました");
                    }
                }
            }
        }
    });

    Some(hk)
}
