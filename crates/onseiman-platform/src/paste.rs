//! テキストの貼り付け（クリップボード＋Ctrl+V / Cmd+V）。

use std::thread;
use std::time::Duration;

use crate::clipboard;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PasteResult {
    pub ok: bool,
    pub reason: &'static str,
    pub clipboard_kept: bool,
    pub hwnd: isize,
}

#[cfg(windows)]
mod imp {
    use super::*;
    use std::mem::{size_of, zeroed};
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
        GetAsyncKeyState, SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP,
        VK_CONTROL, VK_LMENU, VK_LWIN, VK_MENU, VK_RMENU, VK_RWIN, VK_SHIFT,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, IsWindow, SetForegroundWindow,
    };

    const MENU_MASK_VK: u16 = 0xE8;
    const VK_V: u16 = 0x56;
    const VK_Z: u16 = 0x5A;

    fn key_input(vk: u16, up: bool) -> INPUT {
        INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: vk,
                    wScan: 0,
                    dwFlags: if up { KEYEVENTF_KEYUP } else { 0 },
                    time: 0,
                    dwExtraInfo: 0,
                },
            },
        }
    }

    pub fn capture_paste_target() -> isize {
        unsafe { GetForegroundWindow() as isize }
    }

    /// キャプチャ済みウィンドウハンドルから実行ファイル名（例: `slack.exe`）を取得する。
    pub fn target_process_name(target_hwnd: isize) -> Option<String> {
        use windows_sys::Win32::Foundation::{CloseHandle, HWND};
        use windows_sys::Win32::System::Threading::{
            OpenProcess, QueryFullProcessImageNameW, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        use windows_sys::Win32::UI::WindowsAndMessaging::GetWindowThreadProcessId;

        if target_hwnd == 0 {
            return None;
        }
        unsafe {
            let mut pid: u32 = 0;
            GetWindowThreadProcessId(target_hwnd as HWND, &mut pid);
            if pid == 0 {
                return None;
            }
            let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if h.is_null() {
                return None;
            }
            let mut buf = vec![0u16; 1024];
            let mut len = buf.len() as u32;
            let ok = QueryFullProcessImageNameW(h, 0, buf.as_mut_ptr(), &mut len) != 0;
            CloseHandle(h);
            if !ok || len == 0 {
                return None;
            }
            let full = String::from_utf16_lossy(&buf[..len as usize]);
            full.rsplit(['\\', '/'])
                .next()
                .filter(|s| !s.is_empty())
                .map(|s| s.to_lowercase())
        }
    }

    fn release_stuck_modifiers() {
        let mods = [VK_MENU, VK_LMENU, VK_RMENU, VK_SHIFT, VK_LWIN, VK_RWIN];
        let mut ups = Vec::new();
        for vk in mods {
            if (unsafe { GetAsyncKeyState(vk as i32) } as u16) & 0x8000 != 0 {
                ups.push(key_input(vk, true));
            }
        }
        if !ups.is_empty() {
            let mut seq = vec![
                key_input(MENU_MASK_VK, false),
                key_input(MENU_MASK_VK, true),
            ];
            seq.extend(ups);
            unsafe {
                SendInput(seq.len() as u32, seq.as_ptr(), size_of::<INPUT>() as i32);
            }
            thread::sleep(Duration::from_millis(20));
        }
    }

    pub fn send_undo_shortcut(target_hwnd: isize) -> bool {
        let mut fg = unsafe { GetForegroundWindow() };
        if target_hwnd != 0 && fg.is_null() && unsafe { IsWindow(target_hwnd as _) } != 0 {
            unsafe {
                SetForegroundWindow(target_hwnd as _);
            }
            fg = unsafe { GetForegroundWindow() };
        }
        if fg.is_null() {
            return false;
        }
        release_stuck_modifiers();
        let seq = [
            key_input(VK_CONTROL, false),
            key_input(VK_Z, false),
            key_input(VK_Z, true),
            key_input(VK_CONTROL, true),
        ];
        let sent = unsafe { SendInput(seq.len() as u32, seq.as_ptr(), size_of::<INPUT>() as i32) };
        if sent == seq.len() as u32 {
            return true;
        }
        if sent > 0 {
            let rel = [key_input(VK_CONTROL, true)];
            unsafe {
                SendInput(1, rel.as_ptr(), size_of::<INPUT>() as i32);
            }
        }
        false
    }

    pub fn send_paste_shortcut() -> bool {
        release_stuck_modifiers();
        let seq = [
            key_input(VK_CONTROL, false),
            key_input(VK_V, false),
            key_input(VK_V, true),
            key_input(VK_CONTROL, true),
        ];
        let sent = unsafe { SendInput(seq.len() as u32, seq.as_ptr(), size_of::<INPUT>() as i32) };
        if sent == seq.len() as u32 {
            return true;
        }
        if sent > 0 {
            let rel = [key_input(VK_CONTROL, true)];
            unsafe {
                SendInput(1, rel.as_ptr(), size_of::<INPUT>() as i32);
            }
        }
        false
    }

    pub fn paste_text(
        text: &str,
        restore_clipboard: bool,
        paste_delay_ms: u32,
        restore_delay_ms: u32,
        target_hwnd: isize,
    ) -> PasteResult {
        if text.is_empty() {
            return PasteResult {
                ok: false,
                reason: "empty",
                clipboard_kept: false,
                hwnd: 0,
            };
        }
        let mut fg = unsafe { GetForegroundWindow() };
        if target_hwnd != 0 && fg.is_null() && unsafe { IsWindow(target_hwnd as _) } != 0 {
            unsafe {
                SetForegroundWindow(target_hwnd as _);
            }
            fg = unsafe { GetForegroundWindow() };
        }
        let _unused: INPUT = unsafe { zeroed() };
        let prev = if restore_clipboard {
            clipboard::get_text()
        } else {
            None
        };
        if !clipboard::set_text(text) {
            return PasteResult {
                ok: false,
                reason: "clipboard_failed",
                clipboard_kept: false,
                hwnd: fg as isize,
            };
        }
        if fg.is_null() {
            return PasteResult {
                ok: false,
                reason: "no_window",
                clipboard_kept: true,
                hwnd: 0,
            };
        }
        if paste_delay_ms > 0 {
            thread::sleep(Duration::from_millis(paste_delay_ms as u64));
        }
        if !send_paste_shortcut() {
            return PasteResult {
                ok: false,
                reason: "sendinput_failed",
                clipboard_kept: true,
                hwnd: fg as isize,
            };
        }
        if restore_clipboard {
            if let Some(p) = prev {
                thread::sleep(Duration::from_millis(restore_delay_ms.max(150) as u64));
                clipboard::set_text(&p);
                return PasteResult {
                    ok: true,
                    reason: "pasted",
                    clipboard_kept: false,
                    hwnd: fg as isize,
                };
            }
        }
        PasteResult {
            ok: true,
            reason: "pasted",
            clipboard_kept: true,
            hwnd: fg as isize,
        }
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::*;
    use std::process::Command;

    pub fn capture_paste_target() -> isize {
        0
    }

    pub fn target_process_name(_target_hwnd: isize) -> Option<String> {
        None
    }

    pub fn send_undo_shortcut(_target_hwnd: isize) -> bool {
        Command::new("/usr/bin/osascript")
            .args([
                "-e",
                r#"tell application "System Events" to keystroke "z" using command down"#,
            ])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    pub fn send_paste_shortcut() -> bool {
        Command::new("/usr/bin/osascript")
            .args([
                "-e",
                r#"tell application "System Events" to keystroke "v" using command down"#,
            ])
            .status()
            .map(|s| s.success())
            .unwrap_or(false)
    }

    pub fn paste_text(
        text: &str,
        restore_clipboard: bool,
        paste_delay_ms: u32,
        restore_delay_ms: u32,
        _target_hwnd: isize,
    ) -> PasteResult {
        if text.is_empty() {
            return PasteResult {
                ok: false,
                reason: "empty",
                clipboard_kept: false,
                hwnd: 0,
            };
        }
        let prev = if restore_clipboard {
            clipboard::get_text()
        } else {
            None
        };
        if !clipboard::set_text(text) {
            return PasteResult {
                ok: false,
                reason: "clipboard_failed",
                clipboard_kept: false,
                hwnd: 0,
            };
        }
        if paste_delay_ms > 0 {
            thread::sleep(Duration::from_millis(paste_delay_ms as u64));
        }
        if !send_paste_shortcut() {
            return PasteResult {
                ok: false,
                reason: "sendinput_failed",
                clipboard_kept: true,
                hwnd: 0,
            };
        }
        if restore_clipboard {
            if let Some(p) = prev {
                thread::sleep(Duration::from_millis(restore_delay_ms.max(150) as u64));
                clipboard::set_text(&p);
                return PasteResult {
                    ok: true,
                    reason: "pasted",
                    clipboard_kept: false,
                    hwnd: 0,
                };
            }
        }
        PasteResult {
            ok: true,
            reason: "pasted",
            clipboard_kept: true,
            hwnd: 0,
        }
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod imp {
    use super::*;
    pub fn capture_paste_target() -> isize {
        0
    }
    pub fn target_process_name(_target_hwnd: isize) -> Option<String> {
        None
    }
    pub fn send_undo_shortcut(_target_hwnd: isize) -> bool {
        false
    }
    pub fn send_paste_shortcut() -> bool {
        false
    }
    pub fn paste_text(
        _text: &str,
        _restore_clipboard: bool,
        _paste_delay_ms: u32,
        _restore_delay_ms: u32,
        _target_hwnd: isize,
    ) -> PasteResult {
        PasteResult {
            ok: false,
            reason: "unsupported",
            clipboard_kept: false,
            hwnd: 0,
        }
    }
}

pub use imp::{
    capture_paste_target, paste_text, send_paste_shortcut, send_undo_shortcut, target_process_name,
};
