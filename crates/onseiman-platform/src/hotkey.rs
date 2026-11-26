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

#[cfg(target_os = "macos")]
mod imp {
    use super::*;
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::mpsc::Sender;
    use std::sync::{Mutex, OnceLock};
    use std::thread;

    type CGEventTapProxy = *mut c_void;
    type CGEventRef = *mut c_void;
    type CFMachPortRef = *mut c_void;
    type CFRunLoopSourceRef = *mut c_void;
    type CFRunLoopRef = *mut c_void;
    type CFStringRef = *const c_void;
    type CGEventType = u32;
    type CGEventMask = u64;

    const K_CG_HID_EVENT_TAP: u32 = 0;
    const K_CG_HEAD_INSERT_EVENT_TAP: u32 = 0;
    const K_CG_EVENT_TAP_OPTION_DEFAULT: u32 = 0;
    const K_CG_EVENT_KEY_DOWN: u32 = 10;
    const K_CG_EVENT_KEY_UP: u32 = 11;
    const K_CG_EVENT_FLAGS_CHANGED: u32 = 12;
    const K_CG_KEYBOARD_EVENT_KEYCODE: u32 = 9;

    const K_CG_EVENT_FLAG_MASK_ALTERNATE: u64 = 0x0008_0000;
    const K_CG_EVENT_FLAG_MASK_CONTROL: u64 = 0x0004_0000;
    const K_CG_EVENT_FLAG_MASK_SHIFT: u64 = 0x0002_0000;
    const K_CG_EVENT_FLAG_MASK_COMMAND: u64 = 0x0010_0000;

    type CGEventTapCallBack = unsafe extern "C" fn(
        proxy: CGEventTapProxy,
        event_type: CGEventType,
        event: CGEventRef,
        user_info: *mut c_void,
    ) -> CGEventRef;

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGEventTapCreate(
            tap: u32,
            place: u32,
            options: u32,
            events_of_interest: CGEventMask,
            callback: CGEventTapCallBack,
            user_info: *mut c_void,
        ) -> CFMachPortRef;
        fn CGEventTapEnable(tap: CFMachPortRef, enable: bool);
        fn CGEventGetIntegerValueField(event: CGEventRef, field: u32) -> i64;
        fn CGEventGetFlags(event: CGEventRef) -> u64;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        static kCFRunLoopCommonModes: CFStringRef;
        fn CFMachPortCreateRunLoopSource(
            allocator: *const c_void,
            port: CFMachPortRef,
            order: isize,
        ) -> CFRunLoopSourceRef;
        fn CFRunLoopGetCurrent() -> CFRunLoopRef;
        fn CFRunLoopAddSource(rl: CFRunLoopRef, source: CFRunLoopSourceRef, mode: CFStringRef);
        fn CFRunLoopRun();
        fn CFRunLoopStop(rl: CFRunLoopRef);
        fn CFRelease(cf: *const c_void);
    }

    /// macOS 仮想キーコード → Windows VK 相当（`binding.rs` のコード体系に揃える）。
    fn mac_keycode_to_vk(kc: u16) -> Option<u32> {
        match kc {
            61 => Some(0xA5),  // Right Option -> ralt
            58 => Some(0xA4),  // Left Option  -> lalt
            62 => Some(0xA3),  // Right Ctrl   -> rctrl
            59 => Some(0xA2),  // Left Ctrl    -> lctrl
            60 => Some(0xA1),  // Right Shift  -> rshift
            56 => Some(0xA0),  // Left Shift   -> lshift
            54 => Some(0x5C),  // Right Cmd    -> rwin
            55 => Some(0x5B),  // Left Cmd     -> lwin
            53 => Some(0x1B),  // Escape       -> esc
            49 => Some(0x20),  // Space
            122 => Some(0x70), // F1
            120 => Some(0x71), // F2
            99 => Some(0x72),  // F3
            118 => Some(0x73), // F4
            96 => Some(0x74),  // F5
            97 => Some(0x75),  // F6
            98 => Some(0x76),  // F7
            100 => Some(0x77), // F8
            101 => Some(0x78), // F9
            109 => Some(0x79), // F10
            103 => Some(0x7A), // F11
            111 => Some(0x7B), // F12
            _ => None,
        }
    }

    struct MacHookState {
        cfg: HotkeyConfig,
        primary_down: bool,
        ai_down: bool,
        tx: Sender<HotkeyEvent>,
    }

    impl MacHookState {
        fn handle(&mut self, b: Binding, is_down: bool, is_up: bool) -> bool {
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

    static MAC_STATE: OnceLock<Mutex<Option<MacHookState>>> = OnceLock::new();

    unsafe extern "C" fn tap_callback(
        _proxy: CGEventTapProxy,
        event_type: CGEventType,
        event: CGEventRef,
        _user_info: *mut c_void,
    ) -> CGEventRef {
        if event.is_null() {
            return event;
        }
        let kc = CGEventGetIntegerValueField(event, K_CG_KEYBOARD_EVENT_KEYCODE) as u16;
        if let Some(vk) = mac_keycode_to_vk(kc) {
            let (is_down, is_up) = match event_type {
                K_CG_EVENT_KEY_DOWN => (true, false),
                K_CG_EVENT_KEY_UP => (false, true),
                K_CG_EVENT_FLAGS_CHANGED => {
                    let flags = CGEventGetFlags(event);
                    let active = match kc {
                        58 | 61 => (flags & K_CG_EVENT_FLAG_MASK_ALTERNATE) != 0,
                        59 | 62 => (flags & K_CG_EVENT_FLAG_MASK_CONTROL) != 0,
                        56 | 60 => (flags & K_CG_EVENT_FLAG_MASK_SHIFT) != 0,
                        54 | 55 => (flags & K_CG_EVENT_FLAG_MASK_COMMAND) != 0,
                        _ => false,
                    };
                    (active, !active)
                }
                _ => (false, false),
            };
            if is_down || is_up {
                if let Some(lock) = MAC_STATE.get() {
                    if let Ok(mut g) = lock.lock() {
                        if let Some(st) = g.as_mut() {
                            if st.handle(Binding::Key(vk), is_down, is_up) {
                                return ptr::null_mut();
                            }
                        }
                    }
                }
            }
        }
        event
    }

    pub struct GlobalHotkey {
        run_loop: usize,
        join: Option<thread::JoinHandle<()>>,
    }

    impl GlobalHotkey {
        pub fn start(cfg: HotkeyConfig, tx: Sender<HotkeyEvent>) -> Result<Self, String> {
            let lock = MAC_STATE.get_or_init(|| Mutex::new(None));
            *lock.lock().map_err(|_| "lock poisoned")? = Some(MacHookState {
                cfg,
                primary_down: false,
                ai_down: false,
                tx,
            });
            let (rl_tx, rl_rx) = std::sync::mpsc::channel::<Result<usize, String>>();
            let join = thread::spawn(move || unsafe {
                let mask: CGEventMask = (1u64 << K_CG_EVENT_KEY_DOWN)
                    | (1u64 << K_CG_EVENT_KEY_UP)
                    | (1u64 << K_CG_EVENT_FLAGS_CHANGED);
                let tap = CGEventTapCreate(
                    K_CG_HID_EVENT_TAP,
                    K_CG_HEAD_INSERT_EVENT_TAP,
                    K_CG_EVENT_TAP_OPTION_DEFAULT,
                    mask,
                    tap_callback,
                    ptr::null_mut(),
                );
                if tap.is_null() {
                    let _ = rl_tx.send(Err(
                        "アクセシビリティ権限が必要です（システム設定 → プライバシーとセキュリティ → アクセシビリティ で OnseiMan を許可してください）".into()
                    ));
                    return;
                }
                let src = CFMachPortCreateRunLoopSource(ptr::null(), tap, 0);
                let rl = CFRunLoopGetCurrent();
                CFRunLoopAddSource(rl, src, kCFRunLoopCommonModes);
                CGEventTapEnable(tap, true);
                let _ = rl_tx.send(Ok(rl as usize));
                CFRunLoopRun();
                CFRelease(src);
                CFRelease(tap);
            });
            let run_loop = rl_rx
                .recv()
                .map_err(|_| "macOS ホットキースレッドの起動に失敗しました".to_string())??;
            Ok(Self {
                run_loop,
                join: Some(join),
            })
        }
    }

    impl Drop for GlobalHotkey {
        fn drop(&mut self) {
            if self.run_loop != 0 {
                unsafe {
                    CFRunLoopStop(self.run_loop as CFRunLoopRef);
                }
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
            if let Some(lock) = MAC_STATE.get() {
                if let Ok(mut g) = lock.lock() {
                    *g = None;
                }
            }
        }
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod imp {
    use super::*;
    use std::sync::mpsc::Sender;

    pub struct GlobalHotkey;

    impl GlobalHotkey {
        pub fn start(_cfg: HotkeyConfig, _tx: Sender<HotkeyEvent>) -> Result<Self, String> {
            Err("この OS ではグローバルホットキー監視に未対応です".into())
        }
    }
}

pub use imp::GlobalHotkey;
