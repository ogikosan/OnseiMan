//! OS ログイン時の自動起動登録（Windows: HKCU\...\Run / macOS: LaunchAgents）。

use std::path::Path;

#[cfg(windows)]
mod imp {
    use super::*;
    use std::ptr;
    use windows_sys::Win32::System::Registry::{
        RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW, HKEY,
        HKEY_CURRENT_USER, KEY_READ, KEY_SET_VALUE, REG_SZ,
    };

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    const RUN_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";

    pub fn is_enabled(value_name: &str) -> bool {
        let sub = wide(RUN_KEY);
        let name = wide(value_name);
        let mut hkey: HKEY = ptr::null_mut();
        if unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, sub.as_ptr(), 0, KEY_READ, &mut hkey) } != 0 {
            return false;
        }
        let mut size: u32 = 0;
        let ok = unsafe {
            RegQueryValueExW(
                hkey,
                name.as_ptr(),
                ptr::null(),
                ptr::null_mut(),
                ptr::null_mut(),
                &mut size,
            )
        } == 0
            && size > 2;
        unsafe {
            RegCloseKey(hkey);
        }
        ok
    }

    pub fn set_enabled(enabled: bool, exe_path: &Path, value_name: &str) -> Result<(), String> {
        let sub = wide(RUN_KEY);
        let name = wide(value_name);
        let mut hkey: HKEY = ptr::null_mut();
        let rc =
            unsafe { RegOpenKeyExW(HKEY_CURRENT_USER, sub.as_ptr(), 0, KEY_SET_VALUE, &mut hkey) };
        if rc != 0 {
            return Err(format!("レジストリを開けませんでした (code={rc})"));
        }
        let res = if enabled {
            let cmd = format!("\"{}\"", exe_path.display());
            let data = wide(&cmd);
            let bytes = (data.len() * std::mem::size_of::<u16>()) as u32;
            let rc2 = unsafe {
                RegSetValueExW(
                    hkey,
                    name.as_ptr(),
                    0,
                    REG_SZ,
                    data.as_ptr() as *const u8,
                    bytes,
                )
            };
            if rc2 == 0 {
                Ok(())
            } else {
                Err(format!("自動起動の登録に失敗しました (code={rc2})"))
            }
        } else {
            unsafe {
                RegDeleteValueW(hkey, name.as_ptr());
            }
            Ok(())
        };
        unsafe {
            RegCloseKey(hkey);
        }
        res
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use super::*;
    use std::fs;
    use std::path::PathBuf;

    fn plist_path(value_name: &str) -> Option<PathBuf> {
        let home = std::env::var_os("HOME")?;
        Some(
            PathBuf::from(home)
                .join("Library")
                .join("LaunchAgents")
                .join(format!("com.onseiman.{}.plist", value_name.to_lowercase())),
        )
    }

    fn xml_esc(s: &str) -> String {
        s.replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;")
    }

    pub fn is_enabled(value_name: &str) -> bool {
        plist_path(value_name).is_some_and(|p| p.is_file())
    }

    pub fn set_enabled(enabled: bool, exe_path: &Path, value_name: &str) -> Result<(), String> {
        let path = plist_path(value_name).ok_or("HOME が見つかりません")?;
        if !enabled {
            let _ = fs::remove_file(path);
            return Ok(());
        }
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let label = format!("com.onseiman.{}", value_name.to_lowercase());
        let exe = xml_esc(&exe_path.display().to_string());
        let content = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
<plist version=\"1.0\">\n<dict>\n\
  <key>Label</key><string>{label}</string>\n\
  <key>ProgramArguments</key><array><string>{exe}</string></array>\n\
  <key>RunAtLoad</key><true/>\n\
</dict>\n</plist>\n"
        );
        fs::write(path, content).map_err(|e| e.to_string())
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod imp {
    use super::*;
    pub fn is_enabled(_value_name: &str) -> bool {
        false
    }
    pub fn set_enabled(_enabled: bool, _exe_path: &Path, _value_name: &str) -> Result<(), String> {
        Err("この OS では自動起動登録に未対応です".into())
    }
}

pub use imp::{is_enabled, set_enabled};
