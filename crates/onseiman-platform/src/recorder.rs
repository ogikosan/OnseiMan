//! マイク録音（cpal による Windows WASAPI / macOS CoreAudio 共通実装）。

use std::sync::{Arc, Mutex};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{SampleFormat, Stream};

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

/// 任意のサンプルレートを 16kHz へ線形補間で変換する。
pub fn resample_linear(samples: &[f32], src_rate: u32, dst_rate: u32) -> Vec<f32> {
    if samples.is_empty() || src_rate == 0 || dst_rate == 0 || src_rate == dst_rate {
        return samples.to_vec();
    }
    let ratio = src_rate as f64 / dst_rate as f64;
    let out_len = ((samples.len() as f64) / ratio).round().max(1.0) as usize;
    let mut out = Vec::with_capacity(out_len);
    for i in 0..out_len {
        let pos = i as f64 * ratio;
        let idx = pos.floor() as usize;
        let frac = (pos - idx as f64) as f32;
        let a = samples[idx.min(samples.len() - 1)];
        let b = samples[(idx + 1).min(samples.len() - 1)];
        out.push(a + (b - a) * frac);
    }
    out
}

pub struct RecordingHandle {
    stream: Stream,
    buf: Arc<Mutex<Vec<f32>>>,
    src_rate: u32,
}

impl RecordingHandle {
    /// 録音を止め、16kHz モノラルの f32 サンプルを返す。
    pub fn stop(self) -> Vec<f32> {
        drop(self.stream);
        let raw = self.buf.lock().map(|g| g.clone()).unwrap_or_default();
        resample_linear(&raw, self.src_rate, 16000)
    }
}

/// 録音を開始する（`device_name` が `None` なら OS の既定のマイク）。
pub fn start_recording(
    device_name: Option<&str>,
    on_level: Option<Arc<dyn Fn(f32) + Send + Sync>>,
) -> Result<RecordingHandle, String> {
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
    let buf: Arc<Mutex<Vec<f32>>> =
        Arc::new(Mutex::new(Vec::with_capacity(src_rate as usize * 10)));
    let buf_cb = Arc::clone(&buf);

    let push_mono = move |mono: &[f32]| {
        if mono.is_empty() {
            return;
        }
        if let Some(ref cb) = on_level {
            let sum: f32 = mono.iter().map(|x| x * x).sum();
            let rms = (sum / mono.len() as f32).sqrt();
            cb((rms * 10.0).min(1.0));
        }
        if let Ok(mut g) = buf_cb.lock() {
            g.extend_from_slice(mono);
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
    Ok(RecordingHandle {
        stream,
        buf,
        src_rate,
    })
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
