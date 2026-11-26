//! 録音中・認識中・完了通知の非アクティブ・最前面カプセルオーバーレイ。
//!
//! 元の OnseiMan（`pill.py`）と同じなめらかな見た目：
//! - `UpdateLayeredWindow`（`ULW_ALPHA`）＋ 32-bit 乗算済み BGRA DIB による
//!   ピクセル単位アンチエイリアス描画（`SetWindowRgn` を使わないためジャギが一切出ない）
//! - 距離場（SDF）でカプセル本体・枠線・青い丸・声の大きさで広がる輪をなめらかに描画
//! - `WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED` でフォーカスを奪わない
//! - 文字カーソル付近への追従表示・ドラッグ移動の位置記憶・完了トーストに対応

#[derive(Debug, Clone, PartialEq)]
pub enum OverlayState {
    Hidden,
    Recording {
        ai: bool,
        level: f32,
        live_text: String,
    },
    Transcribing,
    Done {
        text: String,
    },
}

#[cfg(windows)]
mod imp {
    use super::OverlayState;
    use std::ptr;
    use std::sync::{Arc, Mutex, OnceLock};
    use std::thread;
    use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, RECT, SIZE, WPARAM};
    use windows_sys::Win32::Graphics::Gdi::{
        CreateCompatibleDC, CreateDIBSection, CreateFontW, DeleteDC, DeleteObject, DrawTextW,
        GetDC, GetDeviceCaps, ReleaseDC, SelectObject, SetBkColor, SetBkMode, SetTextColor,
        AC_SRC_ALPHA, AC_SRC_OVER, ANTIALIASED_QUALITY, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
        BLENDFUNCTION, CLIP_DEFAULT_PRECIS, DEFAULT_CHARSET, DEFAULT_PITCH, DIB_RGB_COLORS,
        DT_END_ELLIPSIS, DT_LEFT, DT_SINGLELINE, DT_VCENTER, FF_DONTCARE, FW_MEDIUM, LOGPIXELSX,
        OUT_DEFAULT_PRECIS, TRANSPARENT,
    };
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        CreateWindowExW, DefWindowProcW, DestroyWindow, DispatchMessageW, GetMessageW,
        GetSystemMetrics, GetWindowRect, PostMessageW, PostQuitMessage, RegisterClassW,
        SendMessageW, SetProcessDPIAware, SetWindowPos, ShowWindow, TranslateMessage,
        UpdateLayeredWindow, HTCAPTION, HWND_TOPMOST, MSG, SM_CXSCREEN, SM_CYSCREEN,
        SWP_NOACTIVATE, SWP_NOSIZE, SW_HIDE, SW_SHOWNOACTIVATE, ULW_ALPHA, WM_APP, WM_CLOSE,
        WM_DESTROY, WM_EXITSIZEMOVE, WM_LBUTTONDBLCLK, WM_LBUTTONDOWN, WM_MOUSEACTIVATE,
        WM_NCLBUTTONDOWN, WNDCLASSW, WS_EX_LAYERED, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
        WS_EX_TOPMOST, WS_POPUP,
    };

    const WM_OVERLAY_UPDATE: u32 = WM_APP + 10;
    const WM_OVERLAY_MOVE: u32 = WM_APP + 11;
    const MA_NOACTIVATE: isize = 3;
    const BASE_PILL_W: f32 = 228.0;
    const BASE_PILL_H: f32 = 44.0;

    unsafe fn dpi_scale() -> f32 {
        let dc = GetDC(ptr::null_mut());
        let dpi = if dc.is_null() {
            96
        } else {
            let v = GetDeviceCaps(dc, LOGPIXELSX as i32);
            ReleaseDC(ptr::null_mut(), dc);
            v
        };
        (dpi as f32 / 96.0).clamp(1.0, 3.0)
    }

    unsafe fn pill_size() -> (i32, i32, f32) {
        let s = dpi_scale();
        (
            (BASE_PILL_W * s).round() as i32,
            (BASE_PILL_H * s).round() as i32,
            s,
        )
    }

    static CURRENT: OnceLock<Mutex<OverlayState>> = OnceLock::new();
    static TARGET_POS: OnceLock<Mutex<(i32, i32)>> = OnceLock::new();
    static PINNED_POS: OnceLock<Mutex<Option<(i32, i32)>>> = OnceLock::new();

    fn current_lock() -> &'static Mutex<OverlayState> {
        CURRENT.get_or_init(|| Mutex::new(OverlayState::Hidden))
    }

    fn pinned_lock() -> &'static Mutex<Option<(i32, i32)>> {
        PINNED_POS.get_or_init(|| Mutex::new(None))
    }

    fn target_pos_lock() -> &'static Mutex<(i32, i32)> {
        TARGET_POS.get_or_init(|| Mutex::new((0, 0)))
    }

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// カプセル（角丸長方形）の符号付き距離関数（内側が負、外側が正）。
    #[inline]
    fn sd_capsule(px: f32, py: f32, w: f32, h: f32, pad: f32) -> f32 {
        let r = (h - 2.0 * pad) * 0.5;
        let cx0 = pad + r;
        let cx1 = w - pad - r;
        let cy = h * 0.5;
        let qx = px.clamp(cx0, cx1);
        let dx = px - qx;
        let dy = py - cy;
        (dx * dx + dy * dy).sqrt() - r
    }

    /// 乗算済みアルファの合成（dst の上に src(r,g,b,a) を載せる）。
    #[inline]
    fn blend_over(dst: &mut [u8], r: f32, g: f32, b: f32, a: f32) {
        if a <= 0.0 {
            return;
        }
        let inv = 1.0 - a;
        let db = dst[0] as f32 / 255.0;
        let dg = dst[1] as f32 / 255.0;
        let dr = dst[2] as f32 / 255.0;
        let da = dst[3] as f32 / 255.0;
        let out_b = (b * a + db * inv).clamp(0.0, 1.0);
        let out_g = (g * a + dg * inv).clamp(0.0, 1.0);
        let out_r = (r * a + dr * inv).clamp(0.0, 1.0);
        let out_a = (a + da * inv).clamp(0.0, 1.0);
        dst[0] = (out_b * 255.0).round() as u8;
        dst[1] = (out_g * 255.0).round() as u8;
        dst[2] = (out_r * 255.0).round() as u8;
        dst[3] = (out_a * 255.0).round() as u8;
    }

    /// ピクセル単位アンチエイリアスのカプセル＋文字を 32-bit 乗算済み BGRA DIB へ描画し
    /// `UpdateLayeredWindow` でウィンドウに反映する。
    unsafe fn render_layered(hwnd: HWND, st: &OverlayState) {
        let (dot_rgb, halo_rgb, label, level) = match st {
            OverlayState::Recording {
                ai: true,
                level,
                live_text,
            } => {
                let lbl = if live_text.is_empty() {
                    "AI 聞いています".to_string()
                } else {
                    live_text.clone()
                };
                (
                    (139.0 / 255.0, 92.0 / 255.0, 246.0 / 255.0),
                    (139.0 / 255.0, 92.0 / 255.0, 246.0 / 255.0),
                    lbl,
                    *level,
                )
            }
            OverlayState::Recording {
                ai: false,
                level,
                live_text,
            } => {
                let lbl = if live_text.is_empty() {
                    "聞いています".to_string()
                } else {
                    live_text.clone()
                };
                (
                    (59.0 / 255.0, 130.0 / 255.0, 246.0 / 255.0),
                    (59.0 / 255.0, 130.0 / 255.0, 246.0 / 255.0),
                    lbl,
                    *level,
                )
            }
            OverlayState::Transcribing => (
                (99.0 / 255.0, 102.0 / 255.0, 241.0 / 255.0),
                (99.0 / 255.0, 102.0 / 255.0, 241.0 / 255.0),
                "文字にしています".to_string(),
                0.0,
            ),
            OverlayState::Done { text } => (
                (16.0 / 255.0, 185.0 / 255.0, 129.0 / 255.0),
                (16.0 / 255.0, 185.0 / 255.0, 129.0 / 255.0),
                format!("✓ {text}"),
                0.0,
            ),
            OverlayState::Hidden => return,
        };

        let (w, h, s) = pill_size();
        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h, // top-down
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB,
                biSizeImage: 0,
                biXPelsPerMeter: 0,
                biYPelsPerMeter: 0,
                biClrUsed: 0,
                biClrImportant: 0,
            },
            bmiColors: [std::mem::zeroed(); 1],
        };

        let screen_dc = GetDC(ptr::null_mut());
        let mem_dc = CreateCompatibleDC(screen_dc);
        let mut bits: *mut std::ffi::c_void = ptr::null_mut();
        let hbm = CreateDIBSection(mem_dc, &bmi, DIB_RGB_COLORS, &mut bits, ptr::null_mut(), 0);
        if hbm.is_null() || bits.is_null() {
            if !hbm.is_null() {
                DeleteObject(hbm);
            }
            DeleteDC(mem_dc);
            ReleaseDC(ptr::null_mut(), screen_dc);
            return;
        }
        let old_bm = SelectObject(mem_dc, hbm);
        let buf = std::slice::from_raw_parts_mut(bits as *mut u8, (w * h * 4) as usize);
        buf.fill(0);

        // 1. なめらかなカプセル本体・枠線・左の丸＋声の輪を SDF でアンチエイリアス描画
        let wf = w as f32;
        let hf = h as f32;
        let body_r = 26.0 / 255.0;
        let body_g = 34.0 / 255.0;
        let body_b = 52.0 / 255.0;
        let body_a = 0.94_f32;
        let edge_r = 96.0 / 255.0;
        let edge_g = 114.0 / 255.0;
        let edge_b = 152.0 / 255.0;

        let dot_cx = 23.0_f32 * s;
        let dot_cy = hf * 0.5;
        let dot_r = 9.0_f32 * s;
        let halo_r = if level > 0.02 {
            dot_r + (level * 7.0).clamp(1.5, 6.5) * s
        } else {
            0.0
        };
        let pad = 1.5 * s;
        let border_w = 1.0 * s;

        for py in 0..h {
            let fy = py as f32 + 0.5;
            for px in 0..w {
                let fx = px as f32 + 0.5;
                let idx = ((py * w + px) * 4) as usize;

                // カプセル本体（外側に余白を残してアンチエイリアス＋軽い外縁）
                let d_cap = sd_capsule(fx, fy, wf, hf, pad);
                let cap_cov = (0.5 - d_cap).clamp(0.0, 1.0);
                if cap_cov <= 0.0 {
                    continue;
                }
                blend_over(
                    &mut buf[idx..idx + 4],
                    body_r,
                    body_g,
                    body_b,
                    cap_cov * body_a,
                );

                // なめらかな枠線
                let border_cov =
                    (1.0 - (d_cap + border_w * 0.5).abs() / border_w).clamp(0.0, 1.0) * cap_cov;
                if border_cov > 0.0 {
                    blend_over(
                        &mut buf[idx..idx + 4],
                        edge_r,
                        edge_g,
                        edge_b,
                        border_cov * 0.55,
                    );
                }

                // 声の大きさで広がる輪（アンチエイリアス円）
                let dx = fx - dot_cx;
                let dy = fy - dot_cy;
                let dist = (dx * dx + dy * dy).sqrt();
                if halo_r > 0.0 {
                    let h_cov = (halo_r + 0.5 - dist).clamp(0.0, 1.0);
                    if h_cov > 0.0 {
                        blend_over(
                            &mut buf[idx..idx + 4],
                            halo_rgb.0,
                            halo_rgb.1,
                            halo_rgb.2,
                            h_cov * 0.34,
                        );
                    }
                }
                // 左の丸本体
                let d_cov = (dot_r + 0.5 - dist).clamp(0.0, 1.0);
                if d_cov > 0.0 {
                    blend_over(
                        &mut buf[idx..idx + 4],
                        dot_rgb.0,
                        dot_rgb.1,
                        dot_rgb.2,
                        d_cov,
                    );
                }
            }
        }

        // 2. 文字を黒背景の別 DIB に ANTIALIASED_QUALITY で白描画し、輝度をアルファとして合成
        let text_dc = CreateCompatibleDC(screen_dc);
        let mut tbits: *mut std::ffi::c_void = ptr::null_mut();
        let thbm = CreateDIBSection(
            text_dc,
            &bmi,
            DIB_RGB_COLORS,
            &mut tbits,
            ptr::null_mut(),
            0,
        );
        if !thbm.is_null() && !tbits.is_null() {
            let old_tbm = SelectObject(text_dc, thbm);
            let tbuf = std::slice::from_raw_parts_mut(tbits as *mut u8, (w * h * 4) as usize);
            tbuf.fill(0);

            let font_name = wide("Yu Gothic UI");
            let font = CreateFontW(
                -(15.0 * s).round() as i32,
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
                ANTIALIASED_QUALITY as u32,
                (DEFAULT_PITCH | FF_DONTCARE) as u32,
                font_name.as_ptr(),
            );
            let old_font = SelectObject(text_dc, font);
            SetBkMode(text_dc, TRANSPARENT as i32);
            SetBkColor(text_dc, 0);
            SetTextColor(text_dc, 0x00FF_FFFF);
            let mut text_u16 = wide(&label);
            let left_px = (44.0 * s).round() as i32;
            let right_pad = (16.0 * s).round() as i32;
            let mut text_rc = RECT {
                left: left_px,
                top: 0,
                right: w - right_pad,
                bottom: h,
            };
            DrawTextW(
                text_dc,
                text_u16.as_mut_ptr(),
                (text_u16.len() - 1) as i32,
                &mut text_rc,
                DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
            );
            SelectObject(text_dc, old_font);
            DeleteObject(font);

            let tr = 232.0 / 255.0;
            let tg = 237.0 / 255.0;
            let tb = 247.0 / 255.0;
            let x_start = (left_px - 2).max(0);
            let x_end = (w - right_pad + 4).min(w);
            for py in 0..h {
                for px in x_start..x_end {
                    let idx = ((py * w + px) * 4) as usize;
                    let lum = tbuf[idx + 1] as f32 / 255.0;
                    if lum > 0.01 {
                        blend_over(&mut buf[idx..idx + 4], tr, tg, tb, lum);
                    }
                }
            }

            SelectObject(text_dc, old_tbm);
            DeleteObject(thbm);
        } else if !thbm.is_null() {
            DeleteObject(thbm);
        }
        DeleteDC(text_dc);

        // 3. UpdateLayeredWindow で乗算済みアルファのまま反映
        let sz = SIZE { cx: w, cy: h };
        let pt_src = POINT { x: 0, y: 0 };
        let bf = BLENDFUNCTION {
            BlendOp: AC_SRC_OVER as u8,
            BlendFlags: 0,
            SourceConstantAlpha: 255,
            AlphaFormat: AC_SRC_ALPHA as u8,
        };
        UpdateLayeredWindow(
            hwnd,
            screen_dc,
            ptr::null(),
            &sz,
            mem_dc,
            &pt_src,
            0,
            &bf,
            ULW_ALPHA,
        );

        SelectObject(mem_dc, old_bm);
        DeleteObject(hbm);
        DeleteDC(mem_dc);
        ReleaseDC(ptr::null_mut(), screen_dc);
    }

    unsafe extern "system" fn wnd_proc(
        hwnd: HWND,
        msg: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        match msg {
            WM_MOUSEACTIVATE => return MA_NOACTIVATE,
            WM_LBUTTONDOWN => {
                ReleaseCapture();
                SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION as WPARAM, 0);
                return 0;
            }
            WM_EXITSIZEMOVE => {
                let mut rc: RECT = std::mem::zeroed();
                if GetWindowRect(hwnd, &mut rc) != 0 {
                    if let Ok(mut p) = pinned_lock().lock() {
                        *p = Some((rc.left, rc.top));
                    }
                }
                return 0;
            }
            WM_LBUTTONDBLCLK => {
                if let Ok(mut p) = pinned_lock().lock() {
                    *p = None;
                }
                return 0;
            }
            WM_OVERLAY_MOVE => {
                let (x, y) = *target_pos_lock().lock().unwrap_or_else(|e| e.into_inner());
                SetWindowPos(hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE);
                return 0;
            }
            WM_OVERLAY_UPDATE => {
                let st = current_lock()
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .clone();
                match st {
                    OverlayState::Hidden => {
                        ShowWindow(hwnd, SW_HIDE);
                    }
                    _ => {
                        render_layered(hwnd, &st);
                        ShowWindow(hwnd, SW_SHOWNOACTIVATE);
                    }
                }
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
                SetProcessDPIAware();
                let hinst = GetModuleHandleW(ptr::null());
                let cls_name = wide("OnseiManOverlayClass");
                let wc = WNDCLASSW {
                    style: 0x0008, // CS_DBLCLKS
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

                let (pw, ph, s) = pill_size();
                let sx = GetSystemMetrics(SM_CXSCREEN);
                let sy = GetSystemMetrics(SM_CYSCREEN);
                let x = (sx - pw) / 2;
                let y = sy - ph - (64.0 * s).round() as i32;

                let title = wide("OnseiManOverlay");
                let hwnd = CreateWindowExW(
                    WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED,
                    cls_name.as_ptr(),
                    title.as_ptr(),
                    WS_POPUP,
                    x,
                    y,
                    pw,
                    ph,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    hinst,
                    ptr::null(),
                );
                if hwnd.is_null() {
                    return;
                }
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

        pub fn position_for(&self, place: &str) {
            unsafe {
                let (pw, ph, s) = pill_size();
                let sx = GetSystemMetrics(SM_CXSCREEN).max(pw + 16);
                let sy = GetSystemMetrics(SM_CYSCREEN).max(ph + 64);
                let margin = (8.0 * s).round() as i32;
                let bottom_pad = (64.0 * s).round() as i32;
                let gap = (12.0 * s).round() as i32;
                let (x, y) = if let Some((px, py)) = pinned_lock().lock().ok().and_then(|g| *g) {
                    (
                        px.clamp(margin, sx - pw - margin),
                        py.clamp(margin, sy - ph - margin),
                    )
                } else if place == "caret" {
                    if let Some((l, t, _r, b)) = crate::caret::caret_rect() {
                        let cx = (l - gap).clamp(margin, sx - pw - margin);
                        let cy = if b + gap + ph < sy - (48.0 * s).round() as i32 {
                            b + gap
                        } else {
                            (t - ph - gap).max(margin)
                        };
                        (cx, cy)
                    } else {
                        ((sx - pw) / 2, sy - ph - bottom_pad)
                    }
                } else {
                    ((sx - pw) / 2, sy - ph - bottom_pad)
                };
                if let Ok(mut tp) = target_pos_lock().lock() {
                    *tp = (x, y);
                }
                if let Ok(h) = self.hwnd.lock() {
                    if *h != 0 {
                        PostMessageW(*h as HWND, WM_OVERLAY_MOVE, 0, 0);
                    }
                }
            }
        }

        pub fn set_state(&self, state: OverlayState) {
            if let Ok(mut g) = current_lock().lock() {
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
        pub fn position_for(&self, _place: &str) {}
        pub fn set_state(&self, _state: OverlayState) {}
    }
}

pub use imp::RecordingOverlay;
