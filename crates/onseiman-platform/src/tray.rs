//! システムトレイアイコン（Windows `Shell_NotifyIconW` / macOS スタブ）。
//!
//! 元の Python 版（`_make_icon_image`）と同じ「青い丸＋白いマイク」アイコンを
//! 純 Rust の 32×32 RGBA ビットマップから `CreateIconIndirect` で生成する。

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayEvent {
    OpenUi,
    OpenDict,
    OpenSettings,
    OpenHelp,
    CopyLast,
    RetranscribeLast,
    CaptureHotkey,
    CaptureAiHotkey,
    OpenLog,
    OpenHistoryTxt,
    CheckUpdate,
    Quit,
}

#[cfg(windows)]
mod imp {
    use super::TrayEvent;
    use std::ptr;
    use std::sync::mpsc::Sender;
    use std::sync::{Arc, Mutex, OnceLock};
    use std::thread;
    use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, POINT, WPARAM};
    use windows_sys::Win32::Graphics::Gdi::{
        CreateBitmap, CreateDIBSection, DeleteObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB,
        DIB_RGB_COLORS,
    };
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::Shell::{
        Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NIM_MODIFY,
        NOTIFYICONDATAW,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreateIconIndirect, CreatePopupMenu, CreateWindowExW, DefWindowProcW,
        DestroyIcon, DestroyMenu, DestroyWindow, DispatchMessageW, GetCursorPos, GetMessageW,
        LoadIconW, PostMessageW, PostQuitMessage, RegisterClassW, SetForegroundWindow,
        SetMenuDefaultItem, TrackPopupMenu, TranslateMessage, HICON, ICONINFO, IDI_APPLICATION,
        MF_GRAYED, MF_SEPARATOR, MF_STRING, MSG, TPM_BOTTOMALIGN, TPM_LEFTALIGN, WM_APP, WM_CLOSE,
        WM_COMMAND, WM_DESTROY, WM_LBUTTONDBLCLK, WM_LBUTTONUP, WM_RBUTTONUP, WNDCLASSW,
    };

    const WM_TRAY_ICON: u32 = WM_APP + 20;
    const WM_TRAY_UPDATE: u32 = WM_APP + 21;
    const CMD_STATUS: usize = 1000;
    const CMD_OPEN_UI: usize = 1001;
    const CMD_QUIT: usize = 1002;
    const CMD_COPY_LAST: usize = 1003;
    const CMD_RETRANSCRIBE_LAST: usize = 1004;
    const CMD_CAPTURE_HOTKEY: usize = 1005;
    const CMD_CAPTURE_AI_HOTKEY: usize = 1006;
    const CMD_OPEN_DICT: usize = 1007;
    const CMD_OPEN_SETTINGS: usize = 1008;
    const CMD_OPEN_HELP: usize = 1009;
    const CMD_OPEN_LOG: usize = 1010;
    const CMD_OPEN_HISTORY_TXT: usize = 1011;
    const CMD_CHECK_UPDATE: usize = 1012;

    struct PendingIcon {
        rgb: (u8, u8, u8),
        tooltip: String,
    }

    static TRAY_TX: OnceLock<Mutex<Option<Sender<TrayEvent>>>> = OnceLock::new();
    static PENDING_ICON: OnceLock<Mutex<Option<PendingIcon>>> = OnceLock::new();
    static CUR_ICON: OnceLock<Mutex<(isize, bool)>> = OnceLock::new();
    static STATUS_TEXT: OnceLock<Mutex<String>> = OnceLock::new();

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// 元の `_make_icon_image`（64×64 座標系）と同じ丸＋白いマイクを 32×32 BGRA で描く。
    /// 戻り値の `bool` が `true` のときのみ `DestroyIcon` で解放すべき自作アイコン。
    pub(super) unsafe fn create_mic_icon(r_bg: u8, g_bg: u8, b_bg: u8) -> (HICON, bool) {
        const SIZE: i32 = 32;
        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: SIZE,
                biHeight: -SIZE, // top-down
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
        let mut bits: *mut std::ffi::c_void = ptr::null_mut();
        let hbm_color = CreateDIBSection(
            ptr::null_mut(),
            &bmi,
            DIB_RGB_COLORS,
            &mut bits,
            ptr::null_mut(),
            0,
        );
        if hbm_color.is_null() || bits.is_null() {
            if !hbm_color.is_null() {
                DeleteObject(hbm_color);
            }
            return (LoadIconW(ptr::null_mut(), IDI_APPLICATION), false);
        }

        let buf = std::slice::from_raw_parts_mut(bits as *mut u8, (SIZE * SIZE * 4) as usize);
        for py in 0..SIZE {
            for px in 0..SIZE {
                // 64×64 座標系へ変換（ピクセル中心）
                let x = (px as f32 + 0.5) * 2.0;
                let y = (py as f32 + 0.5) * 2.0;

                // 外側の円: 中心 (32, 32), 半径 28
                let dx = x - 32.0;
                let dy = y - 32.0;
                let dist = (dx * dx + dy * dy).sqrt();
                let circle_a = (28.5 - dist).clamp(0.0, 1.0);
                if circle_a <= 0.0 {
                    continue;
                }

                // 白いマイク本体: カプセル (24..40, 15..38) 半径 8
                let in_capsule = {
                    let cy = y.clamp(23.0, 30.0);
                    let d = ((x - 32.0).powi(2) + (y - cy).powi(2)).sqrt();
                    d <= 8.0
                };
                // U 字アーク: 中心 (32, 35), 半径 13..15, y >= 34
                let in_arc = {
                    let d = ((x - 32.0).powi(2) + (y - 34.0).powi(2)).sqrt();
                    y >= 33.0 && (11.2..=15.0).contains(&d)
                };
                // 軸: (30..34, 46..53)
                let in_stem = (30.0..=34.0).contains(&x) && (46.0..=53.5).contains(&y);
                // 台座: (23..41, 51..55)
                let in_base = (23.0..=41.0).contains(&x) && (51.0..=55.0).contains(&y);

                let is_white = in_capsule || in_arc || in_stem || in_base;
                let (r, g, b) = if is_white {
                    (255u8, 255u8, 255u8)
                } else {
                    (r_bg, g_bg, b_bg)
                };
                let a = (circle_a * 255.0).round() as u8;
                let idx = ((py * SIZE + px) * 4) as usize;
                // BGRA
                buf[idx] = b;
                buf[idx + 1] = g;
                buf[idx + 2] = r;
                buf[idx + 3] = a;
            }
        }

        let hbm_mask = CreateBitmap(SIZE, SIZE, 1, 1, ptr::null());
        if hbm_mask.is_null() {
            DeleteObject(hbm_color);
            return (LoadIconW(ptr::null_mut(), IDI_APPLICATION), false);
        }
        let ii = ICONINFO {
            fIcon: 1,
            xHotspot: 0,
            yHotspot: 0,
            hbmMask: hbm_mask,
            hbmColor: hbm_color,
        };
        let icon = CreateIconIndirect(&ii);
        DeleteObject(hbm_color);
        DeleteObject(hbm_mask);
        if icon.is_null() {
            (LoadIconW(ptr::null_mut(), IDI_APPLICATION), false)
        } else {
            (icon, true)
        }
    }

    fn emit(ev: TrayEvent) {
        if let Some(lock) = TRAY_TX.get() {
            if let Ok(g) = lock.lock() {
                if let Some(tx) = g.as_ref() {
                    let _ = tx.send(ev);
                }
            }
        }
    }

    unsafe extern "system" fn wnd_proc(
        hwnd: HWND,
        msg: u32,
        wparam: WPARAM,
        lparam: LPARAM,
    ) -> LRESULT {
        match msg {
            WM_TRAY_ICON => {
                let ev = (lparam as u32) & 0xFFFF;
                if ev == WM_LBUTTONUP || ev == WM_LBUTTONDBLCLK {
                    emit(TrayEvent::OpenUi);
                } else if ev == WM_RBUTTONUP {
                    let menu = CreatePopupMenu();
                    if !menu.is_null() {
                        let status_str = STATUS_TEXT
                            .get()
                            .and_then(|m| m.lock().ok())
                            .map(|g| g.clone())
                            .filter(|s| !s.is_empty())
                            .unwrap_or_else(|| "OnseiMan".to_string());
                        let items: &[(u32, usize, Option<&str>)] = &[
                            (MF_STRING | MF_GRAYED, CMD_STATUS, Some(&status_str)),
                            (MF_SEPARATOR, 0, None),
                            (MF_STRING, CMD_OPEN_UI, Some("OnseiMan の画面を開く")),
                            (MF_STRING, CMD_COPY_LAST, Some("直前に入れた文をコピー")),
                            (
                                MF_STRING,
                                CMD_RETRANSCRIBE_LAST,
                                Some("直前の録音をもう一度文字にする"),
                            ),
                            (MF_SEPARATOR, 0, None),
                            (
                                MF_STRING,
                                CMD_CAPTURE_HOTKEY,
                                Some("録音のキーを変える（次に押したキーにする）"),
                            ),
                            (
                                MF_STRING,
                                CMD_CAPTURE_AI_HOTKEY,
                                Some("AI で整えるキーを変える（次に押したキーにする）"),
                            ),
                            (MF_STRING, CMD_OPEN_DICT, Some("辞書")),
                            (MF_STRING, CMD_OPEN_SETTINGS, Some("設定")),
                            (MF_STRING, CMD_OPEN_HELP, Some("使い方")),
                            (
                                MF_STRING,
                                CMD_OPEN_LOG,
                                Some("動きの記録を開く（困ったときに作った人へ）"),
                            ),
                            (
                                MF_STRING,
                                CMD_OPEN_HISTORY_TXT,
                                Some("入れた文の一覧を開く（テキスト）"),
                            ),
                            (MF_STRING, CMD_CHECK_UPDATE, Some("更新を確認")),
                            (MF_SEPARATOR, 0, None),
                            (MF_STRING, CMD_QUIT, Some("終了")),
                        ];
                        for &(flags, id, label) in items {
                            if let Some(txt) = label {
                                let w = wide(txt);
                                AppendMenuW(menu, flags, id, w.as_ptr());
                            } else {
                                AppendMenuW(menu, flags, id, ptr::null());
                            }
                        }
                        SetMenuDefaultItem(menu, CMD_OPEN_UI as u32, 0);
                        let mut pt = POINT { x: 0, y: 0 };
                        GetCursorPos(&mut pt);
                        SetForegroundWindow(hwnd);
                        TrackPopupMenu(
                            menu,
                            TPM_LEFTALIGN | TPM_BOTTOMALIGN,
                            pt.x,
                            pt.y,
                            0,
                            hwnd,
                            ptr::null(),
                        );
                        DestroyMenu(menu);
                    }
                }
                return 0;
            }
            WM_COMMAND => {
                let ev = match wparam & 0xFFFF {
                    CMD_OPEN_UI => Some(TrayEvent::OpenUi),
                    CMD_COPY_LAST => Some(TrayEvent::CopyLast),
                    CMD_RETRANSCRIBE_LAST => Some(TrayEvent::RetranscribeLast),
                    CMD_CAPTURE_HOTKEY => Some(TrayEvent::CaptureHotkey),
                    CMD_CAPTURE_AI_HOTKEY => Some(TrayEvent::CaptureAiHotkey),
                    CMD_OPEN_DICT => Some(TrayEvent::OpenDict),
                    CMD_OPEN_SETTINGS => Some(TrayEvent::OpenSettings),
                    CMD_OPEN_HELP => Some(TrayEvent::OpenHelp),
                    CMD_OPEN_LOG => Some(TrayEvent::OpenLog),
                    CMD_OPEN_HISTORY_TXT => Some(TrayEvent::OpenHistoryTxt),
                    CMD_CHECK_UPDATE => Some(TrayEvent::CheckUpdate),
                    CMD_QUIT => Some(TrayEvent::Quit),
                    _ => None,
                };
                if let Some(ev) = ev {
                    emit(ev);
                }
                return 0;
            }
            WM_TRAY_UPDATE => {
                if let Some(lock) = PENDING_ICON.get() {
                    if let Ok(mut g) = lock.lock() {
                        if let Some(p) = g.take() {
                            let (new_icon, new_owned) = create_mic_icon(p.rgb.0, p.rgb.1, p.rgb.2);
                            let mut nid: NOTIFYICONDATAW = std::mem::zeroed();
                            nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
                            nid.hWnd = hwnd;
                            nid.uID = 1;
                            nid.uFlags = NIF_ICON | NIF_TIP;
                            nid.hIcon = new_icon;
                            let tip_u16: Vec<u16> = p.tooltip.encode_utf16().take(127).collect();
                            for (i, ch) in tip_u16.into_iter().enumerate() {
                                nid.szTip[i] = ch;
                            }
                            Shell_NotifyIconW(NIM_MODIFY, &nid);
                            let cur = CUR_ICON.get_or_init(|| Mutex::new((0, false)));
                            if let Ok(mut cg) = cur.lock() {
                                let (old, old_owned) = *cg;
                                *cg = (new_icon as isize, new_owned);
                                if old_owned && old != 0 {
                                    DestroyIcon(old as HICON);
                                }
                            }
                        }
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

    pub struct TrayIcon {
        hwnd: Arc<Mutex<isize>>,
        join: Option<thread::JoinHandle<()>>,
        added: bool,
    }

    impl TrayIcon {
        pub fn start(tooltip: &str, tx: Sender<TrayEvent>) -> Self {
            if let Ok(mut s) = STATUS_TEXT.get_or_init(|| Mutex::new(String::new())).lock() {
                *s = tooltip.to_string();
            }
            let lock = TRAY_TX.get_or_init(|| Mutex::new(None));
            if let Ok(mut g) = lock.lock() {
                *g = Some(tx);
            }
            let tip_owned = tooltip.to_string();
            let hwnd_cell = Arc::new(Mutex::new(0isize));
            let hwnd_bg = Arc::clone(&hwnd_cell);
            let (ready_tx, ready_rx) = std::sync::mpsc::channel::<bool>();

            let join = thread::spawn(move || unsafe {
                let hinst = GetModuleHandleW(ptr::null());
                let cls = wide("OnseiManTrayClass");
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
                    lpszClassName: cls.as_ptr(),
                };
                RegisterClassW(&wc);
                let title = wide("OnseiManTray");
                let hwnd = CreateWindowExW(
                    0,
                    cls.as_ptr(),
                    title.as_ptr(),
                    0,
                    0,
                    0,
                    0,
                    0,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    hinst,
                    ptr::null(),
                );
                if hwnd.is_null() {
                    let _ = ready_tx.send(false);
                    return;
                }
                if let Ok(mut g) = hwnd_bg.lock() {
                    *g = hwnd as isize;
                }

                // 元の OnseiMan 待機アイコン（緑 #16a34a＋白マイク）
                let (custom_icon, custom_owned) = create_mic_icon(0x16, 0xa3, 0x4a);
                let mut nid: NOTIFYICONDATAW = std::mem::zeroed();
                nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
                nid.hWnd = hwnd;
                nid.uID = 1;
                nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
                nid.uCallbackMessage = WM_TRAY_ICON;
                nid.hIcon = custom_icon;
                let tip_u16: Vec<u16> = tip_owned.encode_utf16().take(127).collect();
                for (i, ch) in tip_u16.into_iter().enumerate() {
                    nid.szTip[i] = ch;
                }
                let ok = Shell_NotifyIconW(NIM_ADD, &nid) != 0;
                if let Ok(mut cg) = CUR_ICON.get_or_init(|| Mutex::new((0, false))).lock() {
                    *cg = (custom_icon as isize, custom_owned);
                }
                if !ok {
                    let err = windows_sys::Win32::Foundation::GetLastError();
                    eprintln!(
                        "Shell_NotifyIconW(NIM_ADD) failed: err=0x{err:08x} cbSize={} hwnd={:?} icon={:?}",
                        nid.cbSize, hwnd, custom_icon
                    );
                }
                let _ = ready_tx.send(ok);

                let mut msg: MSG = std::mem::zeroed();
                while GetMessageW(&mut msg, ptr::null_mut(), 0, 0) > 0 {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
                Shell_NotifyIconW(NIM_DELETE, &nid);
                if let Ok(mut cg) = CUR_ICON.get_or_init(|| Mutex::new((0, false))).lock() {
                    let (cur, owned) = *cg;
                    *cg = (0, false);
                    if owned && cur != 0 {
                        DestroyIcon(cur as HICON);
                    }
                }
            });

            let added = ready_rx
                .recv_timeout(std::time::Duration::from_secs(3))
                .unwrap_or(false);
            Self {
                hwnd: hwnd_cell,
                join: Some(join),
                added,
            }
        }

        pub fn is_added(&self) -> bool {
            self.added
        }

        /// トレイアイコンの色（RGB）とツールチップを動的に変更する。
        pub fn set_state(&self, rgb: (u8, u8, u8), tooltip: &str) {
            if let Ok(mut s) = STATUS_TEXT.get_or_init(|| Mutex::new(String::new())).lock() {
                *s = tooltip.to_string();
            }
            let lock = PENDING_ICON.get_or_init(|| Mutex::new(None));
            if let Ok(mut g) = lock.lock() {
                *g = Some(PendingIcon {
                    rgb,
                    tooltip: tooltip.to_string(),
                });
            }
            if let Ok(h) = self.hwnd.lock() {
                if *h != 0 {
                    unsafe {
                        PostMessageW(*h as HWND, WM_TRAY_UPDATE, 0, 0);
                    }
                }
            }
        }
    }

    impl Drop for TrayIcon {
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
            if let Some(lock) = TRAY_TX.get() {
                if let Ok(mut g) = lock.lock() {
                    *g = None;
                }
            }
        }
    }
}

#[cfg(not(windows))]
mod imp {
    use super::TrayEvent;
    use std::sync::mpsc::Sender;

    pub struct TrayIcon;
    impl TrayIcon {
        pub fn start(_tooltip: &str, _tx: Sender<TrayEvent>) -> Self {
            Self
        }
        pub fn is_added(&self) -> bool {
            true
        }
        pub fn set_state(&self, _rgb: (u8, u8, u8), _tooltip: &str) {}
    }
}

pub use imp::TrayIcon;

#[cfg(all(test, windows))]
mod tests {
    use super::imp::create_mic_icon;
    use windows_sys::Win32::UI::WindowsAndMessaging::DestroyIcon;

    #[test]
    fn mic_icon_creates_cleanly() {
        unsafe {
            let (h, owned) = create_mic_icon(0x25, 0x63, 0xeb);
            assert!(!h.is_null(), "mic icon must create cleanly");
            assert!(owned, "custom mic icon should be owned");
            DestroyIcon(h);
        }
    }
}
