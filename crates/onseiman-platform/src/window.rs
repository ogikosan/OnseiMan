//! 既存の OnseiMan 管理画面ウィンドウ（Edge / Chrome `--app` モード）を探して前面に出す。

/// タイトルが一致する Chromium 系アプリウィンドウがすでに開いていれば前面に出し `true` を返す。
#[cfg(windows)]
pub fn bring_existing_app_window_to_front(title: &str) -> bool {
    use windows_sys::Win32::Foundation::{BOOL, HWND, LPARAM};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetClassNameW, GetWindowTextW, IsIconic, IsWindowVisible, SetForegroundWindow,
        ShowWindow, SW_RESTORE,
    };

    struct Search<'a> {
        want_title: &'a str,
        found: HWND,
    }

    unsafe extern "system" fn enum_cb(hwnd: HWND, lparam: LPARAM) -> BOOL {
        let search = &mut *(lparam as *mut Search<'_>);
        if IsWindowVisible(hwnd) == 0 {
            return 1;
        }
        let mut tbuf = [0u16; 256];
        let tlen = GetWindowTextW(hwnd, tbuf.as_mut_ptr(), tbuf.len() as i32);
        if tlen <= 0 {
            return 1;
        }
        let text = String::from_utf16_lossy(&tbuf[..tlen as usize]);
        if text != search.want_title {
            return 1;
        }
        let mut cbuf = [0u16; 256];
        let clen = GetClassNameW(hwnd, cbuf.as_mut_ptr(), cbuf.len() as i32);
        if clen <= 0 {
            return 1;
        }
        let cls = String::from_utf16_lossy(&cbuf[..clen as usize]);
        if cls.starts_with("Chrome_WidgetWin") {
            search.found = hwnd;
            return 0; // stop
        }
        1
    }

    let mut search = Search {
        want_title: title,
        found: std::ptr::null_mut(),
    };
    unsafe {
        EnumWindows(Some(enum_cb), (&mut search as *mut Search<'_>) as LPARAM);
        if search.found.is_null() {
            return false;
        }
        if IsIconic(search.found) != 0 {
            ShowWindow(search.found, SW_RESTORE);
        }
        SetForegroundWindow(search.found);
        true
    }
}

#[cfg(not(windows))]
pub fn bring_existing_app_window_to_front(_title: &str) -> bool {
    false
}
