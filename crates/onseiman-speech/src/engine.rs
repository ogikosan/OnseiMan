//! speech.cpp の worker で Haas-ja を動かす（Python 版の `shared/speechcpp_engine.py` と同じ決まり）。
//!
//! やりとりは https://github.com/nyosegawa/speech.cpp/blob/main/docs/worker.md の protocol 4。
//!
//! - Haas は「1 回に 1 つの発話」を渡す作り。一度に渡す長さは `MAX_SEGMENT_SEC` まで（呼ぶ側が区切って渡す）。
//! - 辞書の優先語は keywords（1 行に 1 語）で渡す。英字の略語は大文字に直す（cpu → CPU）。
//! - worker が落ちたら、1 回だけ起動し直してやり直す。

use std::collections::HashMap;
use std::fmt;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use base64::Engine as _;
use onseiman_core::acronyms::fix_acronyms;
use serde_json::{json, Value};

use crate::setup;

pub const MAX_SEGMENT_SEC: f64 = 15.0;
const START_TIMEOUT: Duration = Duration::from_secs(180);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(60);
/// worker に 1 回で送る量（16kHz・16 ビットで 4 秒分）
const CHUNK_BYTES: usize = 2 * 16000 * 4;

#[derive(Debug)]
pub struct EngineError(pub String);

impl fmt::Display for EngineError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for EngineError {}

/// Mac は Apple の GPU（Metal）が速くて正確。Windows は CPU（内蔵 GPU では文の終わりが欠けることがあった）
pub fn default_device() -> &'static str {
    if cfg!(target_os = "macos") {
        "gpu"
    } else {
        "cpu"
    }
}

/// speech.cpp の keywords（1 行に 1 語。「書き方=読み,読み」）にする。
pub fn keyword_lines(hotwords: &str, readings: &HashMap<String, Vec<String>>) -> Vec<String> {
    let mut out = Vec::new();
    for w in hotwords.split(',').map(str::trim) {
        if w.is_empty() || w.contains('=') || w.contains('\n') {
            continue;
        }
        let rs: Vec<&str> = readings
            .get(w)
            .map(|v| {
                v.iter()
                    .map(|r| r.trim())
                    .filter(|r| !r.is_empty() && !r.contains([',', '=', '\n']))
                    .collect()
            })
            .unwrap_or_default();
        out.push(if rs.is_empty() {
            w.to_string()
        } else {
            format!("{w}={}", rs.join(","))
        });
    }
    out
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<Option<Value>>,
}

impl Worker {
    fn send(&mut self, v: &Value) -> std::io::Result<()> {
        let mut line = serde_json::to_vec(v)?;
        line.push(b'\n');
        self.stdin.write_all(&line)
    }
}

impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

enum RequestError {
    /// worker が落ちた・返事がない（起動し直してやり直す）
    WorkerDied(String),
    /// worker が断った（やり直しても同じ）
    Refused(String),
}

struct Inner {
    worker: Option<Worker>,
    seq: u64,
}

pub struct SpeechEngine {
    root: PathBuf,
    device: String,
    threads: u32,
    /// テスト用: speech.cpp の代わりに動かすもの
    command: Option<Vec<String>>,
    inner: Mutex<Inner>,
}

impl SpeechEngine {
    /// `device`: "cpu" / "gpu"（空ならおまかせ）。`threads`: 0 ならおまかせ。
    pub fn new(root: &Path, device: &str, threads: u32) -> Self {
        Self {
            root: root.to_path_buf(),
            device: if device.is_empty() {
                default_device().to_string()
            } else {
                device.to_string()
            },
            threads,
            command: None,
            inner: Mutex::new(Inner {
                worker: None,
                seq: 0,
            }),
        }
    }

    pub fn with_command(mut self, command: Vec<String>) -> Self {
        self.command = Some(command);
        self
    }

    pub fn model_name(&self) -> &'static str {
        "Haas-ja-v1.0"
    }

    pub fn active_provider(&self) -> String {
        format!("speech.cpp-{}", self.device)
    }

    fn start(&self) -> Result<Worker, EngineError> {
        let cmd: Vec<String> = match &self.command {
            Some(c) => c.clone(),
            None => {
                let exe = setup::find_exe(&self.root);
                let model = setup::model_path(&self.root);
                let (Some(exe), true) = (exe, model.is_file()) else {
                    return Err(EngineError(
                        "speech.cpp か Haas-ja のデータがありません".into(),
                    ));
                };
                let mut c = vec![
                    exe.to_string_lossy().into_owned(),
                    "worker".into(),
                    model.to_string_lossy().into_owned(),
                    "--device".into(),
                    self.device.clone(),
                ];
                if self.threads > 0 {
                    c.push("--threads".into());
                    c.push(self.threads.to_string());
                }
                c
            }
        };
        let mut command = Command::new(&cmd[0]);
        command
            .args(&cmd[1..])
            .env("SPEECH_NO_UPDATE_CHECK", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        let t0 = Instant::now();
        let mut child = command
            .spawn()
            .map_err(|e| EngineError(format!("speech.cpp を起動できませんでした: {e}")))?;
        let stdin = child.stdin.take().expect("stdin");
        let stdout = child.stdout.take().expect("stdout");
        let (tx, rx) = mpsc::channel::<Option<Value>>();
        std::thread::Builder::new()
            .name("speechcpp-reader".into())
            .spawn(move || {
                for line in BufReader::new(stdout).split(b'\n') {
                    let Ok(line) = line else { break };
                    let text = String::from_utf8_lossy(&line);
                    let text = text.trim();
                    if text.is_empty() {
                        continue;
                    }
                    match serde_json::from_str::<Value>(text) {
                        Ok(v) => {
                            if tx.send(Some(v)).is_err() {
                                return;
                            }
                        }
                        Err(_) => eprintln!(
                            "speech.cpp から読めない行が来ました: {}",
                            text.chars().take(200).collect::<String>()
                        ),
                    }
                }
                let _ = tx.send(None); // 終わった印
            })
            .expect("thread");
        let mut worker = Worker {
            child,
            stdin,
            lines: rx,
        };
        let first = match worker.lines.recv_timeout(START_TIMEOUT) {
            Ok(m) => m,
            Err(_) => {
                drop(worker);
                return Err(EngineError("speech.cpp の起動が終わりませんでした".into()));
            }
        };
        match first {
            Some(m) if m.get("type").and_then(Value::as_str) == Some("ready") => {}
            other => {
                let msg = other
                    .as_ref()
                    .and_then(|m| m.pointer("/error/message"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| {
                        other
                            .map(|m| m.to_string())
                            .unwrap_or_else(|| "すぐに止まりました".into())
                    });
                drop(worker);
                return Err(EngineError(format!(
                    "speech.cpp を起動できませんでした: {msg}"
                )));
            }
        }
        eprintln!(
            "speech.cpp の Haas-ja を読み込みました ({:.1} 秒, {})",
            t0.elapsed().as_secs_f64(),
            self.device
        );
        // 起動の返事を待つあいだに stdin を使っていないので、ここまで来れば使える
        let _ = worker.stdin.flush();
        Ok(worker)
    }

    /// 先に読み込んでおく（最初の聞き取りで待たないように）。
    pub fn load(&self) -> Result<(), EngineError> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if !alive(&mut inner.worker) {
            inner.worker = Some(self.start()?);
        }
        Ok(())
    }

    /// 聞き取る。戻り値は (文, かかったミリ秒)。
    ///
    /// `hotwords`: 優先語（コンマ区切り）。`readings`: {書き方: [読み, ...]}。
    pub fn transcribe(
        &self,
        samples: &[f32],
        sample_rate: u32,
        hotwords: &str,
        readings: &HashMap<String, Vec<String>>,
    ) -> Result<(String, f64), EngineError> {
        let mut pcm = Vec::with_capacity(samples.len() * 2);
        for s in samples {
            let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
            pcm.extend_from_slice(&v.to_le_bytes());
        }
        let mut words = keyword_lines(hotwords, readings);
        let t0 = Instant::now();
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        for attempt in 0..3 {
            if !alive(&mut inner.worker) {
                inner.worker = Some(self.start()?);
            }
            inner.seq += 1;
            let rid = format!("r{}", inner.seq);
            let worker = inner.worker.as_mut().expect("worker");
            match request(worker, &rid, &pcm, sample_rate, &words) {
                Ok(Some(text)) => {
                    return Ok((fix_acronyms(&text), t0.elapsed().as_secs_f64() * 1000.0))
                }
                Ok(None) => words.clear(), // 使えない優先語が混ざっていた。優先語なしでもう一度
                Err(RequestError::Refused(msg)) => return Err(EngineError(msg)),
                Err(RequestError::WorkerDied(msg)) => {
                    // worker が落ちた・返事がない: 止めて、1 回だけ起動し直してやり直す
                    eprintln!("speech.cpp をやり直します: {msg}");
                    inner.worker = None;
                    if attempt > 0 {
                        break;
                    }
                }
            }
        }
        Err(EngineError("speech.cpp で聞き取れませんでした".into()))
    }
}

fn alive(worker: &mut Option<Worker>) -> bool {
    match worker {
        Some(w) => matches!(w.child.try_wait(), Ok(None)),
        None => false,
    }
}

/// 1 回分を送って、文を返す。優先語が原因で断られたら None。
fn request(
    worker: &mut Worker,
    rid: &str,
    pcm: &[u8],
    sample_rate: u32,
    words: &[String],
) -> Result<Option<String>, RequestError> {
    let died = |e: std::io::Error| RequestError::WorkerDied(e.to_string());
    let b64 = base64::engine::general_purpose::STANDARD;
    let mut off = 0;
    let mut seq = 0;
    loop {
        let end = (off + CHUNK_BYTES).min(pcm.len());
        worker
            .send(
                &json!({"type": "chunk", "id": rid, "seq": seq, "pcm": b64.encode(&pcm[off..end])}),
            )
            .map_err(died)?;
        off = end;
        seq += 1;
        if off >= pcm.len() {
            break;
        }
    }
    let mut req = json!({"type": "transcribe", "id": rid, "sample_rate": sample_rate});
    if !words.is_empty() {
        req["keywords"] = Value::String(words.join("\n"));
    }
    worker.send(&req).map_err(died)?;
    worker.stdin.flush().map_err(died)?;
    let deadline = Instant::now() + REQUEST_TIMEOUT;
    loop {
        let wait = deadline
            .saturating_duration_since(Instant::now())
            .max(Duration::from_millis(100));
        let msg = match worker.lines.recv_timeout(wait) {
            Ok(Some(m)) => m,
            Ok(None) | Err(RecvTimeoutError::Disconnected) => {
                return Err(RequestError::WorkerDied("speech.cpp が止まりました".into()))
            }
            Err(RecvTimeoutError::Timeout) => {
                return Err(RequestError::WorkerDied(
                    "speech.cpp から返事がありません".into(),
                ))
            }
        };
        let kind = msg.get("type").and_then(Value::as_str).unwrap_or("");
        if msg.get("id").and_then(Value::as_str) != Some(rid) || kind == "progress" {
            continue; // ほかの返事・進み具合
        }
        if kind == "end" {
            return Ok(Some(
                msg.get("text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string(),
            ));
        }
        let err_msg = msg
            .pointer("/error/message")
            .and_then(Value::as_str)
            .unwrap_or(kind)
            .to_string();
        if !words.is_empty()
            && msg.pointer("/error/option").and_then(Value::as_str) == Some("keywords")
        {
            eprintln!("優先語を使えませんでした: {err_msg}");
            return Ok(None);
        }
        return Err(RequestError::Refused(format!("speech.cpp: {err_msg}")));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keywords() {
        let mut r = HashMap::new();
        r.insert(
            "くっきり版".to_string(),
            vec![
                "クッキリバン".to_string(),
                " ".to_string(),
                "a,b".to_string(),
            ],
        );
        assert_eq!(
            keyword_lines("くっきり版, RTX ,,a=b,OnseiMan", &r),
            vec![
                "くっきり版=クッキリバン".to_string(),
                "RTX".to_string(),
                "OnseiMan".to_string()
            ]
        );
        assert!(keyword_lines("", &r).is_empty());
    }
}
