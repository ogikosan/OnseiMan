//! 認識後のテキスト整形パイプライン（言いよどみ・辞書・正規化・スニペット・音声コマンド・AI 仕上げ）。

use onseiman_core::config::ClientConfig;
use onseiman_core::dictionary::{strip_trailing_period, CustomDictionary};
use onseiman_core::llm_refine::LlmRefiner;
use onseiman_core::voice_commands::apply_voice_commands;
use onseiman_platform::secret_store;

#[derive(Debug, Clone)]
pub struct FormatOutput {
    pub text: String,
    pub used_llm: bool,
    pub llm_error: Option<String>,
}

/// 1 発話のテキストを設定と辞書に従って整形する。
pub fn format_utterance(
    raw: &str,
    cfg: &ClientConfig,
    dict: &CustomDictionary,
    force_llm: bool,
) -> FormatOutput {
    format_utterance_with_root(raw, cfg, dict, force_llm, None)
}

const CHAT_APPS: &[&str] = &[
    "slack.exe",
    "discord.exe",
    "teams.exe",
    "ms-teams.exe",
    "line.exe",
    "chatwork.exe",
    "mattermost.exe",
    "telegram.exe",
];

fn is_chat_app(app: Option<&str>) -> bool {
    app.map(|a| {
        let lower = a.to_ascii_lowercase();
        CHAT_APPS.iter().any(|&c| c == lower)
    })
    .unwrap_or(false)
}

fn build_llm_context_hint(dict: &CustomDictionary, target_app: Option<&str>) -> String {
    let mut parts = Vec::new();
    let mut terms: Vec<String> = Vec::new();
    for w in dict.active_hotwords() {
        if !w.is_empty() && !terms.contains(&w) {
            terms.push(w);
        }
        if terms.len() >= 40 {
            break;
        }
    }
    for (_, v) in dict.replacements() {
        let t = v.trim().to_string();
        if !t.is_empty() && !terms.contains(&t) {
            terms.push(t);
        }
        if terms.len() >= 60 {
            break;
        }
    }
    if !terms.is_empty() {
        parts.push(format!(
            "以下の用語・固有名詞は、文脈に合う場合この表記を優先してください: {}",
            terms.join("、")
        ));
    }
    if is_chat_app(target_app) {
        parts.push("チャットアプリ向けの自然な短文にし、文末の「。」は省いてください。".into());
    }
    parts.join("\n")
}

/// AI 仕上げ後のテキストから改行・制御文字（CR/LF/ESC/C0/C1/U+2028/U+2029）を除去し上限長へ収める。
/// （ターミナル等への自動貼り付けで Enter やエスケープシーケンスが誤送信されるのを防ぐ）
fn sanitize_llm_output(s: &str) -> String {
    const MAX_LLM_CHARS: usize = 5000;
    let mut out = String::with_capacity(s.len().min(MAX_LLM_CHARS * 4));
    let mut prev_space = false;
    let mut count = 0usize;
    for c in s.chars() {
        let code = c as u32;
        if code <= 0x1F || (0x7F..=0x9F).contains(&code) || matches!(c, '\u{2028}' | '\u{2029}') {
            if !prev_space && !out.is_empty() {
                out.push(' ');
                prev_space = true;
                count += 1;
            }
        } else {
            out.push(c);
            prev_space = c == ' ';
            count += 1;
        }
        if count >= MAX_LLM_CHARS {
            break;
        }
    }
    out.trim().to_string()
}

/// 1 発話のテキストを設定と辞書に従って整形する（ChatGPT サインイン利用時は `root_dir` を渡す）。
pub fn format_utterance_with_root(
    raw: &str,
    cfg: &ClientConfig,
    dict: &CustomDictionary,
    force_llm: bool,
    root_dir: Option<&std::path::Path>,
) -> FormatOutput {
    format_utterance_full(raw, cfg, dict, force_llm, root_dir, None)
}

/// 1 発話のテキストを設定・辞書・貼り付け先アプリ情報に従って整形する。
pub fn format_utterance_full(
    raw: &str,
    cfg: &ClientConfig,
    dict: &CustomDictionary,
    force_llm: bool,
    root_dir: Option<&std::path::Path>,
    target_app: Option<&str>,
) -> FormatOutput {
    let mut text = raw.trim().to_string();
    if text.is_empty() {
        return FormatOutput {
            text,
            used_llm: false,
            llm_error: None,
        };
    }

    if cfg.remove_fillers {
        text = dict.remove_fillers(&text);
    }

    text = dict.apply_replacements(&text);

    if cfg.normalize_text {
        text = dict.normalize(&text);
    }

    let (expanded, _matched) = dict.apply_snippets(&text);
    text = expanded;

    if cfg.voice_commands {
        text = apply_voice_commands(&text);
    }

    if cfg.strip_trailing_period || (cfg.app_specific_rules && is_chat_app(target_app)) {
        text = strip_trailing_period(&text);
    }

    let mut used_llm = false;
    let mut llm_error = None;
    let want_llm = (force_llm || cfg.llm_auto_refine) && !cfg.llm_provider.trim().is_empty();
    if want_llm && !text.trim().is_empty() {
        let context_hint = build_llm_context_hint(dict, target_app);
        if cfg.llm_provider == "chatgpt" {
            if let Some(root) = root_dir {
                match crate::chatgpt_auth::refine_dictation_with_context(
                    root,
                    &cfg.llm_model,
                    &text,
                    &context_hint,
                ) {
                    Ok(res) if !res.text.trim().is_empty() => {
                        let cleaned = sanitize_llm_output(&res.text);
                        if !cleaned.is_empty() {
                            text = cleaned;
                            used_llm = true;
                        }
                    }
                    Ok(_) => {}
                    Err(e) => {
                        llm_error = Some(e);
                    }
                }
            }
        } else {
            let stored = cfg
                .llm_api_keys
                .get(&cfg.llm_provider)
                .cloned()
                .unwrap_or_default();
            let api_key = secret_store::unprotect(&stored);
            match LlmRefiner::new(
                &cfg.llm_provider,
                &cfg.llm_model,
                &api_key,
                &cfg.llm_base_url,
                20.0,
            ) {
                Ok(refiner) => match refiner.refine_dictation_with_context(&text, &context_hint) {
                    Ok(res) if !res.text.trim().is_empty() => {
                        let cleaned = sanitize_llm_output(&res.text);
                        if !cleaned.is_empty() {
                            text = cleaned;
                            used_llm = true;
                        }
                    }
                    Ok(_) => {}
                    Err(e) => {
                        llm_error = Some(e);
                    }
                },
                Err(e) => {
                    llm_error = Some(e);
                }
            }
        }
    }

    FormatOutput {
        text,
        used_llm,
        llm_error,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn pipeline_removes_fillers_and_applies_dict() {
        let cfg = ClientConfig {
            remove_fillers: true,
            strip_trailing_period: true,
            ..Default::default()
        };
        let dict = CustomDictionary::from_value(&json!({
            "replacements": { "おんせいまん": "OnseiMan" }
        }));

        let out = format_utterance("えーとおんせいまんです。", &cfg, &dict, false);
        assert_eq!(out.text, "OnseiManです");
        assert!(!out.used_llm);
    }
}
