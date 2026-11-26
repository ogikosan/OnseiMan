//! 認識役を起動する。
//!
//! ```text
//! onseiman-server [--host 0.0.0.0] [--port 8765] [--threads 4] [--device cpu|gpu] [--root <OnseiMan のフォルダ>]
//! onseiman-server setup [--root <OnseiMan のフォルダ>]     聞き取りの部品（speech.cpp と Haas-ja）を用意する
//! ```
//!
//! `--root` を付けなければ、この実行ファイルのあるフォルダを OnseiMan のフォルダとして使う
//! （その中の `tools/speech`・`models/haas-ja`・`server_data` を使う）。

use std::net::ToSocketAddrs;
use std::path::PathBuf;
use std::sync::Arc;

use onseiman_server::{app, log, Config, Limits};
use onseiman_speech::{setup, SpeechEngine};

struct Args {
    setup: bool,
    host: String,
    port: u16,
    threads: u32,
    device: String,
    root: PathBuf,
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn parse_args() -> Result<Args, String> {
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."));
    let mut a = Args {
        setup: false,
        host: env_or("ONSEIMAN_HOST", "0.0.0.0"),
        port: env_or("ONSEIMAN_PORT", "8765")
            .parse()
            .map_err(|_| "ONSEIMAN_PORT が数字ではありません")?,
        threads: env_or("ONSEIMAN_THREADS", "4")
            .parse()
            .map_err(|_| "ONSEIMAN_THREADS が数字ではありません")?,
        device: env_or("ONSEIMAN_DEVICE", ""),
        root: std::env::var_os("ONSEIMAN_ROOT")
            .map(PathBuf::from)
            .unwrap_or(exe_dir),
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut value = |name: &str| {
            it.next()
                .ok_or_else(|| format!("{name} のあとに値がありません"))
        };
        match arg.as_str() {
            "setup" => a.setup = true,
            "--host" => a.host = value("--host")?,
            "--port" => {
                a.port = value("--port")?
                    .parse()
                    .map_err(|_| "--port が数字ではありません")?
            }
            "--threads" => {
                a.threads = value("--threads")?
                    .parse()
                    .map_err(|_| "--threads が数字ではありません")?
            }
            "--device" => a.device = value("--device")?,
            "--root" => a.root = PathBuf::from(value("--root")?),
            // 前の版の起動の登録（Mac の launchd など）が付けている。受け取って使わない
            "--provider" => {
                value("--provider")?;
            }
            "-h" | "--help" => {
                println!("onseiman-server [--host 0.0.0.0] [--port 8765] [--threads 4] [--device cpu|gpu] [--root <フォルダ>]");
                println!("onseiman-server setup [--root <フォルダ>]   聞き取りの部品を用意する");
                std::process::exit(0);
            }
            other => return Err(format!("分からない指定です: {other}")),
        }
    }
    Ok(a)
}

/// 聞き取りの部品を用意する（進み具合を 10% ごとに出す）。
fn run_setup(root: &std::path::Path) -> i32 {
    let mut last = (String::new(), u64::MAX);
    let mut show = |label: &str, done: u64, total: u64| {
        let pct = (done * 100).checked_div(total).unwrap_or(0);
        if label != last.0 || pct / 10 != last.1 / 10 {
            last = (label.to_string(), pct);
            println!("  {label}: {pct}%");
        }
    };
    match setup::ensure(root, &mut show) {
        Ok(true) => {
            println!("用意できました");
            0
        }
        Ok(false) => {
            println!("この機械では使えません（Windows x64 か、Apple の Mac で使えます）");
            2
        }
        Err(e) => {
            println!("用意できませんでした: {e}");
            1
        }
    }
}

/// このパソコンのアドレス（つなぐ側に手で書くとき用）。(種類, IP) の一覧。
fn local_addresses(server_name: &str) -> Vec<(&'static str, String)> {
    let mut out: Vec<(&'static str, String)> = Vec::new();
    if let Ok(addrs) = (server_name, 0u16).to_socket_addrs() {
        for ip in addrs.filter_map(|a| {
            if a.is_ipv4() {
                Some(a.ip().to_string())
            } else {
                None
            }
        }) {
            if ip.starts_with("127.")
                || ip.starts_with("169.254.")
                || out.iter().any(|(_, o)| *o == ip)
            {
                continue;
            }
            out.push((
                if ip.starts_with("100.") {
                    "Tailscale"
                } else {
                    "家のネットワーク"
                },
                ip,
            ));
        }
    }
    out
}

async fn until_stopped() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    {
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("SIGTERM");
        tokio::select! { _ = ctrl_c => {}, _ = term.recv() => {} }
    }
    #[cfg(not(unix))]
    ctrl_c.await;
}

fn main() {
    let args = match parse_args() {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(2);
        }
    };
    if args.setup {
        std::process::exit(run_setup(&args.root));
    }
    if !setup::ready(&args.root) {
        println!("聞き取りの部品がありません。「onseiman-server setup」で用意してください。");
        println!("（さがした場所: {}）", args.root.display());
        std::process::exit(1);
    }
    let engine = Arc::new(SpeechEngine::new(&args.root, &args.device, args.threads));
    if let Err(e) = engine.load() {
        eprintln!("{e}");
        std::process::exit(1);
    }
    let server_name = gethostname::gethostname().to_string_lossy().into_owned();
    let data_dir = std::env::var_os("ONSEIMAN_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| args.root.join("server_data"));
    let cfg = Config {
        dict_dir: data_dir.join("dicts"),
        server_name: server_name.clone(),
        limits: Limits::default(),
    };
    let provider = engine.active_provider();
    let router = app(engine.clone(), cfg);

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio");
    let result: std::io::Result<()> = runtime.block_on(async {
        let listener = tokio::net::TcpListener::bind((args.host.as_str(), args.port)).await?;
        log!(
            "サーバー起動: http://{}:{} ({provider}, {})",
            args.host,
            args.port,
            onseiman_core::VERSION
        );
        println!();
        println!("OnseiMan のサーバーが動いています。この窓は開いたままにしてください。");
        println!("つなぐ側は、設定の「別のパソコン」→「サーバーをさがす」で自動で見つかります。");
        for (kind, ip) in local_addresses(&server_name) {
            println!(
                "  見つからないときに手で書くアドレス（{kind}）: http://{ip}:{}",
                args.port
            );
        }
        println!();
        axum::serve(listener, router)
            .with_graceful_shutdown(until_stopped())
            .await
    });
    drop(engine);
    if let Err(e) = result {
        eprintln!("サーバーを動かせませんでした: {e}");
        std::process::exit(1);
    }
}
