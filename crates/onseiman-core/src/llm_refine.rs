//! AI で文章を整える・選択文を書き換える（Python 版 `client/llm_refine.py` と同じ決まり）。
//!
//! OpenAI / Anthropic / Gemini / Ollama（または OpenAI 互換 API）の 4 つに対応。
//! クラウド API ではリダイレクトを追わない（API キーの流出防止）＋ Gemini のモデル名を URL エンコードする。

use std::time::{Duration, Instant};

use serde_json::{json, Value};

pub const OPENAI_URL: &str = "https://api.openai.com/v1";
pub const ANTHROPIC_URL: &str = "https://api.anthropic.com/v1";
pub const ANTHROPIC_VERSION: &str = "2023-06-01";
pub const GEMINI_URL: &str = "https://generativelanguage.googleapis.com/v1beta";

pub const REFINE_SYSTEM_PROMPT: &str = "あなたは日本語の音声入力テキストを整えるエディターです。\
言い直し（例:「明日、あいや明後日」→「明後日」）、重複、口癖を自然に整理し、\
元の意味を保った読みやすい文章にしてください。\
前置き・解説・引用符・マークダウンの囲みは一切付けず、整形後の本文だけを出力してください。";

pub const REWRITE_SYSTEM_PROMPT: &str = "あなたはテキスト編集アシスタントです。\
ユーザーが選択した元テキストを、音声の指示に従って書き換えてください。\
前置き・解説・引用符・余計なマークダウン囲みは一切付けず、書き換え後のテキストだけを出力してください。";

pub const TEST_TEXT: &str = "えーと、明日の会議は、あ、じゃなくて明後日の3時からでお願いします";

#[derive(Debug, Clone, PartialEq)]
pub struct LlmResult {
    pub text: String,
    pub model: String,
    pub elapsed_ms: f64,
}

pub fn default_model(provider: &str) -> &'static str {
    match provider {
        "openai" => "gpt-5.4-mini",
        "anthropic" => "claude-haiku-5-5",
        "gemini" => "gemini-3.5-flash-lite",
        _ => "",
    }
}

/// パス成分として安全にエンコードする（`urllib.parse.quote(s, safe="")` 相当）。
pub fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

pub struct LlmRefiner {
    pub provider: String,
    pub model: String,
    pub api_key: String,
    pub base_url: String,
    pub timeout: Duration,
}

impl LlmRefiner {
    pub fn new(
        provider: &str,
        model: &str,
        api_key: &str,
        base_url: &str,
        timeout_sec: f64,
    ) -> Result<Self, String> {
        if !matches!(provider, "openai" | "anthropic" | "gemini" | "ollama") {
            return Err("AI が選ばれていません（設定の「AI で整える」で選んでください）".into());
        }
        let m = model.trim();
        let resolved_model = if m.is_empty() {
            default_model(provider).to_string()
        } else {
            m.to_string()
        };
        let key = api_key.trim().to_string();
        let base = base_url.trim().trim_end_matches('/').to_string();
        if matches!(provider, "openai" | "anthropic" | "gemini") && key.is_empty() {
            return Err("API キーが入っていません（設定の「AI で整える」で入れてください）".into());
        }
        if provider == "ollama" && base.is_empty() {
            return Err(
                "AI のアドレスが入っていません（設定の「AI で整える」で入れてください）".into(),
            );
        }
        if !base.is_empty() {
            let lower = base.to_ascii_lowercase();
            if !(lower.starts_with("http://") || lower.starts_with("https://")) {
                return Err("AI のアドレスは http:// または https:// で始めてください".into());
            }
        }
        Ok(Self {
            provider: provider.to_string(),
            model: resolved_model,
            api_key: key,
            base_url: base,
            timeout: Duration::from_secs_f64(timeout_sec.clamp(1.0, 120.0)),
        })
    }

    fn agent(&self) -> ureq::Agent {
        // リダイレクト先へ API キーや本文が送られないよう全プロバイダでリダイレクトを止める
        ureq::AgentBuilder::new()
            .timeout(self.timeout)
            .redirects(0)
            .build()
    }

    fn post_json(
        &self,
        url: &str,
        payload: Value,
        headers: &[(&str, &str)],
    ) -> Result<Value, String> {
        let mut req = self
            .agent()
            .post(url)
            .set("Content-Type", "application/json");
        for &(k, v) in headers {
            req = req.set(k, v);
        }
        match req.send_json(payload) {
            Ok(resp) => resp
                .into_json::<Value>()
                .map_err(|e| format!("AI の返事を読めませんでした: {e}")),
            Err(ureq::Error::Status(code, resp)) => Err(http_error(code, resp, &self.api_key)),
            Err(ureq::Error::Transport(e)) => Err(format!(
                "AI につながりませんでした: {}",
                e.message().unwrap_or("接続エラー")
            )),
        }
    }

    fn own_is_ollama(&self) -> bool {
        if self.base_url.ends_with("/v1") || self.base_url.contains("/v1/") {
            return false;
        }
        let url = format!("{}/api/tags", self.base_url);
        ureq::AgentBuilder::new()
            .timeout(Duration::from_secs(2))
            .redirects(0)
            .build()
            .get(&url)
            .call()
            .map(|r| r.status() == 200)
            .unwrap_or(false)
    }

    pub fn refine_dictation(&self, spoken_text: &str) -> Result<LlmResult, String> {
        self.chat(REFINE_SYSTEM_PROMPT, spoken_text)
    }

    /// 辞書ヒントや貼り付け先アプリの文脈を追加して音声入力文を整える。
    pub fn refine_dictation_with_context(
        &self,
        spoken_text: &str,
        extra_instructions: &str,
    ) -> Result<LlmResult, String> {
        if extra_instructions.trim().is_empty() {
            return self.chat(REFINE_SYSTEM_PROMPT, spoken_text);
        }
        let sys = format!("{REFINE_SYSTEM_PROMPT}\n{extra_instructions}");
        self.chat(&sys, spoken_text)
    }

    pub fn rewrite_selection(
        &self,
        selected_text: &str,
        instruction: &str,
    ) -> Result<LlmResult, String> {
        let prompt = format!("【元のテキスト】\n{selected_text}\n\n【指示】\n{instruction}");
        self.chat(REWRITE_SYSTEM_PROMPT, &prompt)
    }

    fn chat(&self, system_prompt: &str, user_content: &str) -> Result<LlmResult, String> {
        let t0 = Instant::now();
        let model = self.model.clone();
        let text = match self.provider.as_str() {
            "openai" => {
                let auth = format!("Bearer {}", self.api_key);
                let data = self.post_json(
                    &format!("{OPENAI_URL}/chat/completions"),
                    json!({
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content}
                        ]
                    }),
                    &[("Authorization", &auth)],
                )?;
                data["choices"][0]["message"]["content"]
                    .as_str()
                    .unwrap_or("")
                    .to_string()
            }
            "anthropic" => {
                let data = self.post_json(
                    &format!("{ANTHROPIC_URL}/messages"),
                    json!({
                        "model": model,
                        "max_tokens": 2048,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_content}]
                    }),
                    &[
                        ("x-api-key", &self.api_key),
                        ("anthropic-version", ANTHROPIC_VERSION),
                    ],
                )?;
                data["content"]
                    .as_array()
                    .map(|arr| {
                        arr.iter()
                            .filter(|b| b["type"].as_str() == Some("text"))
                            .filter_map(|b| b["text"].as_str())
                            .collect::<String>()
                    })
                    .unwrap_or_default()
            }
            "gemini" => {
                let url = format!(
                    "{GEMINI_URL}/models/{}:generateContent",
                    percent_encode(&model)
                );
                let data = self.post_json(
                    &url,
                    json!({
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_content}]}]
                    }),
                    &[("x-goog-api-key", &self.api_key)],
                )?;
                data["candidates"][0]["content"]["parts"]
                    .as_array()
                    .map(|parts| {
                        parts
                            .iter()
                            .filter(|p| !p["thought"].as_bool().unwrap_or(false))
                            .filter_map(|p| p["text"].as_str())
                            .collect::<String>()
                    })
                    .unwrap_or_default()
            }
            _ => {
                let auth = format!("Bearer {}", self.api_key);
                let hdrs: Vec<(&str, &str)> = if self.api_key.is_empty() {
                    Vec::new()
                } else {
                    vec![("Authorization", auth.as_str())]
                };
                let messages = json!([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]);
                if self.own_is_ollama() {
                    let data = self.post_json(
                        &format!("{}/api/chat", self.base_url),
                        json!({
                            "model": model,
                            "stream": false,
                            "think": false,
                            "keep_alive": "30m",
                            "messages": messages,
                            "options": {"temperature": 0.1}
                        }),
                        &hdrs,
                    )?;
                    data["message"]["content"]
                        .as_str()
                        .unwrap_or("")
                        .to_string()
                } else {
                    let v1 = if self.base_url.ends_with("/v1") {
                        self.base_url.clone()
                    } else {
                        format!("{}/v1", self.base_url)
                    };
                    let data = self.post_json(
                        &format!("{v1}/chat/completions"),
                        json!({
                            "model": model,
                            "temperature": 0.2,
                            "messages": messages,
                            "chat_template_kwargs": { "enable_thinking": false }
                        }),
                        &hdrs,
                    )?;
                    data["choices"][0]["message"]["content"]
                        .as_str()
                        .unwrap_or("")
                        .to_string()
                }
            }
        };
        Ok(LlmResult {
            text: text.trim().to_string(),
            model,
            elapsed_ms: t0.elapsed().as_secs_f64() * 1000.0,
        })
    }
}

fn http_error(code: u16, resp: ureq::Response, api_key: &str) -> String {
    let raw = resp.into_string().unwrap_or_default();
    let mut detail = serde_json::from_str::<Value>(&raw)
        .ok()
        .and_then(|v| {
            v.get("error")
                .and_then(|e| {
                    e.get("message")
                        .and_then(Value::as_str)
                        .or_else(|| e.as_str())
                })
                .map(String::from)
        })
        .unwrap_or_else(|| raw.chars().take(200).collect());
    // エラー文に API キーが映り込んでも画面に出さない
    if !api_key.is_empty() {
        detail = detail.replace(api_key, "***");
    }
    let detail: String = detail.chars().take(200).collect();
    let msg =
        if code == 401 || code == 403 || (code == 400 && detail.to_lowercase().contains("api key"))
        {
            "API キーが正しくありません（作り直したキーを入れてください）"
        } else if code == 404 {
            "モデル名が見つかりません（「モデルを読み込む」で選び直してください）"
        } else if code == 429 {
            "使える量の上限です（しばらく待つか、その会社のページで支払いの設定を確かめてください）"
        } else if code >= 500 {
            "AI の会社の側でエラーが起きています（しばらくしてからもう一度）"
        } else {
            "AI に断られました"
        };
    if detail.is_empty() {
        format!("{msg}（{code}）")
    } else {
        format!("{msg}：{detail}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percent_encode_escapes_path_traversal() {
        assert_eq!(
            percent_encode("gemini-2.5/../../other?x=1"),
            "gemini-2.5%2F..%2F..%2Fother%3Fx%3D1"
        );
    }

    #[test]
    fn validation_errors() {
        assert!(LlmRefiner::new("unknown", "", "", "", 10.0).is_err());
        assert!(LlmRefiner::new("openai", "", "", "", 10.0).is_err());
        assert!(LlmRefiner::new("ollama", "", "", "", 10.0).is_err());
        assert!(LlmRefiner::new("openai", "", "sk-test", "", 10.0).is_ok());
    }
}
