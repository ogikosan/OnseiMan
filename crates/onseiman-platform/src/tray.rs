//! システムトレイアイコン（Windows `Shell_NotifyIconW` / macOS スタブ）。

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TrayEvent {
    OpenUi,
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
    use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows_sys::Win32::UI::Shell::{
        Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NOTIFYICONDATAW,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreatePopupMenu, CreateWindowExW, DefWindowProcW, DestroyMenu, DestroyWindow,
        DispatchMessageW, GetCursorPos, GetMessageW, LoadIconW, PostMessageW, PostQuitMessage,
        RegisterClassW, SetForegroundWindow, TrackPopupMenu, TranslateMessage, IDI_APPLICATION,
        MF_SEPARATOR, MF_STRING, MSG, TPM_BOTTOMALIGN, TPM_LEFTALIGN, WM_APP, WM_CLOSE, WM_COMMAND,
        WM_DESTROY, WM_LBUTTONDBLCLK, WM_LBUTTONUP, WM_RBUTTONUP, WNDCLASSW,
    };

    const WM_TRAY_ICON: u32 = WM_APP + 20;
    const CMD_OPEN_UI: usize = 1001;
    const CMD_QUIT: usize = 1002;

    static TRAY_TX: OnceLock<Mutex<Option<Sender<TrayEvent>>>> = OnceLock::new();

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
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
                        let open_txt = wide("管理画面を開く");
                        let quit_txt = wide("終了");
                        AppendMenuW(menu, MF_STRING, CMD_OPEN_UI, open_txt.as_ptr());
                        AppendMenuW(menu, MF_SEPARATOR, 0, ptr::null());
                        AppendMenuW(menu, MF_STRING, CMD_QUIT, quit_txt.as_ptr());
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
                let id = wparam & 0xFFFF;
                if id == CMD_OPEN_UI {
                    emit(TrayEvent::OpenUi);
                } else if id == CMD_QUIT {
                    emit(TrayEvent::Quit);
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
    }

    impl TrayIcon {
        pub fn start(tooltip: &str, tx: Sender<TrayEvent>) -> Self {
            let lock = TRAY_TX.get_or_init(|| Mutex::new(None));
            if let Ok(mut g) = lock.lock() {
                *g = Some(tx);
            }
            let tip_owned = tooltip.to_string();
            let hwnd_cell = Arc::new(Mutex::new(0isize));
            let hwnd_bg = Arc::clone(&hwnd_cell);

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
                    return;
                }
                if let Ok(mut g) = hwnd_bg.lock() {
                    *g = hwnd as isize;
                }

                let mut nid: NOTIFYICONDATAW = std::mem::zeroed();
                nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
                nid.hWnd = hwnd;
                nid.uID = 1;
                nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
                nid.uCallbackMessage = WM_TRAY_ICON;
                nid.hIcon = LoadIconW(ptr::null_mut(), IDI_APPLICATION);
                let tip_u16: Vec<u16> = tip_owned.encode_utf16().take(127).collect();
                for (i, ch) in tip_u16.into_iter().enumerate() {
                    nid.szTip[i] = ch;
                }
                Shell_NotifyIconW(NIM_ADD, &nid);

                let mut msg: MSG = std::mem::zeroed();
                while GetMessageW(&mut msg, ptr::null_mut(), 0, 0) > 0 {
                    TranslateMessage(&msg);
                    DispatchMessageW(&msg);
                }
                Shell_NotifyIconW(NIM_DELETE, &nid);
            });

            Self {
                hwnd: hwnd_cell,
                join: Some(join),
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
    }
}

pub use imp::TrayIcon;
