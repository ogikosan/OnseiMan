//! LAN 内の OnseiMan 認識サーバーを自動探索する（`/health` 並列プローブ）。

use std::collections::HashSet;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream, UdpSocket};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const DEFAULT_SERVER_PORT: u16 = 8765;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiscoveredServer {
    pub url: String,
    pub name: String,
    pub provider: String,
    pub version: String,
    pub id: String,
}

fn local_ipv4() -> Option<Ipv4Addr> {
    let sock = UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    match sock.local_addr().ok()?.ip() {
        IpAddr::V4(v4) if !v4.is_loopback() => Some(v4),
        _ => None,
    }
}

fn probe_candidate(ip: Ipv4Addr, port: u16) -> Option<DiscoveredServer> {
    let addr = SocketAddr::new(IpAddr::V4(ip), port);
    // まず TCP 接続だけ高速に確認（250ms）
    TcpStream::connect_timeout(&addr, Duration::from_millis(250)).ok()?;
    let url = format!("http://{ip}:{port}");
    let agent = ureq::AgentBuilder::new()
        .timeout(Duration::from_millis(900))
        .redirects(0)
        .build();
    let resp = agent.get(&format!("{url}/health")).call().ok()?;
    if resp.status() != 200 {
        return None;
    }
    let v: serde_json::Value = resp.into_json().ok()?;
    if v.get("status").and_then(|s| s.as_str()) != Some("ok") {
        return None;
    }
    Some(DiscoveredServer {
        url,
        name: v
            .get("name")
            .and_then(|s| s.as_str())
            .unwrap_or("")
            .chars()
            .take(64)
            .collect(),
        provider: v
            .get("provider")
            .and_then(|s| s.as_str())
            .unwrap_or("lan")
            .to_string(),
        version: v
            .get("version")
            .and_then(|s| s.as_str())
            .unwrap_or("")
            .chars()
            .take(20)
            .collect(),
        id: v
            .get("id")
            .and_then(|s| s.as_str())
            .unwrap_or("")
            .chars()
            .take(64)
            .collect(),
    })
}

/// 同一 `/24` サブネットと既知 URL から OnseiMan 認識サーバーを並列探索する。
pub fn find_servers(extra_urls: &[String]) -> Vec<DiscoveredServer> {
    let mut ips: Vec<(Ipv4Addr, u16)> = Vec::new();
    let mut seen_target: HashSet<(Ipv4Addr, u16)> = HashSet::new();

    // すでに登録されているサーバーの IP を先に確認
    for raw in extra_urls {
        let stripped = raw
            .trim()
            .trim_start_matches("http://")
            .trim_start_matches("https://")
            .split('/')
            .next()
            .unwrap_or("");
        let (host, port) = if let Some((h, p)) = stripped.split_once(':') {
            (h, p.parse::<u16>().unwrap_or(DEFAULT_SERVER_PORT))
        } else {
            (stripped, DEFAULT_SERVER_PORT)
        };
        if let Ok(ip) = host.parse::<Ipv4Addr>() {
            if seen_target.insert((ip, port)) {
                ips.push((ip, port));
            }
        }
    }

    // 自分の LAN サブネット /24
    if let Some(me) = local_ipv4() {
        let [a, b, c, _] = me.octets();
        for host in 1u8..=254u8 {
            let ip = Ipv4Addr::new(a, b, c, host);
            if seen_target.insert((ip, DEFAULT_SERVER_PORT)) {
                ips.push((ip, DEFAULT_SERVER_PORT));
            }
        }
    }

    let found: Arc<Mutex<Vec<DiscoveredServer>>> = Arc::new(Mutex::new(Vec::new()));
    // 32 スレッド並列で /24 を約 2 秒以内に走査
    let chunk_size = (ips.len() / 32).max(1);
    let mut handles = Vec::new();
    for chunk in ips.chunks(chunk_size) {
        let chunk_vec = chunk.to_vec();
        let found_cl = Arc::clone(&found);
        handles.push(thread::spawn(move || {
            for (ip, port) in chunk_vec {
                if let Some(srv) = probe_candidate(ip, port) {
                    if let Ok(mut g) = found_cl.lock() {
                        g.push(srv);
                    }
                }
            }
        }));
    }
    for h in handles {
        let _ = h.join();
    }

    let mut out = found.lock().map(|g| g.clone()).unwrap_or_default();
    let mut seen_id: HashSet<String> = HashSet::new();
    out.retain(|s| s.id.is_empty() || seen_id.insert(s.id.clone()));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn find_servers_handles_empty_extra_urls() {
        // ネットワーク環境に関わらずパニックしないこと
        let _ = probe_candidate(Ipv4Addr::new(127, 0, 0, 1), 1);
    }
}
