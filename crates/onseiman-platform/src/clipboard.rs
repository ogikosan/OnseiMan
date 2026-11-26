//! クリップボードのテキスト読み書き（Windows: Win32 Clipboard API / macOS: pbpaste・pbcopy）。

#[cfg(windows)]
mod imp {
    use std::ptr;
    use std::thread;
    use std::time::Duration;
    use windows_sys::Win32::Foundation::GlobalFree;
    use windows_sys::Win32::System::DataExchange::{
        CloseClipboard, EmptyClipboard, GetClipboardData, IsClipboardFormatAvailable,
        OpenClipboard, SetClipboardData,
    };
    use windows_sys::Win32::System::Memory::{
        GlobalAlloc, GlobalLock, GlobalUnlock, GMEM_MOVEABLE,
    };

    const CF_UNICODETEXT: u32 = 13;

    fn open_retry() -> bool {
        for _ in 0..12 {
            // SAFETY: Win32 OpenClipboard with NULL hwnd.
            if unsafe { OpenClipboard(ptr::null_mut()) } != 0 {
                return true;
            }
            thread::sleep(Duration::from_millis(15));
        }
        false
    }

    pub fn get_text() -> Option<String> {
        if unsafe { IsClipboardFormatAvailable(CF_UNICODETEXT) } == 0 {
            return None;
        }
        if !open_retry() {
            return None;
        }
        let result = unsafe {
            let h = GetClipboardData(CF_UNICODETEXT);
            if h.is_null() {
                None
            } else {
                let ptr = GlobalLock(h) as *const u16;
                if ptr.is_null() {
                    None
                } else {
                    let mut len = 0usize;
                    while *ptr.add(len) != 0 {
                        len += 1;
                    }
                    let slice = std::slice::from_raw_parts(ptr, len);
                    let text = String::from_utf16_lossy(slice);
                    GlobalUnlock(h);
                    Some(text)
                }
            }
        };
        unsafe {
            CloseClipboard();
        }
        result
    }

    pub fn set_text(text: &str) -> bool {
        if !open_retry() {
            return false;
        }
        let ok = unsafe {
            EmptyClipboard();
            let mut wide: Vec<u16> = text.encode_utf16().collect();
            wide.push(0);
            let bytes = wide.len() * std::mem::size_of::<u16>();
            let hmem = GlobalAlloc(GMEM_MOVEABLE, bytes);
            if hmem.is_null() {
                false
            } else {
                let dst = GlobalLock(hmem) as *mut u16;
                if dst.is_null() {
                    GlobalFree(hmem);
                    false
                } else {
                    ptr::copy_nonoverlapping(wide.as_ptr(), dst, wide.len());
                    GlobalUnlock(hmem);
                    if SetClipboardData(CF_UNICODETEXT, hmem).is_null() {
                        GlobalFree(hmem);
                        false
                    } else {
                        true
                    }
                }
            }
        };
        unsafe {
            CloseClipboard();
        }
        ok
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use std::io::Write;
    use std::process::{Command, Stdio};

    pub fn get_text() -> Option<String> {
        let out = Command::new("/usr/bin/pbpaste").output().ok()?;
        if !out.status.success() {
            return None;
        }
        Some(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    pub fn set_text(text: &str) -> bool {
        let Ok(mut child) = Command::new("/usr/bin/pbcopy")
            .stdin(Stdio::piped())
            .spawn()
        else {
            return false;
        };
        if let Some(mut stdin) = child.stdin.take() {
            if stdin.write_all(text.as_bytes()).is_err() {
                return false;
            }
        }
        child.wait().map(|s| s.success()).unwrap_or(false)
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod imp {
    pub fn get_text() -> Option<String> {
        None
    }
    pub fn set_text(_text: &str) -> bool {
        false
    }
}

pub use imp::{get_text, set_text};
