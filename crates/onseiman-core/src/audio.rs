//! WAV の読み書き。
//!
//! 読むのは、縮めていない WAV（PCM の 8〜32 ビットと 32 ビットの小数）だけ。縮めてある WAV は受け付けない
//! （小さくても開くと巨大になるものを、外から送り込まれないように）。

use std::io::Cursor;

pub const MAX_CHANNELS: u16 = 8;

#[derive(Debug, PartialEq, Eq)]
pub enum AudioError {
    /// WAV として読めない・扱えない形
    Invalid,
    /// `max_sec` より長い
    TooLong,
}

/// WAV を、モノラルの -1.0〜1.0 の並びにする。戻り値は (音声, 1 秒あたりの数)。
///
/// `max_sec` を渡すと、それより長い音声は `AudioError::TooLong` にする（外から届いた音声で使う）。
pub fn decode_wav(data: &[u8], max_sec: Option<f64>) -> Result<(Vec<f32>, u32), AudioError> {
    let mut reader = hound::WavReader::new(Cursor::new(data)).map_err(|_| AudioError::Invalid)?;
    let spec = reader.spec();
    let channels = spec.channels as usize;
    if channels == 0 || spec.channels > MAX_CHANNELS || spec.sample_rate == 0 {
        return Err(AudioError::Invalid);
    }
    // 先頭に書いてある長さは当てにしない。上限より 1 つ多く読んで、読めたら長すぎる
    let limit_frames = max_sec.map(|s| (s * spec.sample_rate as f64) as usize);
    let take = limit_frames
        .map(|f| (f + 1) * channels)
        .unwrap_or(usize::MAX);
    let raw: Vec<f32> = match (spec.sample_format, spec.bits_per_sample) {
        (hound::SampleFormat::Float, 32) => reader
            .samples::<f32>()
            .take(take)
            .collect::<Result<_, _>>()
            .map_err(|_| AudioError::Invalid)?,
        (hound::SampleFormat::Int, bits @ 8..=32) => {
            let scale = (1u64 << (bits - 1)) as f32;
            reader
                .samples::<i32>()
                .take(take)
                .map(|s| s.map(|v| v as f32 / scale))
                .collect::<Result<_, _>>()
                .map_err(|_| AudioError::Invalid)?
        }
        _ => return Err(AudioError::Invalid),
    };
    let frames = raw.len() / channels;
    if let Some(limit) = limit_frames {
        if frames > limit {
            return Err(AudioError::TooLong);
        }
    }
    let mono = if channels == 1 {
        raw
    } else {
        raw.chunks_exact(channels)
            .map(|c| c.iter().sum::<f32>() / channels as f32)
            .collect()
    };
    Ok((mono, spec.sample_rate))
}

/// 小さい声や語尾の音量差をならす軽量な動的ゲイン調整（`dynaudnorm` 相当）。
///
/// Haas-ja は「区間後半の小声・短い語尾が抜ける」癖があるため、150ms フレームごとに
/// 発話区間だけ滑らかに持ち上げ、無音（背景ノイズ）は増幅しない。
pub fn normalize_voice_dynamics(samples: &[f32], sample_rate: u32) -> Vec<f32> {
    if samples.is_empty() || sample_rate == 0 {
        return samples.to_vec();
    }
    let frame_len = ((sample_rate as usize) * 150 / 1000).max(160);
    let n_frames = samples.len().div_ceil(frame_len);
    let mut raw_gains = Vec::with_capacity(n_frames);

    for f in 0..n_frames {
        let start = f * frame_len;
        let end = (start + frame_len).min(samples.len());
        let chunk = &samples[start..end];
        let mut peak = 0.0f32;
        let mut sum_sq = 0.0f32;
        for &s in chunk {
            let a = s.abs();
            if a > peak {
                peak = a;
            }
            sum_sq += s * s;
        }
        let rms = (sum_sq / chunk.len() as f32).sqrt();
        // 無音・背景ノイズのみのフレームは増幅しない
        if peak < 0.008 && rms < 0.003 {
            raw_gains.push(1.0f32);
        } else {
            let by_rms = if rms > 1e-5 { 0.14 / rms } else { 1.0 };
            let by_peak = if peak > 1e-5 { 0.85 / peak } else { 1.0 };
            raw_gains.push(by_rms.min(by_peak).clamp(1.0, 8.0));
        }
    }

    // 前後 2 フレーム（計 5 フレーム）の移動平均でゲイン変化を滑らかにする
    let mut smooth = vec![1.0f32; n_frames];
    for (i, slot) in smooth.iter_mut().enumerate() {
        let lo = i.saturating_sub(2);
        let hi = (i + 2).min(n_frames - 1);
        let sum: f32 = raw_gains[lo..=hi].iter().sum();
        *slot = sum / ((hi - lo + 1) as f32);
    }

    let mut out = Vec::with_capacity(samples.len());
    for (idx, &s) in samples.iter().enumerate() {
        let f = (idx / frame_len).min(n_frames - 1);
        let next_f = (f + 1).min(n_frames - 1);
        let frac = (idx % frame_len) as f32 / frame_len as f32;
        let g = smooth[f] + (smooth[next_f] - smooth[f]) * frac;
        out.push((s * g).clamp(-0.98, 0.98));
    }
    out
}

/// モノラルの -1.0〜1.0 の並びを、16 ビットの WAV にする。
pub fn encode_wav(samples: &[f32], sample_rate: u32) -> Vec<u8> {
    let spec = hound::WavSpec {
        channels: 1,
        sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut buf = Cursor::new(Vec::with_capacity(44 + samples.len() * 2));
    {
        let mut w = hound::WavWriter::new(&mut buf, spec).expect("WAV の頭を書けません");
        for s in samples {
            w.write_sample((s.clamp(-1.0, 1.0) * 32767.0) as i16)
                .expect("WAV に書けません");
        }
        w.finalize().expect("WAV を閉じられません");
    }
    buf.into_inner()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip() {
        let s: Vec<f32> = (0..1600).map(|i| ((i as f32) * 0.05).sin() * 0.5).collect();
        let wav = encode_wav(&s, 16000);
        let (back, sr) = decode_wav(&wav, None).unwrap();
        assert_eq!((back.len(), sr), (1600, 16000));
        assert!(s.iter().zip(&back).all(|(a, b)| (a - b).abs() < 1e-3));
    }

    #[test]
    fn normalize_boosts_quiet_voice_without_boosting_silence() {
        let mut samples = vec![0.0f32; 4800]; // 0.3s 無音
        let quiet: Vec<f32> = (0..4800).map(|i| ((i as f32) * 0.1).sin() * 0.03).collect();
        samples.extend_from_slice(&quiet);
        let out = normalize_voice_dynamics(&samples, 16000);
        assert_eq!(out.len(), samples.len());
        let silence_peak = out[..2400].iter().fold(0.0f32, |a, &x| a.max(x.abs()));
        let voice_peak = out[4800..].iter().fold(0.0f32, |a, &x| a.max(x.abs()));
        assert!(silence_peak < 1e-5, "silence stayed silent");
        assert!(voice_peak > 0.12, "quiet voice boosted: {voice_peak}");
    }

    #[test]
    fn too_long_and_invalid() {
        let wav = encode_wav(&vec![0.0; 16000 * 3], 16000);
        assert_eq!(decode_wav(&wav, Some(2.0)), Err(AudioError::TooLong));
        assert!(decode_wav(&wav, Some(3.0)).is_ok());
        assert_eq!(decode_wav(b"fLaC\0\0\0\0", None), Err(AudioError::Invalid));
        assert_eq!(decode_wav(&wav[..20], None), Err(AudioError::Invalid));
    }

    #[test]
    fn stereo_is_mixed_down() {
        let spec = hound::WavSpec {
            channels: 2,
            sample_rate: 8000,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut buf = Cursor::new(Vec::new());
        {
            let mut w = hound::WavWriter::new(&mut buf, spec).unwrap();
            for _ in 0..100 {
                w.write_sample(16384i16).unwrap();
                w.write_sample(0i16).unwrap();
            }
            w.finalize().unwrap();
        }
        let (mono, sr) = decode_wav(&buf.into_inner(), None).unwrap();
        assert_eq!((mono.len(), sr), (100, 8000));
        assert!((mono[0] - 0.25).abs() < 1e-4);
    }
}
