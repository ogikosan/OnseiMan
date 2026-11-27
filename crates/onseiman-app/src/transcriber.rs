//! ハイブリッド認識クライアント（LAN サーバー優先＋ローカル `onseiman-speech` フォールバック）。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use onseiman_core::audio::decode_wav;
use onseiman_core::config::ClientConfig;
use onseiman_core::segmenter::{join_texts, split_on_pauses, SegmentOptions};
use onseiman_speech::{SpeechEngine, MAX_SEGMENT_SEC};
use serde::{Deserialize, Serialize};

pub const MAX_ASR_CHARS: usize = 5000;
const DOWN_RETRY_SEC: u64 = 60;

/// 聞き取り結果から制御文字と特殊トークン（`<|...|>`・`/sil`）を除去する。
pub fn clean_asr_text(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let truncated: String = text.chars().take(MAX_ASR_CHARS).collect();
    // 制御文字・行区切りを空白へ
    let mut cleaned = String::with_capacity(truncated.len());
    let mut in_ctrl = false;
    for c in truncated.chars() {
        let is_ctrl = (c as u32) <= 0x1f
            || ((c as u32) >= 0x7f && (c as u32) <= 0x9f)
            || c == '\u{2028}'
            || c == '\u{2029}';
        if is_ctrl {
            if !in_ctrl {
                cleaned.push(' ');
                in_ctrl = true;
            }
        } else {
            in_ctrl = false;
            cleaned.push(c);
        }
    }
    // `<|...|>` を除去
    let mut out = String::with_capacity(cleaned.len());
    let chars: Vec<char> = cleaned.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if i + 1 < chars.len() && chars[i] == '<' && chars[i + 1] == '|' {
            if let Some(end) = (i + 2..chars.len().saturating_sub(1))
                .find(|&j| chars[j] == '|' && chars[j + 1] == '>')
            {
                i = end + 2;
                continue;
            }
        }
        out.push(chars[i]);
        i += 1;
    }
    // `/sil` 単語境界を除去
    let out = strip_sil_token(&out);
    out.trim().to_string()
}

fn strip_sil_token(s: &str) -> String {
    let lower = s.to_lowercase();
    let bytes = lower.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < s.len() {
        if lower[i..].starts_with("/sil") {
            let after = i + 4;
            let boundary_ok = after >= bytes.len()
                || !(bytes[after].is_ascii_alphanumeric() || bytes[after] == b'_');
            if boundary_ok {
                i = after;
                continue;
            }
        }
        let ch = s[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServerInfo {
    pub version: String,
    pub fingerprint: String,
    pub name: String,
    pub id: String,
    pub engine: String,
    pub max_segment_sec: Option<f64>,
    pub provider: String,
}

#[derive(Debug, Clone)]
pub struct TranscriptionResult {
    pub text: String,
    pub backend: String,
    pub elapsed_ms: f64,
}

#[derive(Default)]
struct State {
    preferred: String,
    down_until: HashMap<String, Instant>,
    server_info: HashMap<String, ServerInfo>,
    last_backend_status: String,
}

pub struct HybridTranscriber {
    root_dir: PathBuf,
    http_agent: ureq::Agent,
    state: Mutex<State>,
    local_engine: Mutex<Option<Arc<SpeechEngine>>>,
}

impl HybridTranscriber {
    pub fn new(root_dir: PathBuf) -> Self {
        let http_agent = ureq::AgentBuilder::new().redirects(0).build();
        Self {
            root_dir,
            http_agent,
            state: Mutex::new(State {
                last_backend_status: "待機中".into(),
                ..Default::default()
            }),
            local_engine: Mutex::new(None),
        }
    }

    pub fn last_backend_status(&self) -> String {
        self.state
            .lock()
            .map(|s| s.last_backend_status.clone())
            .unwrap_or_else(|_| "待機中".into())
    }

    pub fn server_info_snapshot(&self) -> HashMap<String, ServerInfo> {
        self.state
            .lock()
            .map(|s| s.server_info.clone())
            .unwrap_or_default()
    }

    pub fn preferred_server_name(&self) -> String {
        let Ok(st) = self.state.lock() else {
            return String::new();
        };
        st.server_info
            .get(&st.preferred)
            .map(|i| i.name.clone())
            .unwrap_or_default()
    }

    fn uses_servers(cfg: &ClientConfig) -> bool {
        let p = cfg.recognition_place.trim();
        if p.is_empty() {
            !cfg.server_urls.is_empty()
        } else {
            p == "server"
        }
    }

    fn server_list(cfg: &ClientConfig) -> Vec<String> {
        if !Self::uses_servers(cfg) {
            return Vec::new();
        }
        let mut seen = Vec::new();
        for u in &cfg.server_urls {
            let b = u.trim().trim_end_matches('/').to_string();
            if !b.is_empty() && !seen.contains(&b) {
                seen.push(b);
            }
        }
        seen
    }

    pub fn ordered_servers(&self, cfg: &ClientConfig) -> Vec<String> {
        let servers = Self::server_list(cfg);
        let now = Instant::now();
        let (mut alive, pref) = {
            let st = self.state.lock().unwrap();
            let alive = servers
                .iter()
                .filter(|u| st.down_until.get(*u).map(|&t| t <= now).unwrap_or(true))
                .cloned()
                .collect::<Vec<_>>();
            (alive, st.preferred.clone())
        };
        let chosen = cfg
            .preferred_server
            .trim()
            .trim_end_matches('/')
            .to_string();
        for first in [pref, chosen] {
            if !first.is_empty() {
                if let Some(pos) = alive.iter().position(|u| u == &first) {
                    let v = alive.remove(pos);
                    alive.insert(0, v);
                }
            }
        }
        if alive.is_empty() {
            servers
        } else {
            alive
        }
    }

    fn probe_one(&self, base_url: &str, timeout_sec: f64) -> Option<ServerInfo> {
        let resp = self
            .http_agent
            .get(&format!("{base_url}/health"))
            .timeout(Duration::from_secs_f64(timeout_sec.clamp(0.5, 2.0)))
            .call()
            .ok()?;
        if resp.status() != 200 {
            return None;
        }
        let data: serde_json::Value = resp.into_json().ok()?;
        if data.get("status").and_then(|v| v.as_str()) != Some("ok") {
            return None;
        }
        let max_seg = data
            .get("max_segment_sec")
            .and_then(|v| v.as_f64())
            .filter(|&f| (1.0..=120.0).contains(&f));
        let info = ServerInfo {
            version: data
                .get("version")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(20)
                .collect(),
            fingerprint: data
                .get("fingerprint")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(64)
                .collect(),
            name: data
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(64)
                .collect(),
            id: data
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(64)
                .collect(),
            engine: data
                .get("engine")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .chars()
                .take(16)
                .collect(),
            max_segment_sec: max_seg,
            provider: data
                .get("provider")
                .and_then(|v| v.as_str())
                .unwrap_or("lan")
                .to_string(),
        };
        if let Ok(mut st) = self.state.lock() {
            st.server_info.insert(base_url.to_string(), info.clone());
        }
        Some(info)
    }

    pub fn check_server_health(&self, cfg: &ClientConfig) -> Option<String> {
        let servers = Self::server_list(cfg);
        let mut results: HashMap<String, Option<ServerInfo>> = HashMap::new();
        for u in &servers {
            results.insert(
                u.clone(),
                self.probe_one(u, cfg.server_connect_timeout_sec.min(1.5)),
            );
        }
        let mut st = self.state.lock().unwrap();
        let down_deadline = Instant::now() + Duration::from_secs(DOWN_RETRY_SEC);
        for u in &servers {
            if results.get(u).and_then(|v| v.as_ref()).is_some() {
                st.down_until.remove(u);
            } else {
                st.down_until.insert(u.clone(), down_deadline);
                if st.preferred == *u {
                    st.preferred.clear();
                }
            }
        }
        let want = cfg.preferred_server.trim().trim_end_matches('/');
        let chosen = if !want.is_empty() && results.get(want).and_then(|v| v.as_ref()).is_some() {
            Some(want.to_string())
        } else if !st.preferred.is_empty()
            && results
                .get(&st.preferred)
                .and_then(|v| v.as_ref())
                .is_some()
        {
            Some(st.preferred.clone())
        } else {
            servers
                .iter()
                .find(|u| results.get(*u).and_then(|v| v.as_ref()).is_some())
                .cloned()
        };
        if let Some(ref c) = chosen {
            let prov = results
                .get(c)
                .and_then(|v| v.as_ref())
                .map(|i| i.provider.as_str())
                .unwrap_or("lan");
            st.preferred = c.clone();
            st.last_backend_status = format!("LAN ({prov})");
            return chosen;
        }
        if cfg.local_fallback || !Self::uses_servers(cfg) {
            st.last_backend_status = "Local".into();
        } else {
            st.last_backend_status = "未接続".into();
        }
        None
    }

    pub fn transcribe_wav(
        &self,
        cfg: &ClientConfig,
        wav_bytes: &[u8],
        hotwords: &[String],
        readings: &HashMap<String, Vec<String>>,
    ) -> Result<TranscriptionResult, String> {
        let t0 = Instant::now();
        let servers = self.ordered_servers(cfg);
        let mut last_err = String::new();

        for base_url in servers {
            match self.post_transcribe(
                &base_url,
                wav_bytes,
                hotwords,
                readings,
                cfg.server_read_timeout_sec,
            ) {
                Ok((text, provider)) => {
                    if let Ok(mut st) = self.state.lock() {
                        st.down_until.remove(&base_url);
                        st.preferred = base_url.clone();
                        st.last_backend_status = format!("LAN ({provider})");
                    }
                    return Ok(TranscriptionResult {
                        text: clean_asr_text(&text),
                        backend: format!("LAN ({provider})"),
                        elapsed_ms: t0.elapsed().as_secs_f64() * 1000.0,
                    });
                }
                Err(e) => {
                    last_err = e;
                    if let Ok(mut st) = self.state.lock() {
                        st.down_until.insert(
                            base_url.clone(),
                            Instant::now() + Duration::from_secs(DOWN_RETRY_SEC),
                        );
                        if st.preferred == base_url {
                            st.preferred.clear();
                        }
                    }
                }
            }
        }

        if cfg.local_fallback || !Self::uses_servers(cfg) {
            let text = self.transcribe_local(cfg, wav_bytes, hotwords, readings)?;
            if let Ok(mut st) = self.state.lock() {
                st.last_backend_status = "Local".into();
            }
            return Ok(TranscriptionResult {
                text: clean_asr_text(&text),
                backend: "Local".into(),
                elapsed_ms: t0.elapsed().as_secs_f64() * 1000.0,
            });
        }

        Err(if last_err.is_empty() {
            "認識サーバーに接続できませんでした".into()
        } else {
            last_err
        })
    }

    fn post_transcribe(
        &self,
        base_url: &str,
        wav_bytes: &[u8],
        hotwords: &[String],
        readings: &HashMap<String, Vec<String>>,
        timeout_sec: f64,
    ) -> Result<(String, String), String> {
        let boundary = "----OnseiManRustBoundary7f3a9c";
        let mut body = Vec::with_capacity(wav_bytes.len() + 1024);

        // audio part（onseiman-server はフィールド名 "audio" を受け付ける）
        body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
        body.extend_from_slice(
            b"Content-Disposition: form-data; name=\"audio\"; filename=\"audio.wav\"\r\n",
        );
        body.extend_from_slice(b"Content-Type: audio/wav\r\n\r\n");
        body.extend_from_slice(wav_bytes);
        body.extend_from_slice(b"\r\n");

        // hotwords part
        if !hotwords.is_empty() {
            let hw = hotwords.join(",");
            body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
            body.extend_from_slice(b"Content-Disposition: form-data; name=\"hotwords\"\r\n\r\n");
            body.extend_from_slice(hw.as_bytes());
            body.extend_from_slice(b"\r\n");
        }

        // keywords part（onseiman-server は読みヒントを "keywords" で受け付ける）
        if !readings.is_empty() {
            if let Ok(rjson) = serde_json::to_string(readings) {
                body.extend_from_slice(format!("--{boundary}\r\n").as_bytes());
                body.extend_from_slice(
                    b"Content-Disposition: form-data; name=\"keywords\"\r\n\r\n",
                );
                body.extend_from_slice(rjson.as_bytes());
                body.extend_from_slice(b"\r\n");
            }
        }

        body.extend_from_slice(format!("--{boundary}--\r\n").as_bytes());

        let resp = self
            .http_agent
            .post(&format!("{base_url}/transcribe"))
            .timeout(Duration::from_secs_f64(timeout_sec.clamp(2.0, 120.0)))
            .set(
                "Content-Type",
                &format!("multipart/form-data; boundary={boundary}"),
            )
            .send_bytes(&body)
            .map_err(|e| format!("LAN サーバー送信エラー: {e}"))?;

        let val: serde_json::Value = resp
            .into_json()
            .map_err(|e| format!("LAN サーバー応答の解析エラー: {e}"))?;
        let text = val
            .get("text")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        let provider = val
            .get("provider")
            .and_then(|v| v.as_str())
            .unwrap_or("lan")
            .to_string();
        Ok((text, provider))
    }

    fn ensure_local_engine(&self, cfg: &ClientConfig) -> Result<Arc<SpeechEngine>, String> {
        let mut guard = self.local_engine.lock().map_err(|_| "lock poisoned")?;
        if let Some(ref eng) = *guard {
            return Ok(Arc::clone(eng));
        }
        let eng = Arc::new(SpeechEngine::new(
            &self.root_dir,
            &cfg.local_device,
            cfg.local_num_threads,
        ));
        *guard = Some(Arc::clone(&eng));
        Ok(eng)
    }

    fn transcribe_local(
        &self,
        cfg: &ClientConfig,
        wav_bytes: &[u8],
        hotwords: &[String],
        readings: &HashMap<String, Vec<String>>,
    ) -> Result<String, String> {
        let eng = self.ensure_local_engine(cfg)?;
        let (samples, sr) =
            decode_wav(wav_bytes, Some(300.0)).map_err(|e| format!("WAV エラー: {e:?}"))?;
        let pieces = split_on_pauses(
            &samples,
            sr,
            SegmentOptions {
                max_segment_sec: MAX_SEGMENT_SEC,
                ..Default::default()
            },
        );
        let hw_csv = hotwords.join(",");
        let mut texts = Vec::with_capacity(pieces.len());
        for piece in pieces {
            let (t, _ms) = eng
                .transcribe(&piece, sr, &hw_csv, readings)
                .map_err(|e| format!("ローカル認識エラー: {e}"))?;
            if !t.trim().is_empty() {
                texts.push(t);
            }
        }
        Ok(join_texts(&texts))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clean_asr_strips_controls_and_special_tokens() {
        assert_eq!(
            clean_asr_text("<|ja|>こんにちは\n世界 /sil"),
            "こんにちは 世界"
        );
        assert_eq!(clean_asr_text("  /SIL テスト <|endoftext|> "), "テスト");
    }
}
