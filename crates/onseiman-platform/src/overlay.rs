//! 録音中・認識中の非アクティブ・最前面カプセルオーバーレイ。
//!
//! 元の OnseiMan（`pill.py`）と同じデザイン：
//! - 角が完全に丸いカプセル形状（`CreateRoundRectRgn` + `SetWindowRgn` で角の黒枠なし）
//! - 左に青い丸＋声の大きさで広がる輪、右に Yu Gothic UI のなめらかな文字
//! - `WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE` で入力先のフォーカスを一切奪わない

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
        BeginPaint, CreateFontW, CreatePen, CreateRoundRectRgn, CreateSolidBrush, DeleteObject,
        DrawTextW, Ellipse, EndPaint, FillRect, InvalidateRect, RoundRect, SelectObject, SetBkMode,
        SetTextColor, SetWindowRgn, CLEARTYPE_QUALITY, CLIP_DEFAULT_PRECIS, DEFAULT_CHARSET,
        DEFAULT_PITCH, DT_LEFT, DT_SINGLELINE, DT_VCENTER, FF_DONTCARE, FW_MEDIUM,
        OUT_DEFAULT_PRECIS, PAINTSTRUCT, PS_NULL, PS_SOLID, TRANSPARENT,
    };
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetMessageW,
        GetSystemMetrics, PostMessageW, PostQuitMessage, RegisterClassW,
        SetLayeredWindowAttributes, ShowWindow, TranslateMessage, LWA_ALPHA, MSG, SM_CXSCREEN,
        SM_CYSCREEN, SW_HIDE, SW_SHOWNOACTIVATE, WM_APP, WM_CLOSE, WM_DESTROY, WM_MOUSEACTIVATE,
        WM_PAINT, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST,
        WS_POPUP,
    };

    const WM_OVERLAY_UPDATE: u32 = WM_APP + 10;
    const MA_NOACTIVATE: isize = 3;
    const PILL_W: i32 = 200;
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
                let full = RECT {
                    left: 0,
                    top: 0,
                    right: PILL_W,
                    bottom: PILL_H,
                };
                // 元の pill.py ダークテーマ配色: body (30, 39, 60), edge (68, 82, 112), text (228, 233, 244)
                let body_col = rgb(30, 39, 60);
                let edge_col = rgb(72, 88, 122);
                let text_col = rgb(228, 233, 244);

                let bg = CreateSolidBrush(body_col);
                FillRect(hdc, &full, bg);
                DeleteObject(bg);

                let pen = CreatePen(PS_SOLID, 1, edge_col);
                let fill = CreateSolidBrush(body_col);
                let old_pen = SelectObject(hdc, pen);
                let old_brush = SelectObject(hdc, fill);
                RoundRect(hdc, 0, 0, PILL_W, PILL_H, PILL_H, PILL_H);
                SelectObject(hdc, old_pen);
                SelectObject(hdc, old_brush);
                DeleteObject(pen);
                DeleteObject(fill);

                let (circle_col, halo_col, label, level) = match st {
                    OverlayState::Recording { ai: true, level } => (
                        rgb(139, 92, 246),
                        rgb(92, 68, 160),
                        "AI 聞いています",
                        level,
                    ),
                    OverlayState::Recording { ai: false, level } => {
                        (rgb(59, 130, 246), rgb(47, 94, 178), "聞いています", level)
                    }
                    OverlayState::Transcribing => {
                        (rgb(99, 102, 241), rgb(64, 72, 138), "文字にしています", 0.0)
                    }
                    OverlayState::Hidden => (rgb(100, 116, 139), body_col, "", 0.0),
                };

                // 左の丸（声の大きさで外側の輪が広がる）
                let cx = 22;
                let cy = PILL_H / 2;
                let null_pen = CreatePen(PS_NULL, 0, 0);
                let prev_p = SelectObject(hdc, null_pen);
                if level > 0.02 {
                    let extra = ((level * 7.0).clamp(1.0, 6.0)) as i32;
                    let hr = 11 + extra;
                    let hb = CreateSolidBrush(halo_col);
                    let prev_b = SelectObject(hdc, hb);
                    Ellipse(hdc, cx - hr, cy - hr, cx + hr, cy + hr);
                    SelectObject(hdc, prev_b);
                    DeleteObject(hb);
                }
                let r = 10;
                let cb = CreateSolidBrush(circle_col);
                let prev_b = SelectObject(hdc, cb);
                Ellipse(hdc, cx - r, cy - r, cx + r, cy + r);
                SelectObject(hdc, prev_b);
                SelectObject(hdc, prev_p);
                DeleteObject(cb);
                DeleteObject(null_pen);

                // Yu Gothic UI でなめらかに文字描画
                let font_name = wide("Yu Gothic UI");
                let font = CreateFontW(
                    -15,
                    0,
                    0,
                    0,
                    FW_MEDIUM as i32,
                    0,
                    0,
                    0,
                    DEFAULT_CHARSET as u32,
                    OUT_DEFAULT_PRECIS as u32,
                    CLIP_DEFAULT_PRECIS as u32,
                    CLEARTYPE_QUALITY as u32,
                    (DEFAULT_PITCH | FF_DONTCARE) as u32,
                    font_name.as_ptr(),
                );
                let old_font = SelectObject(hdc, font);
                SetBkMode(hdc, TRANSPARENT as i32);
                SetTextColor(hdc, text_col);
                let mut text = wide(label);
                let mut text_rc = RECT {
                    left: 44,
                    top: 0,
                    right: PILL_W - 14,
                    bottom: PILL_H,
                };
                DrawTextW(
                    hdc,
                    text.as_mut_ptr(),
                    (text.len() - 1) as i32,
                    &mut text_rc,
                    DT_LEFT | DT_VCENTER | DT_SINGLELINE,
                );
                SelectObject(hdc, old_font);
                DeleteObject(font);

                EndPaint(hwnd, &ps);
                return 0;
            }
            WM_CLOSE => {
                DestroyWindow(hwnd);
                return 0;
            }
            WM_DESTROY => {
                PostQuitMessage(0);
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
                let y = sy - PILL_H - 64;

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
                // カプセル型に切り抜いて角の黒枠を完全になくす
                let rgn = CreateRoundRectRgn(0, 0, PILL_W + 1, PILL_H + 1, PILL_H, PILL_H);
                SetWindowRgn(hwnd, rgn, 1);
                SetLayeredWindowAttributes(hwnd, 0, 240, LWA_ALPHA);
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
