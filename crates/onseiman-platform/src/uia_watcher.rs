//! 貼り付け直後のユーザー手直しを Windows UI Automation（UIA）で自動検出する監視器。
//!
//! 元の Python 版 `client/uia_watcher.py`（`EditCorrectionWatcher`）の純 Rust 移植。
//! 貼り付け直後から最大 25 秒間、同じウィンドウが前面にあるあいだフォーカス中コントロールの
//! テキストを 0.6 秒間隔で読み取り、変更が 1.6 秒落ち着いたらコールバックへ通知する。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

pub struct EditCorrectionWatcher {
    cancel_flag: Mutex<Option<Arc<AtomicBool>>>,
}

impl Default for EditCorrectionWatcher {
    fn default() -> Self {
        Self::new()
    }
}

impl EditCorrectionWatcher {
    pub fn new() -> Self {
        Self {
            cancel_flag: Mutex::new(None),
        }
    }

    pub fn cancel(&self) {
        if let Ok(mut g) = self.cancel_flag.lock() {
            if let Some(flag) = g.take() {
                flag.store(true, Ordering::SeqCst);
            }
        }
    }

    /// 貼り付け直後のテキスト編集を監視し、修正が確定したら `on_edited(pasted_norm, edited_text)` を呼ぶ。
    #[cfg(windows)]
    pub fn watch_after_paste<F>(&self, pasted_text: String, target_hwnd: isize, on_edited: F)
    where
        F: FnOnce(String, String) + Send + 'static,
    {
        let pasted_norm = clean_uia_text(&pasted_text).trim().to_string();
        if pasted_norm.chars().count() < 2 {
            return;
        }
        self.cancel();
        let cancel = Arc::new(AtomicBool::new(false));
        if let Ok(mut g) = self.cancel_flag.lock() {
            *g = Some(Arc::clone(&cancel));
        }

        std::thread::spawn(move || {
            watch_worker(pasted_norm, target_hwnd, on_edited, cancel);
        });
    }

    #[cfg(not(windows))]
    pub fn watch_after_paste<F>(&self, _pasted_text: String, _target_hwnd: isize, _on_edited: F)
    where
        F: FnOnce(String, String) + Send + 'static,
    {
    }
}

#[cfg(windows)]
fn clean_uia_text(s: &str) -> String {
    s.replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\u{200b}', "")
}

#[cfg(windows)]
fn read_focused_element_and_text(
    uia: &windows::Win32::UI::Accessibility::IUIAutomation,
) -> Option<(
    windows::Win32::UI::Accessibility::IUIAutomationElement,
    String,
)> {
    use windows::Win32::UI::Accessibility::{
        IUIAutomationTextPattern, IUIAutomationValuePattern, UIA_TextPatternId, UIA_ValuePatternId,
    };

    unsafe {
        let elem = uia.GetFocusedElement().ok()?;
        // SECURITY.md: パスワード欄は絶対に読まない（取得失敗時も安全側で拒否）
        if elem
            .CurrentIsPassword()
            .map(|b| b.as_bool())
            .unwrap_or(true)
        {
            return None;
        }
        // 1. ValuePattern（1行入力欄・検索欄・チャット欄など）
        if let Ok(vp) = elem.GetCurrentPatternAs::<IUIAutomationValuePattern>(UIA_ValuePatternId) {
            if let Ok(val) = vp.CurrentValue() {
                let s = clean_uia_text(&val.to_string());
                if !s.is_empty() {
                    return Some((elem, s));
                }
            }
        }
        // 2. TextPattern（メモ帳・リッチエディタ・VS Code・ブラウザ textarea 等）
        if let Ok(tp) = elem.GetCurrentPatternAs::<IUIAutomationTextPattern>(UIA_TextPatternId) {
            if let Ok(range) = tp.DocumentRange() {
                if let Ok(txt) = range.GetText(16_384) {
                    let s = clean_uia_text(&txt.to_string());
                    if !s.is_empty() {
                        return Some((elem, s));
                    }
                }
            }
        }
        Some((elem, String::new()))
    }
}

#[cfg(windows)]
fn sleep_cancelable(cancel: &AtomicBool, total_ms: u64) -> bool {
    let step = 50u64;
    let mut elapsed = 0u64;
    while elapsed < total_ms {
        if cancel.load(Ordering::SeqCst) {
            return true;
        }
        let d = step.min(total_ms - elapsed);
        std::thread::sleep(std::time::Duration::from_millis(d));
        elapsed += d;
    }
    cancel.load(Ordering::SeqCst)
}

#[cfg(windows)]
fn watch_worker<F>(pasted_norm: String, target_hwnd: isize, on_edited: F, cancel: Arc<AtomicBool>)
where
    F: FnOnce(String, String) + Send + 'static,
{
    use std::time::{Duration, Instant};
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
        COINIT_MULTITHREADED,
    };
    use windows::Win32::UI::Accessibility::{CUIAutomation, IUIAutomation};
    use windows_sys::Win32::UI::WindowsAndMessaging::GetForegroundWindow;

    // 貼り付け反映を少し待つ
    if sleep_cancelable(&cancel, 450) {
        return;
    }

    unsafe {
        let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
        let should_uninit = hr.is_ok();
        let uia_res: windows::core::Result<IUIAutomation> =
            CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER);
        let Ok(uia) = uia_res else {
            if should_uninit {
                CoUninitialize();
            }
            return;
        };

        let Some((baseline_elem, baseline)) = read_focused_element_and_text(&uia) else {
            if should_uninit {
                CoUninitialize();
            }
            return;
        };
        if baseline.trim().is_empty() {
            if should_uninit {
                CoUninitialize();
            }
            return;
        }

        let probe: String = pasted_norm.chars().take(8).collect();
        if !baseline.contains(&pasted_norm) && !baseline.contains(&probe) {
            if should_uninit {
                CoUninitialize();
            }
            return;
        }

        let deadline = Instant::now() + Duration::from_secs(25);
        let mut last_text = baseline.clone();
        let mut last_change = Instant::now();
        let mut best_edited: Option<String> = None;

        while Instant::now() < deadline && !cancel.load(Ordering::SeqCst) {
            if sleep_cancelable(&cancel, 600) {
                break;
            }
            let fg = GetForegroundWindow() as isize;
            if target_hwnd != 0 && fg != 0 && fg != target_hwnd {
                break;
            }
            let Some((cur_elem, cur)) = read_focused_element_and_text(&uia) else {
                break;
            };
            // 同一ウィンドウ内でも別の入力欄（パスワード欄等）へフォーカス移動したら即終了
            let same_elem = uia
                .CompareElements(&baseline_elem, &cur_elem)
                .map(|b| b.as_bool())
                .unwrap_or(false);
            if !same_elem {
                break;
            }
            // チャットで Enter 送信して空になった → 直前の編集状態を評価
            if cur.trim().is_empty() && best_edited.is_some() {
                break;
            }
            if cur != last_text {
                last_text = cur.clone();
                last_change = Instant::now();
                if cur != baseline && !cur.trim().is_empty() {
                    best_edited = Some(cur);
                }
            } else if best_edited.is_some() && last_change.elapsed() >= Duration::from_millis(1600)
            {
                break;
            }
        }

        if should_uninit {
            CoUninitialize();
        }

        if !cancel.load(Ordering::SeqCst) {
            if let Some(edited) = best_edited {
                on_edited(pasted_norm, edited);
            }
        }
    }
}
