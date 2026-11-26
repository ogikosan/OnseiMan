//! OnseiMan クライアント起動入口（Rust 版）。

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::Arc;
use std::thread;

use chrono::Local;
use onseiman_core::audio::encode_wav;
use onseiman_platform::binding::parse_binding;
use onseiman_platform::hotkey::{GlobalHotkey, HotkeyConfig, HotkeyEvent};
use onseiman_platform::paste::{capture_paste_target, paste_text};
use onseiman_platform::recorder::{list_input_devices, start_recording, RecordingHandle};
use tokio::net::TcpListener;

use onseiman_app::pipeline::format_utterance;
use onseiman_app::web_ui::{open_browser, router, AppContext};

struct Args {
    root: PathBuf,
    port: u16,
    open_ui: bool,
    check_only: bool,
}

fn parse_args() -> Args {
    let mut root = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
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

    // Web UI サーバーを 127.0.0.1:<port>（使用中なら空きポート）で起動
    let listener = match TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], args.port))).await {
        Ok(l) => l,
        Err(_) => TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], 0)))
            .await
            .expect("127.0.0.1 の待機に失敗しました"),
    };
    let actual_port = listener.local_addr().unwrap().port();
    ctx.set_port(actual_port);
    let url = format!("http://127.0.0.1:{actual_port}");
    eprintln!("OnseiMan v{} 管理画面: {url}", env!("CARGO_PKG_VERSION"));

    // バックグラウンドでサーバーの生死確認
    {
        let ctx_health = ctx.clone();
        thread::spawn(move || {
            let cfg = ctx_health.config.lock().unwrap().clone();
            let _ = ctx_health.transcriber.check_server_health(&cfg);
        });
    }

    // グローバルホットキー＋録音・認識・貼り付けループ
    let _hotkey_guard = start_hotkey_loop(ctx.clone());

    if args.open_ui {
        let profile = ctx.root_dir.join("logs").join("browser_profile");
        open_browser(&url, Some(&profile));
    }

    let app = router(ctx);
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .expect("Web UI サーバーでエラーが起きました");
}

fn start_hotkey_loop(ctx: Arc<AppContext>) -> Option<GlobalHotkey> {
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
        let mut active_recorder: Option<RecordingHandle> = None;
        let mut active_target: isize = 0;
        let mut active_ai = false;

        let start_rec = |ctx: &Arc<AppContext>,
                         rec: &mut Option<RecordingHandle>,
                         target: &mut isize,
                         ai_flag: &mut bool,
                         use_ai: bool| {
            let cfg = ctx.config.lock().unwrap().clone();
            *target = capture_paste_target();
            *ai_flag = use_ai;
            let mic_name = cfg.microphone.as_ref().and_then(|v| v.as_str());
            match start_recording(mic_name, None) {
                Ok(r) => {
                    *rec = Some(r);
                    eprintln!("録音開始 (ai={use_ai})");
                }
                Err(e) => eprintln!("録音開始エラー: {e}"),
            }
        };

        let finish_rec = |ctx: &Arc<AppContext>,
                          rec: &mut Option<RecordingHandle>,
                          target: isize,
                          use_ai: bool| {
            let Some(r) = rec.take() else {
                return;
            };
            let samples = r.stop();
            let duration_sec = samples.len() as f64 / 16_000.0;
            if duration_sec < 0.15 {
                eprintln!("録音が短すぎるためスキップ ({duration_sec:.2}s)");
                return;
            }
            let ctx_job = ctx.clone();
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
                            eprintln!(
                                "認識完了 [{}] {:.0}ms paste={}",
                                res.backend, res.elapsed_ms, pr.reason
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
                        }
                    }
                    Err(e) => {
                        eprintln!("認識エラー: {e}");
                    }
                }
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
                        if active_recorder.is_none() {
                            start_rec(
                                &ctx,
                                &mut active_recorder,
                                &mut active_target,
                                &mut active_ai,
                                false,
                            );
                        }
                    } else if active_recorder.is_none() {
                        start_rec(
                            &ctx,
                            &mut active_recorder,
                            &mut active_target,
                            &mut active_ai,
                            false,
                        );
                    } else {
                        finish_rec(&ctx, &mut active_recorder, active_target, active_ai);
                    }
                }
                HotkeyEvent::PrimaryRelease => {
                    if is_hold && active_recorder.is_some() {
                        finish_rec(&ctx, &mut active_recorder, active_target, active_ai);
                    }
                }
                HotkeyEvent::AiPress => {
                    if is_hold {
                        if active_recorder.is_none() {
                            start_rec(
                                &ctx,
                                &mut active_recorder,
                                &mut active_target,
                                &mut active_ai,
                                true,
                            );
                        }
                    } else if active_recorder.is_none() {
                        start_rec(
                            &ctx,
                            &mut active_recorder,
                            &mut active_target,
                            &mut active_ai,
                            true,
                        );
                    } else {
                        finish_rec(&ctx, &mut active_recorder, active_target, active_ai);
                    }
                }
                HotkeyEvent::AiRelease => {
                    if is_hold && active_recorder.is_some() {
                        finish_rec(&ctx, &mut active_recorder, active_target, active_ai);
                    }
                }
                HotkeyEvent::Cancel => {
                    if let Some(r) = active_recorder.take() {
                        let _ = r.stop();
                        eprintln!("録音をキャンセルしました");
                    }
                }
            }
        }
    });

    Some(hk)
}
