//! 録音中の逐次ライブ文字起こしセッション（元の Python 版 `client/live_session.py` の移植）。
//!
//! 長めの録音中に無音区切りで届いた音声チャンクを裏スレッドで順番に認識しておき、
//! 録音終了時は未処理分だけ待って連結する。失敗した区切りは最後にもう 1 回だけ再試行する。

use std::collections::BTreeMap;
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::Instant;

use onseiman_core::segmenter::{join_segments, CutKind};

#[derive(Debug, Clone)]
pub struct SegmentRecognition {
    pub text: String,
    pub backend: String,
    pub elapsed_ms: f64,
}

#[derive(Debug, Clone)]
pub struct LiveResult {
    pub text: String,
    pub backend: String,
    pub segments: usize,
    pub wait_ms: f64,
    pub asr_ms_total: f64,
}

struct SharedState {
    samples: BTreeMap<usize, Vec<f32>>,
    texts: BTreeMap<usize, String>,
    cuts: BTreeMap<usize, CutKind>,
    errors: BTreeMap<usize, String>,
    backend: String,
    asr_ms: f64,
    count: usize,
    cancelled: bool,
    closed: bool,
}

pub type RecognizeFn = Arc<dyn Fn(&[f32]) -> Result<SegmentRecognition, String> + Send + Sync>;
pub type ProgressFn = Arc<dyn Fn(&str) + Send + Sync>;

pub struct LiveTranscription {
    tx: mpsc::Sender<Option<(usize, Vec<f32>)>>,
    state: Arc<Mutex<SharedState>>,
    recognize: RecognizeFn,
    worker: Option<thread::JoinHandle<()>>,
}

impl LiveTranscription {
    pub fn start(recognize: RecognizeFn, on_progress: Option<ProgressFn>) -> Self {
        let (tx, rx) = mpsc::channel::<Option<(usize, Vec<f32>)>>();
        let state = Arc::new(Mutex::new(SharedState {
            samples: BTreeMap::new(),
            texts: BTreeMap::new(),
            cuts: BTreeMap::new(),
            errors: BTreeMap::new(),
            backend: String::new(),
            asr_ms: 0.0,
            count: 0,
            cancelled: false,
            closed: false,
        }));
        let st_bg = Arc::clone(&state);
        let rec_bg = Arc::clone(&recognize);
        let worker = thread::spawn(move || {
            while let Ok(Some((idx, samples))) = rx.recv() {
                if st_bg.lock().map(|g| g.cancelled).unwrap_or(true) {
                    continue;
                }
                recognize_one(&st_bg, &rec_bg, idx, &samples);
                if let Some(ref cb) = on_progress {
                    if !st_bg.lock().map(|g| g.cancelled).unwrap_or(true) {
                        let so_far = text_so_far_inner(&st_bg);
                        cb(&so_far);
                    }
                }
            }
        });
        Self {
            tx,
            state,
            recognize,
            worker: Some(worker),
        }
    }

    pub fn submit(&self, samples: Vec<f32>) {
        self.submit_with_cut(samples, CutKind::Forced);
    }

    pub fn submit_with_cut(&self, samples: Vec<f32>, cut: CutKind) {
        let idx = {
            let Ok(mut g) = self.state.lock() else {
                return;
            };
            if g.closed {
                return;
            }
            let i = g.count;
            g.count += 1;
            g.samples.insert(i, samples.clone());
            g.cuts.insert(i, cut);
            i
        };
        let _ = self.tx.send(Some((idx, samples)));
    }

    pub fn segment_count(&self) -> usize {
        self.state.lock().map(|g| g.count).unwrap_or(0)
    }

    pub fn cancel(&mut self) {
        if let Ok(mut g) = self.state.lock() {
            g.cancelled = true;
            g.closed = true;
        }
        let _ = self.tx.send(None);
    }

    pub fn finish(mut self) -> Result<LiveResult, String> {
        let t0 = Instant::now();
        if let Ok(mut g) = self.state.lock() {
            g.closed = true;
        }
        let _ = self.tx.send(None);
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
        let failed: Vec<(usize, Vec<f32>)> = {
            let g = self.state.lock().unwrap();
            g.errors
                .keys()
                .filter_map(|&idx| g.samples.get(&idx).map(|s| (idx, s.clone())))
                .collect()
        };
        for (idx, samples) in failed {
            recognize_one(&self.state, &self.recognize, idx, &samples);
        }
        let g = self.state.lock().unwrap();
        if !g.errors.is_empty() {
            let first = g.errors.values().next().cloned().unwrap_or_default();
            return Err(format!(
                "{} 個の区切りを認識できませんでした: {first}",
                g.errors.len()
            ));
        }
        let pairs: Vec<(String, CutKind)> = (0..g.count)
            .map(|i| {
                (
                    g.texts.get(&i).cloned().unwrap_or_default(),
                    g.cuts.get(&i).copied().unwrap_or(CutKind::Forced),
                )
            })
            .collect();
        Ok(LiveResult {
            text: join_segments(&pairs),
            backend: g.backend.clone(),
            segments: g.count,
            wait_ms: t0.elapsed().as_secs_f64() * 1000.0,
            asr_ms_total: g.asr_ms,
        })
    }
}

fn recognize_one(
    state: &Arc<Mutex<SharedState>>,
    recognize: &RecognizeFn,
    idx: usize,
    samples: &[f32],
) {
    match recognize(samples) {
        Ok(res) => {
            if let Ok(mut g) = state.lock() {
                g.texts.insert(idx, res.text);
                g.errors.remove(&idx);
                if !res.backend.is_empty() {
                    g.backend = res.backend;
                }
                g.asr_ms += res.elapsed_ms;
            }
        }
        Err(e) => {
            if let Ok(mut g) = state.lock() {
                g.errors.insert(idx, e);
            }
        }
    }
}

fn text_so_far_inner(state: &Arc<Mutex<SharedState>>) -> String {
    let Ok(g) = state.lock() else {
        return String::new();
    };
    let mut done = Vec::new();
    for i in 0..g.count {
        match g.texts.get(&i) {
            Some(t) => done.push((
                t.clone(),
                g.cuts.get(&i).copied().unwrap_or(CutKind::Forced),
            )),
            None => break,
        }
    }
    join_segments(&done)
}

/// 直前と今回の認識結果で一致している先頭部分だけを返す（コンセンサス表示用）。
pub fn stable_prefix(prev: &str, curr: &str) -> String {
    prev.chars()
        .zip(curr.chars())
        .take_while(|(a, b)| a == b)
        .map(|(a, _)| a)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_prefix_finds_common_head() {
        assert_eq!(
            stable_prefix("明日の会議は", "明日の会議について"),
            "明日の会議"
        );
    }

    #[test]
    fn live_transcription_joins_segments_in_order() {
        let rec: RecognizeFn = Arc::new(|samples: &[f32]| {
            Ok(SegmentRecognition {
                text: format!("seg{}", samples.len()),
                backend: "test".into(),
                elapsed_ms: 10.0,
            })
        });
        let live = LiveTranscription::start(rec, None);
        live.submit(vec![0.1; 10]);
        live.submit(vec![0.1; 20]);
        let res = live.finish().unwrap();
        assert_eq!(res.segments, 2);
        assert_eq!(res.text, "seg10 seg20");
        assert_eq!(res.backend, "test");
    }

    #[test]
    fn live_transcription_inserts_period_on_pause_cuts() {
        let rec: RecognizeFn = Arc::new(|samples: &[f32]| {
            let text = if samples.len() == 10 {
                "修正が上がってきました"
            } else {
                "対応を進めてください"
            };
            Ok(SegmentRecognition {
                text: text.into(),
                backend: "test".into(),
                elapsed_ms: 5.0,
            })
        });
        let live = LiveTranscription::start(rec, None);
        live.submit_with_cut(vec![0.1; 10], CutKind::Pause);
        live.submit_with_cut(vec![0.1; 20], CutKind::End);
        let res = live.finish().unwrap();
        assert_eq!(res.text, "修正が上がってきました。対応を進めてください");
    }
}
