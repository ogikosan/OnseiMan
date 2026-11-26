//! グローバルホットキー監視のイベント型とプラットフォーム起動口。
//!
//! Windows では `WH_KEYBOARD_LL` と `WH_MOUSE_LL` を専用のメッセージループスレッドで回す。
//! 自前の `SendInput`（`LLKHF_INJECTED` / `LLMHF_INJECTED`）は無視する。

use crate::binding::Binding;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HotkeyEvent {
    PrimaryPress,
    PrimaryRelease,
    AiPress,
    AiRelease,
    Cancel,
}

#[derive(Debug, Clone)]
pub struct HotkeyConfig {
    pub primary: Binding,
    pub extra: Option<Binding>,
    pub ai: Option<Binding>,
    pub cancel: Option<Binding>,
    pub suppress: bool,
}

#[cfg(windows)]
mod imp {
    use super::*;
    use std::ptr;
    use std::sync::mpsc::Sender;
    use std::sync::{Mutex, OnceLock};
    use std::thread;
    use windows_sys::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
    use windows_sys::Win32::System::Threading::GetCurrentThreadId;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CallNextHookEx, DispatchMessageW, GetMessageW, PostThreadMessageW, SetWindowsHookExW,
        TranslateMessage, UnhookWindowsHookEx, KBDLLHOOKSTRUCT, MSG, MSLLHOOKSTRUCT,
        WH_KEYBOARD_LL, WH_MOUSE_LL, WM_KEYDOWN, WM_KEYUP, WM_MBUTTONDOWN, WM_MBUTTONUP, WM_QUIT,
        WM_SYSKEYDOWN, WM_SYSKEYUP, WM_XBUTTONDOWN, WM_XBUTTONUP,
    };

    use crate::binding::MouseButton;

    struct HookState {
        cfg: HotkeyConfig,
        primary_down: bool,
        ai_down: bool,
        tx: Sender<HotkeyEvent>,
    }

    impl HookState {
        fn handle_binding(&mut self, b: Binding, is_down: bool, is_up: bool) -> bool {
            let is_primary = self.cfg.primary == b || self.cfg.extra == Some(b);
            if is_primary {
                if is_down && !self.primary_down {
                    self.primary_down = true;
                    let _ = self.tx.send(HotkeyEvent::PrimaryPress);
                } else if is_up && self.primary_down {
                    self.primary_down = false;
                    let _ = self.tx.send(HotkeyEvent::PrimaryRelease);
                }
                return self.cfg.suppress;
            }
            if self.cfg.ai == Some(b) {
                if is_down && !self.ai_down {
                    self.ai_down = true;
                    let _ = self.tx.send(HotkeyEvent::AiPress);
                } else if is_up && self.ai_down {
                    self.ai_down = false;
                    let _ = self.tx.send(HotkeyEvent::AiRelease);
                }
                return self.cfg.suppress;
            }
            if self.cfg.cancel == Some(b) && is_down {
                let _ = self.tx.send(HotkeyEvent::Cancel);
            }
            false
        }
    }

    static STATE: OnceLock<Mutex<Option<HookState>>> = OnceLock::new();

    unsafe extern "system" fn kbd_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        if code >= 0 && lparam != 0 {
            let info = &*(lparam as *const KBDLLHOOKSTRUCT);
            // 自前の SendInput（LLKHF_INJECTED = 0x10）は無視する
            if (info.flags & 0x10) == 0 {
                let is_down = wparam == WM_KEYDOWN as WPARAM || wparam == WM_SYSKEYDOWN as WPARAM;
                let is_up = wparam == WM_KEYUP as WPARAM || wparam == WM_SYSKEYUP as WPARAM;
                if (is_down || is_up) && STATE.get().is_some() {
                    if let Ok(mut guard) = STATE.get().unwrap().lock() {
                        if let Some(st) = guard.as_mut() {
                            if st.handle_binding(Binding::Key(info.vkCode), is_down, is_up) {
                                return 1;
                            }
                        }
                    }
                }
            }
        }
        CallNextHookEx(ptr::null_mut(), code, wparam, lparam)
    }

    unsafe extern "system" fn mouse_proc(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        if code >= 0 && lparam != 0 {
            let info = &*(lparam as *const MSLLHOOKSTRUCT);
            // LLMHF_INJECTED = 0x01
            if (info.flags & 0x01) == 0 {
                let wp = wparam as u32;
                let (btn, is_down, is_up) = match wp {
                    WM_MBUTTONDOWN => (Some(MouseButton::Middle), true, false),
                    WM_MBUTTONUP => (Some(MouseButton::Middle), false, true),
                    WM_XBUTTONDOWN | WM_XBUTTONUP => {
                        let xbtn = (info.mouseData >> 16) & 0xFFFF;
                        let b = match xbtn {
                            1 => Some(MouseButton::X1),
                            2 => Some(MouseButton::X2),
                            _ => None,
                        };
                        (b, wp == WM_XBUTTONDOWN, wp == WM_XBUTTONUP)
                    }
                    _ => (None, false, false),
                };
                if let Some(btn) = btn {
                    if let Some(lock) = STATE.get() {
                        if let Ok(mut guard) = lock.lock() {
                            if let Some(st) = guard.as_mut() {
                                if st.handle_binding(Binding::Mouse(btn), is_down, is_up) {
                                    return 1;
                                }
                            }
                        }
                    }
                }
            }
        }
        CallNextHookEx(ptr::null_mut(), code, wparam, lparam)
    }

    pub struct GlobalHotkey {
        thread_id: u32,
        join: Option<thread::JoinHandle<()>>,
    }

    impl GlobalHotkey {
        pub fn start(cfg: HotkeyConfig, tx: Sender<HotkeyEvent>) -> Result<Self, String> {
            let needs_mouse = matches!(cfg.primary, Binding::Mouse(_))
                || matches!(cfg.extra, Some(Binding::Mouse(_)))
                || matches!(cfg.ai, Some(Binding::Mouse(_)))
                || matches!(cfg.cancel, Some(Binding::Mouse(_)));

            let lock = STATE.get_or_init(|| Mutex::new(None));
            *lock.lock().map_err(|_| "lock poisoned")? = Some(HookState {
                cfg,
                primary_down: false,
                ai_down: false,
                tx,
            });
            let (tid_tx, tid_rx) = std::sync::mpsc::channel();
            let join = thread::spawn(move || unsafe {
                let tid = GetCurrentThreadId();
                let _ = tid_tx.send(tid);
                let kbd_hook =
                    SetWindowsHookExW(WH_KEYBOARD_LL, Some(kbd_proc), ptr::null_mut(), 0);
                if kbd_hook.is_null() {
                    return;
                }
                let mouse_hook = if needs_mouse {
                    SetWindowsHookExW(WH_MOUSE_LL, Some(mouse_proc), ptr::null_mut(), 0)
                } else {
                    ptr::null_mut()
                };
                let mut msg: MSG = std::mem::zeroed();
                while GetMessageW(&mut msg, ptr::null_mut(), 0, 0) > 0 {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
                UnhookWindowsHookEx(kbd_hook);
                if !mouse_hook.is_null() {
                    UnhookWindowsHookEx(mouse_hook);
                }
            });
            let thread_id = tid_rx
                .recv()
                .map_err(|_| "フックスレッドの起動に失敗しました".to_string())?;
            Ok(Self {
                thread_id,
                join: Some(join),
            })
        }
    }

    impl Drop for GlobalHotkey {
        fn drop(&mut self) {
            unsafe {
                PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0);
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
            if let Some(lock) = STATE.get() {
                if let Ok(mut g) = lock.lock() {
                    *g = None;
                }
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::*;
    use std::sync::mpsc::Sender;

    pub struct GlobalHotkey;

    impl GlobalHotkey {
        pub fn start(_cfg: HotkeyConfig, _tx: Sender<HotkeyEvent>) -> Result<Self, String> {
            Err("macOS の CGEventTap グローバルフックは次ステップで実装します".into())
        }
    }
}

pub use imp::GlobalHotkey;
