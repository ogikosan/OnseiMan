//! キーボードフック詰まり監視と修飾キー解放（元の `client/watchdog.py` の移植）。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// 押しっぱなしになった修飾キー（Alt / Ctrl / Shift / Win）を安全に解放する。
#[cfg(windows)]
pub fn release_stuck_modifiers() {
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        GetAsyncKeyState, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP,
        VK_LCONTROL, VK_LMENU, VK_LSHIFT, VK_LWIN, VK_RCONTROL, VK_RMENU, VK_RSHIFT, VK_RWIN,
    };

    const MODS: [u16; 8] = [
        VK_RMENU,
        VK_LMENU,
        VK_RCONTROL,
        VK_LCONTROL,
        VK_RSHIFT,
        VK_LSHIFT,
        VK_LWIN,
        VK_RWIN,
    ];
    let mut inputs = Vec::new();
    for &vk in &MODS {
        // SAFETY: Win32 GetAsyncKeyState
        if (unsafe { GetAsyncKeyState(vk as i32) } as u16 & 0x8000) != 0 {
            inputs.push(INPUT {
                r#type: INPUT_KEYBOARD,
                Anonymous: INPUT_0 {
                    ki: KEYBDINPUT {
                        wVk: vk,
                        wScan: 0,
                        dwFlags: KEYEVENTF_KEYUP,
                        time: 0,
                        dwExtraInfo: 0,
                    },
                },
            });
        }
    }
    if !inputs.is_empty() {
        unsafe {
            SendInput(
                inputs.len() as u32,
                inputs.as_ptr(),
                std::mem::size_of::<INPUT>() as i32,
            );
        }
    }
}

#[cfg(not(windows))]
pub fn release_stuck_modifiers() {}

pub struct HookWatchdog {
    stop: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
}

impl HookWatchdog {
    /// フックの心拍（`heartbeat`）を監視し、2.5 秒以上応答が止まったら修飾キー解放＋コールバックを呼ぶ。
    pub fn start<F>(heartbeat: &'static AtomicU64, on_stall: F) -> Self
    where
        F: Fn(u64) + Send + 'static,
    {
        let stop = Arc::new(AtomicBool::new(false));
        let stop_bg = Arc::clone(&stop);
        let join = thread::spawn(move || {
            const STALE_LIMIT_MS: u64 = 2500;
            let mut grace_until = Instant::now() + Duration::from_secs(5);
            let mut last_loop = Instant::now();
            while !stop_bg.load(Ordering::Relaxed) {
                thread::sleep(Duration::from_millis(200));
                let now_inst = Instant::now();
                // スリープ復帰直後は誤検知を避けるため 3 秒猶予
                if now_inst.duration_since(last_loop) > Duration::from_secs(1) {
                    grace_until = now_inst + Duration::from_secs(3);
                }
                last_loop = now_inst;
                if now_inst < grace_until {
                    continue;
                }
                let beat = heartbeat.load(Ordering::Relaxed);
                if beat == 0 {
                    continue;
                }
                let stale = now_ms().saturating_sub(beat);
                if stale > STALE_LIMIT_MS {
                    release_stuck_modifiers();
                    on_stall(stale);
                    // 連続発火を防ぐため次の猶予を入れる
                    grace_until = Instant::now() + Duration::from_secs(5);
                }
            }
        });
        Self {
            stop,
            join: Some(join),
        }
    }
}

impl Drop for HookWatchdog {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(j) = self.join.take() {
            let _ = j.join();
        }
    }
}
