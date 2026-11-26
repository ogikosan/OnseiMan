//! 録音中・認識中の非アクティブ・最前面カプセルオーバーレイ。
//!
//! Windows では `WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` で
//! 入力先ウィンドウのフォーカスを一切奪わずに画面下部中央へ表示する。

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum OverlayState {
    Hidden,
    Recording { ai: bool, level: f32 },
    Transcribing,
}

#[cfg(windows)]
mod imp {
    use super::OverlayState;
    use std::ptr;
    use std::sync::{Arc, Mutex};
    use std::thread;
    use windows_sys::Win32::Foundation::{COLORREF, HWND, LPARAM, LRESULT, RECT, WPARAM};
    use windows_sys::Win32::Graphics::Gdi::{
        BeginPaint, CreatePen, CreateSolidBrush, DeleteObject, DrawTextW, EndPaint, FillRect,
        InvalidateRect, RoundRect, SelectObject, SetBkMode, SetTextColor, DT_CENTER, DT_SINGLELINE,
        DT_VCENTER, PAINTSTRUCT, PS_SOLID, TRANSPARENT,
    };
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetMessageW,
        GetSystemMetrics, PostMessageW, RegisterClassW, SetLayeredWindowAttributes, ShowWindow,
        TranslateMessage, LWA_ALPHA, MSG, SM_CXSCREEN, SM_CYSCREEN, SW_HIDE, SW_SHOWNOACTIVATE,
        WM_APP, WM_CLOSE, WM_DESTROY, WM_MOUSEACTIVATE, WM_PAINT, WNDCLASSW, WS_EX_LAYERED,
        WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP,
    };

    const WM_OVERLAY_UPDATE: u32 = WM_APP + 10;
    const MA_NOACTIVATE: isize = 3;
    const PILL_W: i32 = 220;
    const PILL_H: i32 = 42;

    static CURRENT: Mutex<OverlayState> = Mutex::new(OverlayState::Hidden);

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    fn rgb(r: u8, g: u8, b: u8) -> COLORREF {
        (r as u32) | ((g as u32) << 8) | ((b as u32) << 16)
    }

    unsafe extern "system" fn wnd_proc(
        hwnd: HWND,
        msg: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        match msg {
            WM_MOUSEACTIVATE => return MA_NOACTIVATE,
            WM_OVERLAY_UPDATE => {
                let st = *CURRENT.lock().unwrap();
                match st {
                    OverlayState::Hidden => {
                        ShowWindow(hwnd, SW_HIDE);
                    }
                    _ => {
                        ShowWindow(hwnd, SW_SHOWNOACTIVATE);
                        InvalidateRect(hwnd, ptr::null(), 1);
                    }
                }
                return 0;
            }
            WM_PAINT => {
                let st = *CURRENT.lock().unwrap();
                let mut ps: PAINTSTRUCT = std::mem::zeroed();
                let hdc = BeginPaint(hwnd, &mut ps);
                let mut rc = RECT {
                    left: 0,
                    top: 0,
                    right: PILL_W,
                    bottom: PILL_H,
                };
                let bg = CreateSolidBrush(rgb(24, 24, 30));
                FillRect(hdc, &rc, bg);
                DeleteObject(bg);

                let (border_col, dot_col, label, level) = match st {
                    OverlayState::Recording { ai: true, level } => {
                        (rgb(168, 85, 247), rgb(192, 132, 252), "● AI 録音中…", level)
                    }
                    OverlayState::Recording { ai: false, level } => {
                        (rgb(239, 68, 68), rgb(248, 113, 113), "● 録音中…", level)
                    }
                    OverlayState::Transcribing => {
                        (rgb(59, 130, 246), rgb(96, 165, 250), "… 認識中", 0.0)
                    }
                    OverlayState::Hidden => (rgb(60, 60, 70), rgb(120, 120, 130), "", 0.0),
                };

                let pen = CreatePen(PS_SOLID, 2, border_col);
                let fill = CreateSolidBrush(rgb(28, 30, 38));
                let old_pen = SelectObject(hdc, pen);
                let old_brush = SelectObject(hdc, fill);
                RoundRect(hdc, 1, 1, PILL_W - 1, PILL_H - 1, 20, 20);
                SelectObject(hdc, old_pen);
                SelectObject(hdc, old_brush);
                DeleteObject(pen);
                DeleteObject(fill);

                // 音量バー（下部）
                if level > 0.001 {
                    let frac = (level * 12.0).clamp(0.05, 1.0);
                    let bar_w = ((PILL_W - 32) as f32 * frac) as i32;
                    let bar_rc = RECT {
                        left: 16,
                        top: PILL_H - 6,
                        right: 16 + bar_w,
                        bottom: PILL_H - 3,
                    };
                    let bar_brush = CreateSolidBrush(dot_col);
                    FillRect(hdc, &bar_rc, bar_brush);
                    DeleteObject(bar_brush);
                }

                SetBkMode(hdc, TRANSPARENT as i32);
                SetTextColor(hdc, rgb(244, 244, 248));
                let mut text = wide(label);
                rc.bottom -= 4;
                DrawTextW(
                    hdc,
                    text.as_mut_ptr(),
                    (text.len() - 1) as i32,
                    &mut rc,
                    DT_CENTER | DT_VCENTER | DT_SINGLELINE,
                );
                EndPaint(hwnd, &ps);
                return 0;
            }
            WM_CLOSE => {
                DestroyWindow(hwnd);
                return 0;
            }
            WM_DESTROY => {
                windows_sys::Win32::UI::WindowsAndMessaging::PostQuitMessage(0);
                return 0;
            }
            _ => {}
        }
        DefWindowProcW(hwnd, msg, wparam, lparam)
    }

    pub struct RecordingOverlay {
        hwnd: Arc<Mutex<isize>>,
        join: Option<thread::JoinHandle<()>>,
    }

    impl RecordingOverlay {
        pub fn start() -> Self {
            let hwnd_cell = Arc::new(Mutex::new(0isize));
            let hwnd_bg = Arc::clone(&hwnd_cell);
            let join = thread::spawn(move || unsafe {
                let hinst = GetModuleHandleW(ptr::null());
                let cls_name = wide("OnseiManOverlayClass");
                let wc = WNDCLASSW {
                    style: 0,
                    lpfnWndProc: Some(wnd_proc),
                    cbClsExtra: 0,
                    cbWndExtra: 0,
                    hInstance: hinst,
                    hIcon: ptr::null_mut(),
                    hCursor: ptr::null_mut(),
                    hbrBackground: ptr::null_mut(),
                    lpszMenuName: ptr::null(),
                    lpszClassName: cls_name.as_ptr(),
                };
                RegisterClassW(&wc);

                let sx = GetSystemMetrics(SM_CXSCREEN);
                let sy = GetSystemMetrics(SM_CYSCREEN);
                let x = (sx - PILL_W) / 2;
                let y = sy - PILL_H - 72;

                let title = wide("OnseiMan");
                let hwnd = CreateWindowExW(
                    WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
                    cls_name.as_ptr(),
                    title.as_ptr(),
                    WS_POPUP,
                    x,
                    y,
                    PILL_W,
                    PILL_H,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    hinst,
                    ptr::null(),
                );
                if hwnd.is_null() {
                    return;
                }
                SetLayeredWindowAttributes(hwnd, 0, 238, LWA_ALPHA);
                if let Ok(mut g) = hwnd_bg.lock() {
                    *g = hwnd as isize;
                }

                let mut msg: MSG = std::mem::zeroed();
                while GetMessageW(&mut msg, ptr::null_mut(), 0, 0) > 0 {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
            });
            Self {
                hwnd: hwnd_cell,
                join: Some(join),
            }
        }

        pub fn set_state(&self, state: OverlayState) {
            if let Ok(mut g) = CURRENT.lock() {
                *g = state;
            }
            if let Ok(h) = self.hwnd.lock() {
                if *h != 0 {
                    unsafe {
                        PostMessageW(*h as HWND, WM_OVERLAY_UPDATE, 0, 0);
                    }
                }
            }
        }
    }

    impl Drop for RecordingOverlay {
        fn drop(&mut self) {
            if let Ok(h) = self.hwnd.lock() {
                if *h != 0 {
                    unsafe {
                        PostMessageW(*h as HWND, WM_CLOSE, 0, 0);
                    }
                }
            }
            if let Some(j) = self.join.take() {
                let _ = j.join();
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::OverlayState;

    pub struct RecordingOverlay;
    impl RecordingOverlay {
        pub fn start() -> Self {
            Self
        }
        pub fn set_state(&self, _state: OverlayState) {}
    }
}

pub use imp::RecordingOverlay;
