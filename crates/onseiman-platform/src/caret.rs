//! いま文字を入力している場所（文字カーソル／フォーカス中の入力欄）の画面座標を取得する。
//!
//! 元の Python 版 `client/caret.py` と同様に以下を順に試す:
//! 1. `GetGUIThreadInfo` の文字カーソル（メモ帳など標準 Win32 入力欄）
//! 2. UI Automation のフォーカス要素（`IUIAutomation::GetFocusedElement`）の矩形

pub type Rect = (i32, i32, i32, i32);

#[cfg(windows)]
fn valid_rect(r: Rect) -> bool {
    let (l, t, ri, b) = r;
    (ri - l) < 4000 && (b - t) < 4000 && !(l == 0 && t == 0 && ri == 0 && b == 0)
}

#[cfg(windows)]
fn from_gui_thread() -> Option<Rect> {
    use windows_sys::Win32::Foundation::POINT;
    use windows_sys::Win32::Graphics::Gdi::ClientToScreen;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        GetForegroundWindow, GetGUIThreadInfo, GetWindowThreadProcessId, GUITHREADINFO,
    };

    unsafe {
        let fg = GetForegroundWindow();
        if fg.is_null() {
            return None;
        }
        let tid = GetWindowThreadProcessId(fg, std::ptr::null_mut());
        let mut info: GUITHREADINFO = std::mem::zeroed();
        info.cbSize = std::mem::size_of::<GUITHREADINFO>() as u32;
        if GetGUIThreadInfo(tid, &mut info) == 0 || info.hwndCaret.is_null() {
            return None;
        }
        let rc = info.rcCaret;
        if rc.right == rc.left && rc.bottom == rc.top {
            return None;
        }
        let mut pt1 = POINT {
            x: rc.left,
            y: rc.top,
        };
        let mut pt2 = POINT {
            x: rc.right,
            y: rc.bottom,
        };
        ClientToScreen(info.hwndCaret, &mut pt1);
        ClientToScreen(info.hwndCaret, &mut pt2);
        let r = (pt1.x, pt1.y, pt2.x.max(pt1.x + 1), pt2.y);
        valid_rect(r).then_some(r)
    }
}

#[cfg(windows)]
fn from_uia_focus() -> Option<Rect> {
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER,
        COINIT_MULTITHREADED,
    };
    use windows::Win32::UI::Accessibility::{CUIAutomation, IUIAutomation};

    unsafe {
        let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
        let should_uninit = hr.is_ok();
        let res = (|| -> Option<Rect> {
            let uia: IUIAutomation =
                CoCreateInstance(&CUIAutomation, None, CLSCTX_INPROC_SERVER).ok()?;
            let elem = uia.GetFocusedElement().ok()?;
            let br = elem.CurrentBoundingRectangle().ok()?;
            let w = br.right - br.left;
            let h = br.bottom - br.top;
            if w <= 0 || h <= 0 || w > 2400 || h > 1600 {
                return None;
            }
            let r = (br.left, br.top, br.right, br.bottom);
            valid_rect(r).then_some(r)
        })();
        if should_uninit {
            CoUninitialize();
        }
        res
    }
}

/// 文字カーソルまたはフォーカス中入力欄の画面座標 `(left, top, right, bottom)` を返す。
#[cfg(windows)]
pub fn caret_rect() -> Option<Rect> {
    from_gui_thread().or_else(from_uia_focus)
}

#[cfg(not(windows))]
pub fn caret_rect() -> Option<Rect> {
    None
}
