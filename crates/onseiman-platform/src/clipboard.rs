//! クリップボードの読み書きと全フォーマット（画像・ファイル含む）スナップショット復元。
//! Windows では `EnumClipboardFormats` + `OleDuplicateData` で画像やファイル形式も丸ごと退避・復元し、
//! さらに `CanIncludeInClipboardHistory = 0` / `CanUploadToCloudClipboard = 0` /
//! `ExcludeClipboardContentFromMonitorProcessing` を付けて `Win+V` 履歴を汚さない。

#[cfg(windows)]
mod imp {
    use std::ffi::c_void;
    use std::ptr;
    use std::thread;
    use std::time::Duration;
    use windows_sys::Win32::Foundation::GlobalFree;
    use windows_sys::Win32::System::DataExchange::{
        CloseClipboard, EmptyClipboard, EnumClipboardFormats, GetClipboardData,
        GetClipboardFormatNameW, GetClipboardSequenceNumber, IsClipboardFormatAvailable,
        OpenClipboard, RegisterClipboardFormatW, SetClipboardData,
    };
    use windows_sys::Win32::System::Memory::{
        GlobalAlloc, GlobalLock, GlobalSize, GlobalUnlock, GMEM_MOVEABLE,
    };

    const CF_UNICODETEXT: u32 = 13;
    const SNAPSHOT_MAX_BYTES: usize = 64 * 1024 * 1024;
    const SNAPSHOT_MAX_FORMATS: usize = 256;

    #[link(name = "ole32")]
    extern "system" {
        fn OleDuplicateData(source: *mut c_void, format: u16, flags: u32) -> *mut c_void;
    }

    #[link(name = "gdi32")]
    extern "system" {
        fn CopyEnhMetaFileW(metafile: *mut c_void, file: *const u16) -> *mut c_void;
        fn DeleteObject(object: *mut c_void) -> i32;
        fn DeleteMetaFile(metafile: *mut c_void) -> i32;
        fn DeleteEnhMetaFile(metafile: *mut c_void) -> i32;
    }

    #[repr(C)]
    #[derive(Clone, Copy)]
    struct MetafilePict {
        mm: i32,
        x_ext: i32,
        y_ext: i32,
        hmf: *mut c_void,
    }

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

    fn register_format(name: &str) -> u32 {
        let wide: Vec<u16> = name.encode_utf16().chain(std::iter::once(0)).collect();
        unsafe { RegisterClipboardFormatW(wide.as_ptr()) }
    }

    fn set_dword_format_locked(format_name: &str, value: u32) {
        let fmt = register_format(format_name);
        if fmt == 0 {
            return;
        }
        let bytes = value.to_le_bytes();
        unsafe {
            let hmem = GlobalAlloc(GMEM_MOVEABLE, bytes.len());
            if hmem.is_null() {
                return;
            }
            let dst = GlobalLock(hmem) as *mut u8;
            if dst.is_null() {
                GlobalFree(hmem);
                return;
            }
            ptr::copy_nonoverlapping(bytes.as_ptr(), dst, bytes.len());
            GlobalUnlock(hmem);
            if SetClipboardData(fmt, hmem).is_null() {
                GlobalFree(hmem);
            }
        }
    }

    /// Windows 10/11 のクリップボード履歴（Win+V）とクラウド同期から除外するフラグを付与する。
    fn mark_exclude_from_history_locked() {
        set_dword_format_locked("ExcludeClipboardContentFromMonitorProcessing", 1);
        set_dword_format_locked("CanIncludeInClipboardHistory", 0);
        set_dword_format_locked("CanUploadToCloudClipboard", 0);
    }

    fn write_unicode_locked(text: &str) -> bool {
        unsafe {
            let mut wide: Vec<u16> = text.encode_utf16().collect();
            wide.push(0);
            let bytes = wide.len() * std::mem::size_of::<u16>();
            let hmem = GlobalAlloc(GMEM_MOVEABLE, bytes);
            if hmem.is_null() {
                return false;
            }
            let dst = GlobalLock(hmem) as *mut u16;
            if dst.is_null() {
                GlobalFree(hmem);
                return false;
            }
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
                    let max_u16 = GlobalSize(h) / std::mem::size_of::<u16>();
                    let mut len = 0usize;
                    while len < max_u16 && *ptr.add(len) != 0 {
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

    /// ユーザーが明示的にコピー操作したとき用（通常のクリップボード書き込み）。
    pub fn set_text(text: &str) -> bool {
        if !open_retry() {
            return false;
        }
        let ok = unsafe {
            EmptyClipboard();
            write_unicode_locked(text)
        };
        unsafe {
            CloseClipboard();
        }
        ok
    }

    /// 音声入力の貼り付け専用: `Win+V` 履歴・クラウド同期から除外して書き込み、書き込み後のシーケンス番号を返す。
    pub fn set_text_for_paste(text: &str) -> Option<u32> {
        if !open_retry() {
            return None;
        }
        let ok = unsafe {
            if EmptyClipboard() == 0 {
                false
            } else {
                let wrote = write_unicode_locked(text);
                if wrote {
                    mark_exclude_from_history_locked();
                }
                wrote
            }
        };
        let seq = if ok {
            Some(unsafe { GetClipboardSequenceNumber() })
        } else {
            None
        };
        unsafe {
            CloseClipboard();
        }
        seq
    }

    #[derive(Clone, Copy)]
    enum FormatKind {
        Global,
        Bitmap,
        Palette,
        MetafilePict,
        EnhMetafile,
    }

    impl FormatKind {
        fn from_format(format: u32) -> Option<Self> {
            match format {
                2 | 0x82 => Some(Self::Bitmap),
                9 => Some(Self::Palette),
                3 | 0x83 => Some(Self::MetafilePict),
                14 | 0x8e => Some(Self::EnhMetafile),
                1 | 4..=8 | 10..=13 | 15..=17 | 0x81 | 0xc000..=0xffff => Some(Self::Global),
                _ => None,
            }
        }
    }

    struct OwnedFormat {
        format: u32,
        handle: *mut c_void,
        kind: FormatKind,
        nested_metafile: *mut c_void,
    }

    // SAFETY: Handles are unshared duplicates owned exclusively by this struct until transferred back to Windows.
    unsafe impl Send for OwnedFormat {}

    impl Drop for OwnedFormat {
        fn drop(&mut self) {
            if self.handle.is_null() {
                return;
            }
            unsafe {
                match self.kind {
                    FormatKind::Global => {
                        GlobalFree(self.handle);
                    }
                    FormatKind::Bitmap | FormatKind::Palette => {
                        DeleteObject(self.handle);
                    }
                    FormatKind::MetafilePict => {
                        if !self.nested_metafile.is_null() {
                            DeleteMetaFile(self.nested_metafile);
                        }
                        GlobalFree(self.handle);
                    }
                    FormatKind::EnhMetafile => {
                        DeleteEnhMetaFile(self.handle);
                    }
                }
            }
        }
    }

    fn is_transient_format(format: u32) -> bool {
        if format < 0xc000 {
            return false;
        }
        let mut buf = [0u16; 256];
        let len = unsafe { GetClipboardFormatNameW(format, buf.as_mut_ptr(), buf.len() as i32) };
        if len <= 0 {
            return false;
        }
        let name = String::from_utf16_lossy(&buf[..len as usize]);
        [
            "DataObject",
            "Ole Private Data",
            "Wine Marshalled DataObject",
        ]
        .iter()
        .any(|t| name.eq_ignore_ascii_case(t))
    }

    /// クリップボードの全フォーマット（画像・ファイル・テキスト等）のスナップショット。
    pub struct ClipboardSnapshot {
        formats: Vec<OwnedFormat>,
    }

    impl ClipboardSnapshot {
        pub fn capture() -> Option<Self> {
            if !open_retry() {
                return None;
            }
            let mut formats = Vec::new();
            let mut used_bytes = 0usize;
            let mut fmt = 0u32;
            let ok = unsafe {
                loop {
                    fmt = EnumClipboardFormats(fmt);
                    if fmt == 0 {
                        break true;
                    }
                    if is_transient_format(fmt) {
                        continue;
                    }
                    if formats.len() >= SNAPSHOT_MAX_FORMATS {
                        break false;
                    }
                    let Some(kind) = FormatKind::from_format(fmt) else {
                        continue;
                    };
                    let src = GetClipboardData(fmt);
                    if src.is_null() {
                        continue;
                    }
                    if matches!(kind, FormatKind::Global) {
                        let sz = GlobalSize(src);
                        if sz == 0 || used_bytes.saturating_add(sz) > SNAPSHOT_MAX_BYTES {
                            continue;
                        }
                        used_bytes += sz;
                    }
                    let dup = match kind {
                        FormatKind::EnhMetafile => CopyEnhMetaFileW(src, ptr::null()),
                        FormatKind::Bitmap => OleDuplicateData(src, 2, GMEM_MOVEABLE),
                        FormatKind::Palette => OleDuplicateData(src, 9, GMEM_MOVEABLE),
                        FormatKind::MetafilePict => OleDuplicateData(src, 3, GMEM_MOVEABLE),
                        FormatKind::Global => OleDuplicateData(src, fmt as u16, GMEM_MOVEABLE),
                    };
                    if dup.is_null() {
                        continue;
                    }
                    let mut nested = ptr::null_mut();
                    if matches!(kind, FormatKind::MetafilePict)
                        && GlobalSize(dup) >= std::mem::size_of::<MetafilePict>()
                    {
                        let p = GlobalLock(dup) as *const MetafilePict;
                        if !p.is_null() {
                            nested = (*p).hmf;
                            GlobalUnlock(dup);
                        }
                    }
                    formats.push(OwnedFormat {
                        format: fmt,
                        handle: dup,
                        kind,
                        nested_metafile: nested,
                    });
                }
            };
            unsafe {
                CloseClipboard();
            }
            if ok {
                Some(Self { formats })
            } else {
                None
            }
        }

        /// 元のクリップボードを復元する（元が空なら空にする）。
        /// 待機中に他アプリがクリップボードを書き換えていた場合は上書きしない。
        pub fn restore(mut self, expected_seq: Option<u32>) -> bool {
            if let Some(exp) = expected_seq {
                if unsafe { GetClipboardSequenceNumber() } != exp {
                    return false;
                }
            }
            if !open_retry() {
                return false;
            }
            let ok = unsafe {
                if let Some(exp) = expected_seq {
                    if GetClipboardSequenceNumber() != exp {
                        CloseClipboard();
                        return false;
                    }
                }
                if EmptyClipboard() == 0 {
                    false
                } else if self.formats.is_empty() {
                    // 元が空なら空のまま（認識文を残さない）
                    true
                } else {
                    let mut any_ok = false;
                    for item in &mut self.formats {
                        if !SetClipboardData(item.format, item.handle).is_null() {
                            item.handle = ptr::null_mut();
                            item.nested_metafile = ptr::null_mut();
                            any_ok = true;
                        }
                    }
                    if any_ok {
                        mark_exclude_from_history_locked();
                    }
                    any_ok
                }
            };
            unsafe {
                CloseClipboard();
            }
            ok
        }
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

    pub fn set_text_for_paste(text: &str) -> Option<u32> {
        set_text(text).then_some(0)
    }

    pub struct ClipboardSnapshot {
        prev: Option<String>,
    }

    impl ClipboardSnapshot {
        pub fn capture() -> Option<Self> {
            Some(Self { prev: get_text() })
        }

        pub fn restore(self, _expected_seq: Option<u32>) -> bool {
            set_text(self.prev.as_deref().unwrap_or(""))
        }
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
    pub fn set_text_for_paste(_text: &str) -> Option<u32> {
        None
    }
    pub struct ClipboardSnapshot;
    impl ClipboardSnapshot {
        pub fn capture() -> Option<Self> {
            None
        }
        pub fn restore(self, _expected_seq: Option<u32>) -> bool {
            false
        }
    }
}

pub use imp::{get_text, set_text, set_text_for_paste, ClipboardSnapshot};
