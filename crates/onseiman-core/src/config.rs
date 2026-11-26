//! クライアント設定（`config.json`）。Python 版 `client/config.py` と同じ項目・既定値で読み書きする。

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use serde::{Deserialize, Serialize};

fn d_true() -> bool {
    true
}
fn d_hotkey() -> String {
    "ralt".to_string()
}
fn d_hotkey_mode() -> String {
    "toggle".to_string()
}
fn d_silence_stop_sec() -> f64 {
    1.8
}
fn d_silence_rms() -> f64 {
    0.012
}
fn d_preroll_sec() -> f64 {
    0.4
}
fn d_segment_pause() -> f64 {
    0.6
}
fn d_segment_min() -> f64 {
    8.0
}
fn d_segment_max() -> f64 {
    18.0
}
fn d_auto_learn() -> u32 {
    2
}
fn d_chat_apps() -> Vec<String> {
    [
        "Discord.exe",
        "Slack.exe",
        "LINE.exe",
        "ms-teams.exe",
        "Teams.exe",
        "Telegram.exe",
        "Chatwork.exe",
    ]
    .iter()
    .map(|s| (*s).to_string())
    .collect()
}
fn d_llm_timeout() -> f64 {
    25.0
}
fn d_cancel_key() -> String {
    "esc".to_string()
}
fn d_min_dur() -> f64 {
    0.3
}
fn d_max_dur() -> f64 {
    120.0
}
fn d_sample_rate() -> u32 {
    16000
}
fn d_connect_timeout() -> f64 {
    1.5
}
fn d_read_timeout() -> f64 {
    30.0
}
fn d_dict_share_name() -> String {
    "shared".to_string()
}
fn d_local_threads() -> u32 {
    4
}
fn d_dict_path() -> String {
    "dict.json".to_string()
}
fn d_paste_delay() -> u32 {
    50
}
fn d_cb_restore_delay() -> u32 {
    180
}
fn d_indicator() -> String {
    "pill".to_string()
}
fn d_overlay_place() -> String {
    "bottom".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ClientConfig {
    #[serde(default = "d_hotkey")]
    pub hotkey: String,
    #[serde(default = "d_hotkey_mode")]
    pub hotkey_mode: String,
    #[serde(default)]
    pub hotkey_extra: String,
    #[serde(default)]
    pub auto_stop_on_silence: bool,
    #[serde(default = "d_silence_stop_sec")]
    pub silence_stop_sec: f64,
    #[serde(default = "d_silence_rms")]
    pub silence_rms_threshold: f64,
    #[serde(default = "d_true")]
    pub remove_fillers: bool,
    #[serde(default)]
    pub normalize_text: bool,
    #[serde(default)]
    pub voice_commands: bool,
    #[serde(default)]
    pub undo_voice_command: bool,
    #[serde(default = "d_true")]
    pub keep_mic_open: bool,
    #[serde(default = "d_preroll_sec")]
    pub preroll_sec: f64,
    #[serde(default)]
    pub live_segmentation: bool,
    #[serde(default = "d_segment_pause")]
    pub segment_pause_sec: f64,
    #[serde(default = "d_segment_min")]
    pub segment_min_sec: f64,
    #[serde(default = "d_segment_max")]
    pub segment_max_sec: f64,
    #[serde(default = "d_true")]
    pub strip_trailing_period: bool,
    #[serde(default = "d_true")]
    pub app_specific_rules: bool,
    #[serde(default = "d_true")]
    pub learn_from_edits: bool,
    #[serde(default = "d_auto_learn")]
    pub auto_learn_after_count: u32,
    #[serde(default = "d_chat_apps")]
    pub chat_apps: Vec<String>,
    #[serde(default)]
    pub ai_hotkey: String,
    #[serde(default)]
    pub llm_provider: String,
    #[serde(default)]
    pub llm_auto_refine: bool,
    #[serde(default)]
    pub llm_base_url: String,
    #[serde(default)]
    pub llm_model: String,
    #[serde(default)]
    pub llm_api_keys: BTreeMap<String, String>,
    #[serde(default = "d_llm_timeout")]
    pub llm_timeout_sec: f64,
    #[serde(default = "d_cancel_key")]
    pub cancel_key: String,
    #[serde(default = "d_true")]
    pub suppress_hotkey: bool,
    #[serde(default = "d_min_dur")]
    pub min_duration_sec: f64,
    #[serde(default = "d_max_dur")]
    pub max_duration_sec: f64,
    #[serde(default = "d_sample_rate")]
    pub sample_rate: u32,
    #[serde(default)]
    pub microphone: Option<serde_json::Value>,
    #[serde(default)]
    pub recognition_place: String,
    #[serde(default)]
    pub server_urls: Vec<String>,
    #[serde(default)]
    pub server_names: BTreeMap<String, String>,
    #[serde(default)]
    pub preferred_server: String,
    #[serde(default)]
    pub local_device: String,
    #[serde(default = "d_connect_timeout")]
    pub server_connect_timeout_sec: f64,
    #[serde(default)]
    pub dict_share: String,
    #[serde(default)]
    pub dict_share_folder: String,
    #[serde(default = "d_dict_share_name")]
    pub dict_share_name: String,
    #[serde(default = "d_read_timeout")]
    pub server_read_timeout_sec: f64,
    #[serde(default = "d_true")]
    pub local_fallback: bool,
    #[serde(default = "d_local_threads")]
    pub local_num_threads: u32,
    #[serde(default)]
    pub language: String,
    #[serde(default = "d_true")]
    pub itn: bool,
    #[serde(default = "d_dict_path")]
    pub dict_path: String,
    #[serde(default = "d_true")]
    pub restore_clipboard: bool,
    #[serde(default = "d_paste_delay")]
    pub paste_delay_ms: u32,
    #[serde(default = "d_cb_restore_delay")]
    pub clipboard_restore_delay_ms: u32,
    #[serde(default = "d_true")]
    pub show_overlay: bool,
    #[serde(default)]
    pub overlay_position: Option<[i32; 2]>,
    #[serde(default = "d_indicator")]
    pub recording_indicator: String,
    #[serde(default = "d_true")]
    pub mute_other_audio: bool,
    #[serde(default = "d_overlay_place")]
    pub overlay_place: String,
    #[serde(default = "d_true")]
    pub beep_enabled: bool,
    #[serde(default)]
    pub ui_theme: String,
}

impl Default for ClientConfig {
    fn default() -> Self {
        serde_json::from_str("{}").expect("empty object produces default ClientConfig")
    }
}

impl ClientConfig {
    pub fn load(path: &Path) -> Self {
        if !path.exists() {
            let cfg = Self::default();
            let _ = cfg.save(path);
            return cfg;
        }
        let Ok(raw) = fs::read_to_string(path) else {
            return Self::default();
        };
        let trimmed = raw.strip_prefix('\u{feff}').unwrap_or(&raw);
        serde_json::from_str(trimmed).unwrap_or_default()
    }

    pub fn save(&self, path: &Path) -> std::io::Result<()> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut text = serde_json::to_string_pretty(self)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        text.push('\n');
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, text.as_bytes())?;
        fs::rename(&tmp, path)
    }

    pub fn uses_servers(&self) -> bool {
        let place = self.recognition_place.trim();
        if place.is_empty() {
            !self.server_urls.is_empty()
        } else {
            place == "server"
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_and_roundtrip() {
        let tmp = tempfile::tempdir().unwrap();
        let p = tmp.path().join("config.json");
        let cfg = ClientConfig::load(&p);
        assert_eq!(cfg.hotkey, "ralt");
        assert_eq!(cfg.hotkey_mode, "toggle");
        assert!(cfg.strip_trailing_period);
        assert!(!cfg.normalize_text);
        assert!(!cfg.voice_commands);
        assert!(!cfg.undo_voice_command);
        assert!(p.exists());

        let mut cfg2 = cfg.clone();
        cfg2.hotkey = "mouse_x1".into();
        cfg2.server_urls = vec!["http://100.64.0.1:8765".into()];
        cfg2.save(&p).unwrap();
        let loaded = ClientConfig::load(&p);
        assert_eq!(loaded, cfg2);
        assert!(loaded.uses_servers());
    }
}
