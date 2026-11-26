//! マイク録音（cpal による Windows WASAPI / macOS CoreAudio 共通実装）。
//!
//! `keep_mic_open` が有効なときは常時待機ストリーム＋ 0.4 秒のプリロール（リングバッファ）を維持し、
//! キーを押す直前・直後の言い出し一音の欠落や WASAPI 起動待ち（200〜350ms）をなくす。

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, Stream};

const PREROLL_SEC: f64 = 0.4;

/// 利用可能な入力デバイス（マイク）の名前一覧。
pub fn list_input_devices() -> Vec<String> {
    let host = cpal::default_host();
    let Ok(devices) = host.input_devices() else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for dev in devices {
        if let Ok(name) = dev.name() {
            if !name.is_empty() && !out.contains(&name) {
                out.push(name);
            }
        }
    }
    out
}

/// 任意のサンプルレートを 16kHz へアンチエイリアス付きで変換する。
/// ダウンサンプリング時（例: 48kHz→16kHz）は出力サンプル区間を平均化して折り返し雑音を防ぐ。
pub fn resample_linear(samples: &[f32], src_rate: u32, dst_rate: u32) -> Vec<f32> {
    if samples.is_empty() || src_rate == 0 || dst_rate == 0 || src_rate == dst_rate {
        return samples.to_vec();
    }
    let ratio = src_rate as f64 / dst_rate as f64;
    let out_len = ((samples.len() as f64) / ratio).round().max(1.0) as usize;
    let mut out = Vec::with_capacity(out_len);
    if ratio > 1.0 {
        for i in 0..out_len {
            let start = i as f64 * ratio;
            let end = ((i + 1) as f64 * ratio).min(samples.len() as f64);
            let s0 = start.floor() as usize;
            let s1 = end.ceil() as usize;
            let mut acc = 0.0f64;
            let mut wsum = 0.0f64;
            let hi_idx = s1.min(samples.len());
            for (off, &sample) in samples[s0..hi_idx].iter().enumerate() {
                let j = s0 + off;
                let lo = (j as f64).max(start);
                let hi = ((j + 1) as f64).min(end);
                let w = (hi - lo).max(0.0);
                if w > 0.0 {
                    acc += sample as f64 * w;
                    wsum += w;
                }
            }
            out.push(if wsum > 0.0 { (acc / wsum) as f32 } else { 0.0 });
        }
    } else {
        for i in 0..out_len {
            let pos = i as f64 * ratio;
            let idx = pos.floor() as usize;
            let frac = (pos - idx as f64) as f32;
            let a = samples[idx.min(samples.len() - 1)];
            let b = samples[(idx + 1).min(samples.len() - 1)];
            out.push(a + (b - a) * frac);
        }
    }
    out
}

type LevelCb = Arc<dyn Fn(f32) + Send + Sync>;

struct SharedAudio {
    recording: AtomicBool,
    active_buf: Mutex<Vec<f32>>,
    ring: Mutex<VecDeque<f32>>,
    on_level: Mutex<Option<LevelCb>>,
    last_cb: Mutex<Instant>,
    src_rate: u32,
}

enum Cmd {
    WarmUp {
        device_name: Option<String>,
        reply: mpsc::Sender<Result<(), String>>,
    },
    Start {
        device_name: Option<String>,
        keep_open: bool,
        on_level: Option<LevelCb>,
        reply: mpsc::Sender<Result<Arc<SharedAudio>, String>>,
    },
    Stop {
        keep_open: bool,
        reply: mpsc::Sender<Vec<f32>>,
    },
}

struct StreamSlot {
    _stream: Stream,
    device_name: Option<String>,
    shared: Arc<SharedAudio>,
}

fn open_cpal_stream(device_name: Option<&str>) -> Result<StreamSlot, String> {
    let host = cpal::default_host();
    let device = match device_name.filter(|s| !s.is_empty()) {
        Some(want) => host
            .input_devices()
            .map_err(|e| format!("マイク一覧を取得できませんでした: {e}"))?
            .find(|d| d.name().ok().as_deref() == Some(want))
            .or_else(|| host.default_input_device()),
        None => host.default_input_device(),
    }
    .ok_or_else(|| "使えるマイクが見つかりません".to_string())?;

    let config = device
        .default_input_config()
        .map_err(|e| format!("マイクの設定を取得できませんでした: {e}"))?;
    let src_rate = config.sample_rate().0;
    let channels = config.channels() as usize;
    let preroll_cap = ((src_rate as f64) * PREROLL_SEC).round() as usize;

    let shared = Arc::new(SharedAudio {
        recording: AtomicBool::new(false),
        active_buf: Mutex::new(Vec::with_capacity(src_rate as usize * 10)),
        ring: Mutex::new(VecDeque::with_capacity(preroll_cap + 2048)),
        on_level: Mutex::new(None),
        last_cb: Mutex::new(Instant::now()),
        src_rate,
    });
    let sh_cb = Arc::clone(&shared);

    let push_mono = move |mono: &[f32]| {
        if mono.is_empty() {
            return;
        }
        if let Ok(mut t) = sh_cb.last_cb.lock() {
            *t = Instant::now();
        }
        if sh_cb.recording.load(Ordering::Acquire) {
            let cb_opt = sh_cb.on_level.lock().ok().and_then(|g| g.clone());
            if let Some(cb) = cb_opt {
                let sum: f32 = mono.iter().map(|x| x * x).sum();
                let rms = (sum / mono.len() as f32).sqrt();
                cb((rms * 10.0).min(1.0));
            }
            if let Ok(mut g) = sh_cb.active_buf.lock() {
                g.extend_from_slice(mono);
            }
        } else if preroll_cap > 0 {
            if let Ok(mut r) = sh_cb.ring.lock() {
                r.extend(mono.iter().copied());
                while r.len() > preroll_cap {
                    r.pop_front();
                }
            }
        }
    };

    let err_fn = |err| {
        let _ = err;
    };
    let stream_cfg = config.config();
    let stream = match config.sample_format() {
        SampleFormat::F32 => device.build_input_stream(
            &stream_cfg,
            move |data: &[f32], _| {
                let mono: Vec<f32> = data
                    .chunks(channels.max(1))
                    .map(|c| c.iter().sum::<f32>() / c.len() as f32)
                    .collect();
                push_mono(&mono);
            },
            err_fn,
            None,
        ),
        SampleFormat::I16 => device.build_input_stream(
            &stream_cfg,
            move |data: &[i16], _| {
                let mono: Vec<f32> = data
                    .chunks(channels.max(1))
                    .map(|c| c.iter().map(|&v| v as f32 / 32768.0).sum::<f32>() / c.len() as f32)
                    .collect();
                push_mono(&mono);
            },
            err_fn,
            None,
        ),
        SampleFormat::U16 => device.build_input_stream(
            &stream_cfg,
            move |data: &[u16], _| {
                let mono: Vec<f32> = data
                    .chunks(channels.max(1))
                    .map(|c| {
                        c.iter()
                            .map(|&v| (v as f32 - 32768.0) / 32768.0)
                            .sum::<f32>()
                            / c.len() as f32
                    })
                    .collect();
                push_mono(&mono);
            },
            err_fn,
            None,
        ),
        other => return Err(format!("未対応のマイク形式です: {other:?}")),
    }
    .map_err(|e| format!("マイクを開けませんでした: {e}"))?;

    stream
        .play()
        .map_err(|e| format!("録音を開始できませんでした: {e}"))?;

    Ok(StreamSlot {
        _stream: stream,
        device_name: device_name.map(String::from),
        shared,
    })
}

fn manager_tx() -> &'static Mutex<mpsc::Sender<Cmd>> {
    static TX: OnceLock<Mutex<mpsc::Sender<Cmd>>> = OnceLock::new();
    TX.get_or_init(|| {
        let (tx, rx) = mpsc::channel::<Cmd>();
        thread::spawn(move || {
            let mut slot: Option<StreamSlot> = None;
            while let Ok(cmd) = rx.recv() {
                match cmd {
                    Cmd::WarmUp { device_name, reply } => {
                        let reusable = slot.as_ref().is_some_and(|s| {
                            s.device_name == device_name
                                && s.shared
                                    .last_cb
                                    .lock()
                                    .map(|t| t.elapsed() < Duration::from_millis(1500))
                                    .unwrap_or(false)
                        });
                        if reusable {
                            let _ = reply.send(Ok(()));
                        } else {
                            slot = None;
                            match open_cpal_stream(device_name.as_deref()) {
                                Ok(s) => {
                                    slot = Some(s);
                                    let _ = reply.send(Ok(()));
                                }
                                Err(e) => {
                                    let _ = reply.send(Err(e));
                                }
                            }
                        }
                    }
                    Cmd::Start {
                        device_name,
                        keep_open,
                        on_level,
                        reply,
                    } => {
                        let reusable = keep_open
                            && slot.as_ref().is_some_and(|s| {
                                s.device_name == device_name
                                    && s.shared
                                        .last_cb
                                        .lock()
                                        .map(|t| t.elapsed() < Duration::from_millis(1500))
                                        .unwrap_or(false)
                            });
                        if !reusable {
                            slot = None;
                            match open_cpal_stream(device_name.as_deref()) {
                                Ok(s) => slot = Some(s),
                                Err(e) => {
                                    let _ = reply.send(Err(e));
                                    continue;
                                }
                            }
                        }
                        if let Some(ref s) = slot {
                            // プリロール（直前 0.4 秒）を先頭に引き継いで録音開始
                            let preroll: Vec<f32> = if reusable {
                                s.shared
                                    .ring
                                    .lock()
                                    .map(|mut r| r.drain(..).collect())
                                    .unwrap_or_default()
                            } else {
                                if let Ok(mut r) = s.shared.ring.lock() {
                                    r.clear();
                                }
                                Vec::new()
                            };
                            if let Ok(mut ab) = s.shared.active_buf.lock() {
                                ab.clear();
                                ab.extend_from_slice(&preroll);
                            }
                            if let Ok(mut cb) = s.shared.on_level.lock() {
                                *cb = on_level;
                            }
                            s.shared.recording.store(true, Ordering::Release);
                            let _ = reply.send(Ok(Arc::clone(&s.shared)));
                        }
                    }
                    Cmd::Stop { keep_open, reply } => {
                        let out = if let Some(ref s) = slot {
                            s.shared.recording.store(false, Ordering::Release);
                            if let Ok(mut cb) = s.shared.on_level.lock() {
                                *cb = None;
                            }
                            let raw = s
                                .shared
                                .active_buf
                                .lock()
                                .map(|mut ab| std::mem::take(&mut *ab))
                                .unwrap_or_default();
                            let sr = s.shared.src_rate;
                            if !keep_open {
                                slot = None;
                            }
                            resample_linear(&raw, sr, 16000)
                        } else {
                            Vec::new()
                        };
                        let _ = reply.send(out);
                    }
                }
            }
        });
        Mutex::new(tx)
    })
}

pub struct RecordingHandle {
    shared: Arc<SharedAudio>,
    keep_open: bool,
    stopped: bool,
}

impl RecordingHandle {
    /// これまでに録音した 16kHz 相当の長さを秒で返す。
    pub fn elapsed_sec(&self) -> f64 {
        if self.shared.src_rate == 0 {
            return 0.0;
        }
        let n = self.shared.active_buf.lock().map(|g| g.len()).unwrap_or(0);
        n as f64 / self.shared.src_rate as f64
    }

    /// 録音バッファの先頭から現在までを取り出し、16kHz モノラルとして返す（ライブ分割用）。
    pub fn drain_16k(&self) -> Vec<f32> {
        let raw = self
            .shared
            .active_buf
            .lock()
            .map(|mut g| std::mem::take(&mut *g))
            .unwrap_or_default();
        resample_linear(&raw, self.shared.src_rate, 16000)
    }

    /// 録音を止め、16kHz モノラルの f32 サンプルを返す。
    pub fn stop(mut self) -> Vec<f32> {
        self.stopped = true;
        let (tx, rx) = mpsc::channel();
        if let Ok(mtx) = manager_tx().lock() {
            let _ = mtx.send(Cmd::Stop {
                keep_open: self.keep_open,
                reply: tx,
            });
        }
        rx.recv().unwrap_or_default()
    }
}

impl Drop for RecordingHandle {
    fn drop(&mut self) {
        if !self.stopped {
            let (tx, rx) = mpsc::channel();
            if let Ok(mtx) = manager_tx().lock() {
                let _ = mtx.send(Cmd::Stop {
                    keep_open: self.keep_open,
                    reply: tx,
                });
            }
            let _ = rx.recv_timeout(Duration::from_secs(1));
        }
    }
}

/// マイクを待機状態で開いておき、直前 0.4 秒のプリロール録音を有効にする。
pub fn warm_mic(device_name: Option<&str>) -> Result<(), String> {
    let (tx, rx) = mpsc::channel();
    manager_tx()
        .lock()
        .map_err(|_| "マイク管理スレッドが応答しません".to_string())?
        .send(Cmd::WarmUp {
            device_name: device_name.filter(|s| !s.is_empty()).map(String::from),
            reply: tx,
        })
        .map_err(|_| "マイク管理スレッドに送信できませんでした".to_string())?;
    rx.recv()
        .map_err(|_| "マイク待機開始の応答がありませんでした".to_string())?
}

/// 録音を開始する（`keep_open = true` のときは常時待機ストリーム＋0.4秒プリロールを使う）。
pub fn start_recording_ex(
    device_name: Option<&str>,
    keep_open: bool,
    on_level: Option<Arc<dyn Fn(f32) + Send + Sync>>,
) -> Result<RecordingHandle, String> {
    let (tx, rx) = mpsc::channel();
    manager_tx()
        .lock()
        .map_err(|_| "マイク管理スレッドが応答しません".to_string())?
        .send(Cmd::Start {
            device_name: device_name.filter(|s| !s.is_empty()).map(String::from),
            keep_open,
            on_level,
            reply: tx,
        })
        .map_err(|_| "マイク管理スレッドに送信できませんでした".to_string())?;
    let shared = rx
        .recv()
        .map_err(|_| "録音開始の応答がありませんでした".to_string())??;
    Ok(RecordingHandle {
        shared,
        keep_open,
        stopped: false,
    })
}

/// 録音を開始する（`device_name` が `None` なら OS の既定のマイク）。
pub fn start_recording(
    device_name: Option<&str>,
    on_level: Option<Arc<dyn Fn(f32) + Send + Sync>>,
) -> Result<RecordingHandle, String> {
    start_recording_ex(device_name, false, on_level)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resample_preserves_length_ratio() {
        let input = vec![0.5f32; 48000];
        let out = resample_linear(&input, 48000, 16000);
        assert_eq!(out.len(), 16000);
        assert!((out[0] - 0.5).abs() < 1e-5);
    }
}
