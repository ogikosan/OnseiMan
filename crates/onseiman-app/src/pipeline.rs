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

    if cfg.strip_trailing_period {
        text = strip_trailing_period(&text);
    }

    let mut used_llm = false;
    let mut llm_error = None;
    let want_llm = (force_llm || cfg.llm_auto_refine) && !cfg.llm_provider.trim().is_empty();
    if want_llm && !text.trim().is_empty() {
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
            Ok(refiner) => match refiner.refine_dictation(&text) {
                Ok(res) if !res.text.trim().is_empty() => {
                    text = res.text;
                    used_llm = true;
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
