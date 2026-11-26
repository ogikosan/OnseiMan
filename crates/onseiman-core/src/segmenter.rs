//! 録音を「話の切れ目（短い無音）」で区切る。Python 版の `shared/segmenter.py` と同じ決まり。
//!
//! 聞き取りの Haas-ja は 1 回に 1 つの発話を渡す作りで、いくつもの文が入った長い録音では文がまるごと抜けることがある。
//!
//! - 区切りが `min_segment_sec` 以上たまり、その後 `pause_sec` 以上の無音が続いたら、無音の終わりで切る
//!   （しきい値より小さい語尾の声を切り落とさないため）。
//! - 切れ目がないまま `max_segment_sec` に達したら、直近数秒のうち一番静かな所で強制的に切る。
//! - ほぼ完全に無音の区切りは捨てる（認識に送らない）。

pub const FRAME_SEC: f64 = 0.02;

#[derive(Debug, Clone, Copy)]
pub struct SegmentOptions {
    pub rms_threshold: f64,
    pub pause_sec: f64,
    pub min_segment_sec: f64,
    pub max_segment_sec: f64,
    pub search_back_sec: f64,
    /// 区切りを捨てる音量。None なら `rms_threshold` の 1/4
    pub drop_below_rms: Option<f64>,
}

impl Default for SegmentOptions {
    fn default() -> Self {
        Self {
            rms_threshold: 0.012,
            pause_sec: 0.5,
            min_segment_sec: 4.0,
            max_segment_sec: 18.0,
            search_back_sec: 6.0,
            drop_below_rms: None,
        }
    }
}

pub struct Segmenter {
    rms_threshold: f64,
    drop_below_rms: f64,
    frame: usize,
    pause_frames: usize,
    min_frames: usize,
    max_frames: usize,
    search_frames: usize,
    buf: Vec<f32>,     // 現在の区切りの音声
    rms: Vec<f32>,     // 現在の区切りのフレームごとの音量
    pending: Vec<f32>, // フレームに満たない端数
    silent_run: usize,
}

fn frames_of(sec: f64) -> usize {
    (sec / FRAME_SEC).round().max(0.0) as usize
}

/// 足し算の順番を numpy（`np.sum`・`np.mean`）と同じにする。
///
/// 小数の足し算は順番で最後の桁が変わる。区切る場所を Python 版と 1 フレームもずらさないために、同じ順番で足す
/// （8 個ずつに分けて足し、128 個より多ければ半分に分けて足す）。
fn pairwise_sum(a: &[f32]) -> f32 {
    let n = a.len();
    if n < 8 {
        return a.iter().fold(0.0, |acc, x| acc + x);
    }
    if n <= 128 {
        let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
        let whole = n - n % 8;
        let mut i = 8;
        while i < whole {
            for (k, slot) in r.iter_mut().enumerate() {
                *slot += a[i + k];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for x in &a[whole..] {
            res += x;
        }
        return res;
    }
    let mut half = n / 2;
    half -= half % 8;
    pairwise_sum(&a[..half]) + pairwise_sum(&a[half..])
}

fn mean(a: &[f32]) -> f32 {
    pairwise_sum(a) / a.len() as f32
}

fn rms_of(frame: &[f32]) -> f32 {
    let squares: Vec<f32> = frame.iter().map(|x| x * x).collect();
    mean(&squares).sqrt()
}

impl Segmenter {
    pub fn new(sample_rate: u32, o: SegmentOptions) -> Self {
        Self {
            rms_threshold: o.rms_threshold,
            // 区切りを捨てるのは、ほぼ完全に無音のときだけ。小声を取りこぼさないよう、切れ目の判定よりずっと低くする
            drop_below_rms: o.drop_below_rms.unwrap_or(o.rms_threshold * 0.25),
            frame: ((sample_rate as f64 * FRAME_SEC) as usize).max(1),
            pause_frames: frames_of(o.pause_sec).max(1),
            min_frames: frames_of(o.min_segment_sec),
            max_frames: frames_of(o.max_segment_sec),
            search_frames: frames_of(o.search_back_sec),
            buf: Vec::new(),
            rms: Vec::new(),
            pending: Vec::new(),
            silent_run: 0,
        }
    }

    /// 音声を足し、切れた区切り（声を含むものだけ）を返す。
    pub fn feed(&mut self, chunk: &[f32]) -> Vec<Vec<f32>> {
        let mut out = Vec::new();
        let mut data = std::mem::take(&mut self.pending);
        data.extend_from_slice(chunk);
        let n_full = data.len() / self.frame;
        self.pending = data.split_off(n_full * self.frame);
        if n_full == 0 {
            return out;
        }
        let frames = data;
        self.buf.extend_from_slice(&frames);
        for i in 0..n_full {
            let r = rms_of(&frames[i * self.frame..(i + 1) * self.frame]);
            self.rms.push(r);
            self.silent_run = if (r as f64) < self.rms_threshold {
                self.silent_run + 1
            } else {
                0
            };
            let n = self.rms.len();
            let cut_at = if n >= self.min_frames && self.silent_run >= self.pause_frames {
                // 無音と判定した部分は前の区切りに全部含める（小声の語尾を切り落とさない）
                Some(n)
            } else if n >= self.max_frames {
                Some(self.quietest_cut(n))
            } else {
                None
            };
            if let Some(cut) = cut_at {
                if let Some(seg) = self.take(cut) {
                    out.push(seg);
                }
            }
        }
        out
    }

    fn quietest_cut(&self, n: usize) -> usize {
        const WIN: usize = 10; // 0.2 秒の平均で一番静かな所
        let lo = (self.min_frames / 2).max(n.saturating_sub(self.search_frames));
        let (mut best, mut best_val) = (n, f32::INFINITY);
        if n >= WIN {
            for i in lo..=(n - WIN) {
                let v = mean(&self.rms[i..i + WIN]);
                if v < best_val {
                    best = i + WIN / 2;
                    best_val = v;
                }
            }
        }
        best
    }

    fn take(&mut self, cut_frames: usize) -> Option<Vec<f32>> {
        let cut = cut_frames * self.frame;
        let rest = self.buf.split_off(cut);
        let seg = std::mem::replace(&mut self.buf, rest);
        let rest_rms = self.rms.split_off(cut_frames);
        let seg_rms = std::mem::replace(&mut self.rms, rest_rms);
        self.silent_run = self
            .rms
            .iter()
            .rev()
            .take_while(|r| (**r as f64) < self.rms_threshold)
            .count();
        let loudest = seg_rms.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        if seg_rms.is_empty() || (loudest as f64) < self.drop_below_rms {
            return None;
        }
        Some(seg)
    }

    /// 残り（最後の区切り）を返す。声がほとんど無ければ None。
    pub fn flush(&mut self) -> Option<Vec<f32>> {
        let mut tail = std::mem::take(&mut self.buf);
        tail.append(&mut self.pending);
        let rms = std::mem::take(&mut self.rms);
        self.silent_run = 0;
        if rms.is_empty() {
            return None; // 1 フレームに満たない端数だけ
        }
        let loudest = rms.iter().cloned().fold(f32::NEG_INFINITY, f32::max);
        if (loudest as f64) < self.drop_below_rms {
            return None;
        }
        Some(tail)
    }
}

/// 保存済みの音声を同じ規則で区切る。声が全く検出されないときは元の音声をそのまま 1 つ返す。
pub fn split_on_pauses(
    samples: &[f32],
    sample_rate: u32,
    options: SegmentOptions,
) -> Vec<Vec<f32>> {
    let mut seg = Segmenter::new(sample_rate, options);
    let mut parts = Vec::new();
    for block in samples.chunks(1024) {
        parts.extend(seg.feed(block));
    }
    if let Some(tail) = seg.flush() {
        parts.push(tail);
    }
    if parts.is_empty() && !samples.is_empty() {
        parts.push(samples.to_vec());
    }
    parts
}

/// 区切りごとの認識結果をつなぐ。英数字どうしが接するときだけ空白を入れる。
pub fn join_texts<S: AsRef<str>>(texts: &[S]) -> String {
    let mut out = String::new();
    for t in texts {
        let t = t.as_ref().trim();
        if t.is_empty() {
            continue;
        }
        let last = out.chars().last();
        let first = t.chars().next();
        if let (Some(a), Some(b)) = (last, first) {
            if a.is_ascii_alphanumeric() && b.is_ascii_alphanumeric() {
                out.push(' ');
            }
        }
        out.push_str(t);
    }
    out
}

/// 動的ノイズフロア学習＋末尾 200ms 余白付きの適応型セグメンタ（`OpenHinoVoice` 方式）。
///
/// 固定しきい値ではなく冒頭 200ms と無音フレームから背景雑音（`noise_floor`）を常時学習し、
/// `noise_floor * 3.0`（下限 0.006）が 3 フレーム連続したら発話開始、600ms 無音で 200ms の
/// 末尾余白を残して区切る。上限（既定 9 秒）到達時は直近 3.5 秒の最静寂フレームで切る。
pub struct AdaptiveSegmenter {
    frame_len: usize,
    calib_frames: usize,
    silence_close_frames: usize,
    max_frames: usize,
    cut_search_frames: usize,
    tail_frames: usize,
    noise_floor: f32,
    frames_seen: usize,
    speech_run: usize,
    silent_run: usize,
    in_speech: bool,
    pending: Vec<f32>,
    open: Vec<f32>,
    open_rms: Vec<f32>,
}

impl AdaptiveSegmenter {
    pub fn new(sample_rate: u32, max_sec: f64) -> Self {
        let frame_len = ((sample_rate as f64) * FRAME_SEC).round().max(1.0) as usize;
        Self {
            frame_len,
            calib_frames: frames_of(0.2),
            silence_close_frames: frames_of(0.6),
            max_frames: frames_of(max_sec).max(1),
            cut_search_frames: frames_of(3.5),
            tail_frames: frames_of(0.2),
            noise_floor: 0.003,
            frames_seen: 0,
            speech_run: 0,
            silent_run: 0,
            in_speech: false,
            pending: Vec::new(),
            open: Vec::new(),
            open_rms: Vec::new(),
        }
    }

    pub fn push(&mut self, samples: &[f32]) -> Vec<Vec<f32>> {
        self.pending.extend_from_slice(samples);
        let mut ready = Vec::new();
        while self.pending.len() >= self.frame_len {
            let frame: Vec<f32> = self.pending.drain(..self.frame_len).collect();
            let r = rms_of(&frame);
            self.frames_seen += 1;

            let thresh = (self.noise_floor * 3.0).max(0.006);
            if r < thresh && r < 0.02 {
                // 背景雑音をゆっくり追従学習
                let alpha = if self.frames_seen <= self.calib_frames {
                    0.25
                } else {
                    0.03
                };
                self.noise_floor = self.noise_floor * (1.0 - alpha) + r * alpha;
            }

            if !self.in_speech {
                if r >= thresh {
                    self.speech_run += 1;
                    self.open.extend_from_slice(&frame);
                    self.open_rms.push(r);
                    if self.speech_run >= 3 {
                        self.in_speech = true;
                        self.silent_run = 0;
                    }
                } else {
                    self.speech_run = 0;
                    // 発話開始直前の 1〜2 フレームだけ残す
                    if self.open_rms.len() > 2 {
                        let drop_n = self.open_rms.len() - 2;
                        self.open.drain(..drop_n * self.frame_len);
                        self.open_rms.drain(..drop_n);
                    }
                    self.open.extend_from_slice(&frame);
                    self.open_rms.push(r);
                }
                continue;
            }

            self.open.extend_from_slice(&frame);
            self.open_rms.push(r);
            if r < thresh {
                self.silent_run += 1;
            } else {
                self.silent_run = 0;
            }

            if self.silent_run >= self.silence_close_frames {
                let keep_frames = self
                    .open_rms
                    .len()
                    .saturating_sub(self.silent_run.saturating_sub(self.tail_frames));
                let cut_samples = (keep_frames * self.frame_len).min(self.open.len());
                if cut_samples >= self.frame_len * 10 {
                    ready.push(self.open[..cut_samples].to_vec());
                }
                self.open.clear();
                self.open_rms.clear();
                self.in_speech = false;
                self.speech_run = 0;
                self.silent_run = 0;
            } else if self.open_rms.len() >= self.max_frames {
                // 上限到達時は直近 cut_search_frames の最静寂フレームで切る
                let total = self.open_rms.len();
                let lo = total.saturating_sub(self.cut_search_frames);
                let mut best_idx = total - 1;
                let mut best_val = f32::MAX;
                for (off, &val) in self.open_rms[lo..].iter().enumerate() {
                    if val < best_val {
                        best_val = val;
                        best_idx = lo + off;
                    }
                }
                let cut_frames = (best_idx + 1).max(1);
                let cut_samples = (cut_frames * self.frame_len).min(self.open.len());
                ready.push(self.open[..cut_samples].to_vec());
                self.open.drain(..cut_samples);
                self.open_rms.drain(..cut_frames.min(self.open_rms.len()));
                self.silent_run = 0;
            }
        }
        ready
    }

    pub fn flush(&mut self) -> Option<Vec<f32>> {
        if !self.pending.is_empty() {
            self.open.append(&mut self.pending);
        }
        if self.in_speech && self.open.len() >= self.frame_len * 5 {
            self.in_speech = false;
            self.open_rms.clear();
            Some(std::mem::take(&mut self.open))
        } else {
            self.open.clear();
            self.open_rms.clear();
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_adds_space_only_between_ascii_alnum() {
        assert_eq!(
            join_texts(&["こんにちは。", " 元気ですか"]),
            "こんにちは。元気ですか"
        );
        assert_eq!(join_texts(&["hello", "world"]), "hello world");
        assert_eq!(join_texts(&["RTX", "5090", "です"]), "RTX 5090です");
        assert_eq!(join_texts(&["", "  ", "a"]), "a");
    }

    #[test]
    fn silence_only_is_returned_as_one_piece() {
        let s = vec![0.0f32; 16000 * 3];
        let parts = split_on_pauses(&s, 16000, SegmentOptions::default());
        assert_eq!(parts.len(), 1);
        assert_eq!(parts[0].len(), s.len());
        assert!(split_on_pauses(&[], 16000, SegmentOptions::default()).is_empty());
    }
}
