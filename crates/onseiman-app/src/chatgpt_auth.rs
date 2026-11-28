//! Sign in with ChatGPT（オープンソース向け ChatGPT Plus / Pro プラン利用）。
//!
//! OpenAI 公式手順（`https://developers.openai.com/siwc/token-sharing-open-source`）に従い、
//! ブラウザで PKCE OAuth 認可 → `127.0.0.1:1455..1458/auth/callback` で認可コード受取 →
//! JWKS で ID トークン RS256 署名検証 → OS 暗号化保存（Windows DPAPI / macOS Keychain）→
//! `POST https://api.openai.com/v1/responses`（`store: false`, `stream: true`）で文章整形を行う。

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use onseiman_core::llm_refine::{
    strip_transcript_tags, wrap_transcript, LlmResult, REFINE_SYSTEM_PROMPT,
};
use onseiman_platform::secret_store;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const AUTHORIZE_URL: &str = "https://auth.openai.com/api/accounts/authorize";
const TOKEN_URL: &str = "https://auth.openai.com/api/accounts/oauth/token";
const DISCOVERY_URL: &str = "https://auth.openai.com/.well-known/openid-configuration";
const JWKS_URL: &str = "https://auth.openai.com/.well-known/jwks.json";
const ISSUER: &str = "https://auth.openai.com";
const RESOURCE: &str = "https://api.openai.com/v1";
const API: &str = "https://api.openai.com/v1";
const SCOPE: &str = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct";
const PLAN_SCOPE: &str = "chatgpt.tokens.use.direct";
const CALLBACK_PATH: &str = "/auth/callback";
const PORTS: [u16; 4] = [1455, 1456, 1457, 1458];
pub const DEFAULT_MODEL: &str = "gpt-5.4-mini";

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct Registration {
    host_id: String,
    client_id: String,
    email: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Tokens {
    access_token: String,
    refresh_token: String,
    id_token: String,
    expires_at: u64,
    scope: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChatGptStatus {
    pub signed_in: bool,
    pub email: String,
    pub plan_usage: bool,
}

fn refresh_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

fn reg_path(root: &Path) -> PathBuf {
    root.join("chatgpt.json")
}

fn tokens_path(root: &Path) -> PathBuf {
    root.join("chatgpt_tokens.enc")
}

fn now_sec() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs())
}

fn shared_agent() -> &'static ureq::Agent {
    static AGENT: OnceLock<ureq::Agent> = OnceLock::new();
    AGENT.get_or_init(|| ureq::AgentBuilder::new().redirects(0).build())
}

fn load_registration(root: &Path) -> Registration {
    std::fs::read_to_string(reg_path(root))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_registration(root: &Path, r: &Registration) -> Result<(), String> {
    let text = serde_json::to_string_pretty(r).map_err(|e| e.to_string())? + "\n";
    let tmp = reg_path(root).with_extension("json.tmp");
    std::fs::write(&tmp, text).map_err(|e| format!("登録情報を保存できません: {e}"))?;
    std::fs::rename(&tmp, reg_path(root)).map_err(|e| format!("登録情報を保存できません: {e}"))
}

fn load_tokens(root: &Path) -> Option<Tokens> {
    let enc = std::fs::read_to_string(tokens_path(root)).ok()?;
    let plain = secret_store::unprotect(enc.trim());
    if plain.is_empty() {
        return None;
    }
    serde_json::from_str(&plain).ok()
}

fn save_tokens(root: &Path, t: &Tokens) -> Result<(), String> {
    let plain = serde_json::to_string(t).map_err(|e| e.to_string())?;
    let enc = secret_store::protect_for("chatgpt_tokens", &plain)?;
    let tmp = tokens_path(root).with_extension("enc.tmp");
    std::fs::write(&tmp, enc).map_err(|e| format!("トークンを保存できません: {e}"))?;
    std::fs::rename(&tmp, tokens_path(root)).map_err(|e| format!("トークンを保存できません: {e}"))
}

fn has_plan_scope(scope: &str) -> bool {
    scope.split_whitespace().any(|s| s == PLAN_SCOPE)
}

pub fn status(root: &Path) -> ChatGptStatus {
    match load_tokens(root) {
        Some(t) => ChatGptStatus {
            signed_in: true,
            email: load_registration(root).email,
            plan_usage: has_plan_scope(&t.scope),
        },
        None => ChatGptStatus {
            signed_in: false,
            email: String::new(),
            plan_usage: false,
        },
    }
}

/// ブラウザで ChatGPT にサインインし、トークンを暗号化保存する（最長 5 分待機）。
pub fn sign_in(root: &Path) -> Result<ChatGptStatus, String> {
    let mut reg = load_registration(root);
    if reg.host_id.is_empty() {
        reg.host_id = format!("urn:uuid:{}", uuid_v4());
        save_registration(root, &reg)?;
    }
    let (listener, port) = PORTS
        .iter()
        .find_map(|p| TcpListener::bind(("127.0.0.1", *p)).ok().map(|l| (l, *p)))
        .ok_or("受け口のポート（1455〜1458）が使われていて、サインインを始められません")?;
    let redirect_uri = format!("http://127.0.0.1:{port}{CALLBACK_PATH}");
    let verifier = URL_SAFE_NO_PAD.encode(random_bytes(32));
    let state = URL_SAFE_NO_PAD.encode(random_bytes(16));
    let nonce = URL_SAFE_NO_PAD.encode(random_bytes(16));
    let first = reg.client_id.is_empty();
    let mut params = vec![
        (
            "client_id",
            if first {
                "dynamic_agent_client".to_string()
            } else {
                reg.client_id.clone()
            },
        ),
        ("ext_agent_host_id", reg.host_id.clone()),
        ("response_type", "code".into()),
        ("redirect_uri", redirect_uri.clone()),
        ("scope", SCOPE.into()),
        ("resource", RESOURCE.into()),
        ("state", state.clone()),
        ("nonce", nonce.clone()),
        ("code_challenge_method", "S256".into()),
        ("code_challenge", pkce_challenge(&verifier)),
    ];
    if first {
        params.push(("agent_name_hint", "OnseiMan".into()));
    } else if !reg.email.is_empty() {
        params.push(("login_hint", reg.email.clone()));
    }
    let url = format!("{AUTHORIZE_URL}?{}", encode_query(&params));
    open_auth_url(&url);

    let query = wait_for_callback(&listener, Duration::from_secs(300))?;
    if query.get("state").map(String::as_str) != Some(state.as_str()) {
        return Err("サインインの応答が一致しません。もう一度お試しください".into());
    }
    if let Some(err) = query.get("error") {
        return Err(if err == "access_denied" {
            "サインインが取り消されました".into()
        } else {
            format!("サインインに失敗しました: {err}")
        });
    }
    let code = query
        .get("code")
        .ok_or("認可コードが返ってきませんでした")?;
    match (first, query.get("client_id")) {
        (true, Some(id)) => reg.client_id = id.clone(),
        (true, None) => return Err("登録が完了しませんでした。もう一度お試しください".into()),
        (false, Some(id)) if *id != reg.client_id => return Err("登録情報が一致しません".into()),
        _ => {}
    }

    let resp: TokenResponse = post_form(&[
        ("grant_type", "authorization_code"),
        ("client_id", &reg.client_id),
        ("code", code),
        ("code_verifier", &verifier),
        ("redirect_uri", &redirect_uri),
        ("resource", RESOURCE),
    ])?;
    let claims = check_id_token(&resp.id_token, &reg.client_id, &nonce)?;
    reg.email = claims.email.unwrap_or_default();
    save_registration(root, &reg)?;
    let tokens = Tokens {
        access_token: resp.access_token,
        refresh_token: resp.refresh_token.unwrap_or_default(),
        id_token: resp.id_token,
        expires_at: now_sec() + resp.expires_in,
        scope: resp.scope.unwrap_or_default(),
    };
    let _g = refresh_lock().lock().unwrap_or_else(|e| e.into_inner());
    save_tokens(root, &tokens)?;
    Ok(status(root))
}

pub fn sign_out(root: &Path) -> Result<(), String> {
    let _g = refresh_lock().lock().unwrap_or_else(|e| e.into_inner());
    if let Some(t) = load_tokens(root) {
        let reg = load_registration(root);
        let endpoint = shared_agent()
            .get(DISCOVERY_URL)
            .timeout(Duration::from_secs(6))
            .call()
            .ok()
            .and_then(|r| r.into_json::<serde_json::Value>().ok())
            .and_then(|v| v["revocation_endpoint"].as_str().map(str::to_string));
        if let Some(url) = endpoint.filter(|u| u.starts_with("https://auth.openai.com/")) {
            let _ = shared_agent()
                .post(&url)
                .timeout(Duration::from_secs(6))
                .send_form(&[
                    ("token", t.refresh_token.as_str()),
                    ("token_type_hint", "refresh_token"),
                    ("client_id", &reg.client_id),
                ]);
        }
    }
    match std::fs::remove_file(tokens_path(root)) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(format!("トークンを消せません: {e}")),
    }
}

fn access_token(root: &Path) -> Result<String, String> {
    let t = load_tokens(root).ok_or("ChatGPT にサインインしていません")?;
    if !has_plan_scope(&t.scope) {
        return Err(
            "ChatGPT プランの利用が許可されていません。設定からサインインし直してください".into(),
        );
    }
    if t.expires_at > now_sec() + 60 {
        return Ok(t.access_token);
    }
    let _g = refresh_lock().lock().unwrap_or_else(|e| e.into_inner());
    let mut latest = load_tokens(root).ok_or("ChatGPT からサインアウトしています")?;
    if latest.expires_at > now_sec() + 60 {
        return Ok(latest.access_token);
    }
    let reg = load_registration(root);
    let resp: TokenResponse = post_form(&[
        ("grant_type", "refresh_token"),
        ("client_id", &reg.client_id),
        ("refresh_token", &latest.refresh_token),
        ("resource", RESOURCE),
    ])
    .map_err(|e| format!("{e}。設定から ChatGPT にサインインし直してください"))?;
    latest.access_token = resp.access_token;
    if let Some(r) = resp.refresh_token {
        latest.refresh_token = r;
    }
    latest.expires_at = now_sec() + resp.expires_in;
    if let Some(s) = resp.scope {
        latest.scope = s;
    }
    save_tokens(root, &latest)?;
    Ok(latest.access_token)
}

pub fn refresh_if_needed(root: &Path) {
    let _ = access_token(root);
}

pub fn list_models(root: &Path) -> Result<Vec<String>, String> {
    let token = access_token(root)?;
    let resp = shared_agent()
        .get(&format!("{API}/models"))
        .timeout(Duration::from_secs(15))
        .set("Authorization", &format!("Bearer {token}"))
        .call()
        .map_err(|e| format!("モデル一覧を取得できません: {e}"))?;
    let v: serde_json::Value = resp.into_json().map_err(|e| e.to_string())?;
    let mut out: Vec<String> = v["models"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|m| m["visibility"] == "list")
        .filter_map(|m| m["slug"].as_str().map(String::from))
        .collect();
    if out.is_empty() {
        out = v["data"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|m| m["id"].as_str().map(String::from))
            .collect();
    }
    if out.is_empty() {
        out.push(DEFAULT_MODEL.into());
    }
    Ok(out)
}

/// Responses API（`store: false`, `stream: true`）で話した文を整える。
pub fn refine_dictation(root: &Path, model: &str, spoken_text: &str) -> Result<LlmResult, String> {
    refine_dictation_with_context(root, model, spoken_text, "")
}

/// 辞書ヒントや貼り付け先文脈付きで Responses API を呼び出す（低遅延のため `reasoning.effort = "minimal"` を優先）。
pub fn refine_dictation_with_context(
    root: &Path,
    model: &str,
    spoken_text: &str,
    extra_instructions: &str,
) -> Result<LlmResult, String> {
    let t0 = Instant::now();
    let token = access_token(root)?;
    let resolved_model = if model.trim().is_empty() {
        DEFAULT_MODEL
    } else {
        model.trim()
    };
    let instructions = if extra_instructions.trim().is_empty() {
        REFINE_SYSTEM_PROMPT.to_string()
    } else {
        format!("{REFINE_SYSTEM_PROMPT}\n{extra_instructions}")
    };
    let wrapped_input = wrap_transcript(spoken_text);
    let send = |with_minimal_reasoning: bool| -> Result<ureq::Response, Box<ureq::Error>> {
        let mut body = serde_json::json!({
            "model": resolved_model,
            "instructions": instructions,
            "input": [{"role": "user", "content": wrapped_input}],
            "store": false,
            "stream": true,
        });
        if with_minimal_reasoning {
            body["reasoning"] = serde_json::json!({ "effort": "minimal" });
        }
        shared_agent()
            .post(&format!("{API}/responses"))
            .timeout(Duration::from_secs(35))
            .set("Authorization", &format!("Bearer {token}"))
            .send_json(body)
            .map_err(Box::new)
    };
    let resp = match send(true) {
        Ok(r) => r,
        Err(e) => match *e {
            ureq::Error::Status(400, _) => match send(false) {
                Ok(r) => r,
                Err(e2) => match *e2 {
                    ureq::Error::Status(code, r) => {
                        let v: serde_json::Value = r.into_json().unwrap_or_default();
                        let err_code = v["error"]["code"].as_str().unwrap_or("");
                        return Err(explain_error(code, err_code));
                    }
                    other => return Err(format!("ChatGPT につながりませんでした: {other}")),
                },
            },
            ureq::Error::Status(code, r) => {
                let v: serde_json::Value = r.into_json().unwrap_or_default();
                let err_code = v["error"]["code"].as_str().unwrap_or("");
                return Err(explain_error(code, err_code));
            }
            other => return Err(format!("ChatGPT につながりませんでした: {other}")),
        },
    };
    let text = read_sse_stream(resp.into_reader())?;
    Ok(LlmResult {
        text: strip_transcript_tags(&text),
        model: resolved_model.to_string(),
        elapsed_ms: t0.elapsed().as_secs_f64() * 1000.0,
    })
}

const MAX_SSE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_SSE_TEXT_BYTES: usize = 512 * 1024;

fn read_sse_stream(reader: impl Read) -> Result<String, String> {
    let mut text = String::new();
    for line in BufReader::new(reader.take(MAX_SSE_BYTES)).lines() {
        let line = line.map_err(|e| format!("応答の読み取りに失敗しました: {e}"))?;
        let Some(data) = line.strip_prefix("data:") else {
            continue;
        };
        let Ok(event) = serde_json::from_str::<serde_json::Value>(data.trim()) else {
            continue;
        };
        match event["type"].as_str().unwrap_or("") {
            "response.output_text.delta" => {
                let delta = event["delta"].as_str().unwrap_or("");
                if text.len().saturating_add(delta.len()) > MAX_SSE_TEXT_BYTES {
                    return Err("応答が長すぎるため中断しました".into());
                }
                text.push_str(delta);
            }
            "response.completed" => return Ok(text),
            "response.failed" => {
                let code = event["response"]["error"]["code"].as_str().unwrap_or("");
                return Err(explain_error(0, code));
            }
            "response.incomplete" => return Err("応答が最後まで返りませんでした".into()),
            _ => {}
        }
    }
    Err("応答が途中で切れました".into())
}

fn explain_error(http: u16, code: &str) -> String {
    match code {
        "subscription_sharing_usage_limit_exceeded" => {
            "ChatGPT プランの利用上限に達しました".into()
        }
        "subscription_sharing_user_not_eligible" => {
            "この ChatGPT プランでは使えません（Plus または Pro が必要です）".into()
        }
        "subscription_sharing_usage_unavailable" | "subscription_sharing_user_unavailable" => {
            "ChatGPT が一時的に使えません".into()
        }
        "subscription_sharing_invalid_user" => "ChatGPT にサインインし直してください".into(),
        "" => format!("ChatGPT のエラー（HTTP {http}）"),
        other => format!("ChatGPT のエラー（{other}）"),
    }
}

fn wait_for_callback(
    listener: &TcpListener,
    timeout: Duration,
) -> Result<HashMap<String, String>, String> {
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let deadline = Instant::now() + timeout;
    loop {
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream.set_nonblocking(false).ok();
                stream.set_read_timeout(Some(Duration::from_secs(5))).ok();
                let mut line = String::new();
                BufReader::new((&mut stream).take(8192))
                    .read_line(&mut line)
                    .ok();
                let target = line.split_whitespace().nth(1).unwrap_or("");
                let (path, query) = target.split_once('?').unwrap_or((target, ""));
                if path != CALLBACK_PATH {
                    let _ = stream.write_all(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    );
                    continue;
                }
                let body = "<!doctype html><meta charset=utf-8><title>OnseiMan</title><body style=\"font-family:sans-serif;padding:3em\"><h2>OnseiMan にサインインしました</h2><p>このタブは閉じてかまいません。</p>";
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                return Ok(parse_query(query));
            }
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                if Instant::now() > deadline {
                    return Err("サインインが 5 分以内に終わりませんでした".into());
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(e) => return Err(format!("サインインの受け口でエラー: {e}")),
        }
    }
}

#[derive(Deserialize)]
struct TokenResponse {
    access_token: String,
    refresh_token: Option<String>,
    id_token: String,
    expires_in: u64,
    scope: Option<String>,
}

#[derive(Deserialize)]
struct IdClaims {
    iss: String,
    aud: serde_json::Value,
    exp: u64,
    nonce: Option<String>,
    sub: String,
    email: Option<String>,
}

fn check_id_token(id_token: &str, client_id: &str, nonce: &str) -> Result<IdClaims, String> {
    use jsonwebtoken::{Algorithm, DecodingKey, Validation};
    let header =
        jsonwebtoken::decode_header(id_token).map_err(|_| "IDトークンのヘッダーが不正です")?;
    if header.alg != Algorithm::RS256 {
        return Err("想定外のIDトークン署名方式です".into());
    }
    let kid = header.kid.ok_or("IDトークンの署名鍵IDがありません")?;
    let jwks: serde_json::Value = shared_agent()
        .get(JWKS_URL)
        .timeout(Duration::from_secs(15))
        .call()
        .map_err(|e| format!("署名鍵を取得できません: {e}"))?
        .into_json()
        .map_err(|e| format!("署名鍵を読めません: {e}"))?;
    let key = jwks["keys"]
        .as_array()
        .and_then(|keys| {
            keys.iter().find(|k| {
                k["kid"].as_str() == Some(kid.as_str())
                    && k["kty"] == "RSA"
                    && k["alg"] == "RS256"
                    && k["use"] == "sig"
            })
        })
        .ok_or("対応するOpenAI署名鍵が見つかりません")?;
    let (n, e) = (
        key["n"].as_str().ok_or("署名鍵が不正です")?,
        key["e"].as_str().ok_or("署名鍵が不正です")?,
    );
    let decoding_key =
        DecodingKey::from_rsa_components(n, e).map_err(|_| "署名鍵を解析できません")?;
    let mut validation = Validation::new(Algorithm::RS256);
    validation.set_audience(&[client_id]);
    validation.set_issuer(&[ISSUER]);
    let claims = jsonwebtoken::decode::<IdClaims>(id_token, &decoding_key, &validation)
        .map_err(|_| "IDトークンの署名・有効期限の検証に失敗しました")?
        .claims;
    let aud_ok = match &claims.aud {
        serde_json::Value::String(a) => a == client_id,
        serde_json::Value::Array(a) => a.iter().any(|v| v == client_id),
        _ => false,
    };
    if claims.iss != ISSUER
        || !aud_ok
        || claims.exp <= now_sec()
        || claims.nonce.as_deref() != Some(nonce)
        || claims.sub.is_empty()
    {
        return Err("ID トークンの検証に失敗しました".into());
    }
    Ok(claims)
}

fn post_form<T: serde::de::DeserializeOwned>(form: &[(&str, &str)]) -> Result<T, String> {
    match shared_agent()
        .post(TOKEN_URL)
        .timeout(Duration::from_secs(15))
        .send_form(form)
    {
        Ok(r) => r
            .into_json()
            .map_err(|e| format!("トークンの応答を読めません: {e}")),
        Err(ureq::Error::Status(code, r)) => {
            let body: serde_json::Value = r.into_json().unwrap_or_default();
            Err(format!(
                "トークンの取得に失敗しました（{code} {}）",
                body["error"].as_str().unwrap_or("")
            ))
        }
        Err(e) => Err(format!("認証サーバーに接続できません: {e}")),
    }
}

fn pkce_challenge(verifier: &str) -> String {
    let digest = Sha256::digest(verifier.as_bytes());
    URL_SAFE_NO_PAD.encode(digest)
}

fn random_bytes(n: usize) -> Vec<u8> {
    let mut buf = vec![0u8; n];
    getrandom::getrandom(&mut buf).expect("CSPRNG failed");
    buf
}

fn uuid_v4() -> String {
    let mut b = random_bytes(16);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

fn percent_encode(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

fn encode_query(params: &[(&str, String)]) -> String {
    params
        .iter()
        .map(|(k, v)| format!("{k}={}", percent_encode(v)))
        .collect::<Vec<_>>()
        .join("&")
}

fn parse_query(q: &str) -> HashMap<String, String> {
    q.split('&')
        .filter_map(|kv| kv.split_once('='))
        .map(|(k, v)| (percent_decode(k), percent_decode(v)))
        .collect()
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'+' => out.push(b' '),
            b'%' if i + 2 < b.len() => {
                if let Some(byte) = std::str::from_utf8(&b[i + 1..i + 3])
                    .ok()
                    .and_then(|h| u8::from_str_radix(h, 16).ok())
                {
                    out.push(byte);
                    i += 2;
                } else {
                    out.push(b'%');
                }
            }
            c => out.push(c),
        }
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn open_auth_url(url: &str) {
    if !url.starts_with("https://auth.openai.com/") {
        return;
    }
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("explorer.exe").arg(url).spawn();
    }
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg(url).spawn();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pkce_matches_rfc7636_example() {
        assert_eq!(
            pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
    }

    #[test]
    fn query_round_trip() {
        let q = encode_query(&[
            ("scope", SCOPE.into()),
            ("redirect_uri", "http://127.0.0.1:1455/auth/callback".into()),
        ]);
        assert!(q.contains("scope=openid%20profile"));
        let back = parse_query("code=a%2Bb&scope=openid+email&state=x");
        assert_eq!(back["code"], "a+b");
        assert_eq!(back["scope"], "openid email");
    }

    #[test]
    fn uuid_is_v4() {
        let u = uuid_v4();
        assert_eq!(u.len(), 36);
        assert_eq!(&u[14..15], "4");
    }

    #[test]
    fn sse_stream_collects_deltas() {
        let sse = "data: {\"type\":\"response.output_text.delta\",\"delta\":\"明日\"}\n\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"です。\"}\n\ndata: {\"type\":\"response.completed\"}\n";
        assert_eq!(read_sse_stream(sse.as_bytes()).unwrap(), "明日です。");
    }
}
