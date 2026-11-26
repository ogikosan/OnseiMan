//! API キーの保護保存。
//! - Windows: DPAPI（`dpapi:<base64>`。Python 版 `client/secret_store.py` と完全互換）
//! - macOS: Keychain（`keychain:<account>`。`/usr/bin/security` を使用）

pub const DPAPI_PREFIX: &str = "dpapi:";
pub const KEYCHAIN_PREFIX: &str = "keychain:";

#[cfg(windows)]
pub fn protect(value: &str) -> Result<String, String> {
    use base64::Engine as _;
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CryptProtectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    if value.is_empty() {
        return Ok(String::new());
    }
    let bytes = value.as_bytes();
    let input = CRYPT_INTEGER_BLOB {
        cbData: bytes.len() as u32,
        pbData: bytes.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };
    // SAFETY: input は有効なスライス、output は呼び出し後に LocalFree で解放する。
    let ok = unsafe {
        CryptProtectData(
            &input,
            ptr::null(),
            ptr::null(),
            ptr::null_mut(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if ok == 0 || output.pbData.is_null() {
        return Err("API キーの鍵をかけることができませんでした".into());
    }
    let slice = unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) };
    let encoded = base64::engine::general_purpose::STANDARD.encode(slice);
    unsafe {
        LocalFree(output.pbData as _);
    }
    Ok(format!("{DPAPI_PREFIX}{encoded}"))
}

#[cfg(windows)]
pub fn unprotect(stored: &str) -> String {
    use base64::Engine as _;
    use std::ptr;
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Cryptography::{
        CryptUnprotectData, CRYPTPROTECT_UI_FORBIDDEN, CRYPT_INTEGER_BLOB,
    };

    let Some(b64) = stored.strip_prefix(DPAPI_PREFIX) else {
        return String::new();
    };
    let Ok(bytes) = base64::engine::general_purpose::STANDARD.decode(b64) else {
        return String::new();
    };
    let input = CRYPT_INTEGER_BLOB {
        cbData: bytes.len() as u32,
        pbData: bytes.as_ptr() as *mut u8,
    };
    let mut output = CRYPT_INTEGER_BLOB {
        cbData: 0,
        pbData: ptr::null_mut(),
    };
    // SAFETY: input は有効なバッファ、output は LocalFree で解放する。
    let ok = unsafe {
        CryptUnprotectData(
            &input,
            ptr::null_mut(),
            ptr::null(),
            ptr::null_mut(),
            ptr::null(),
            CRYPTPROTECT_UI_FORBIDDEN,
            &mut output,
        )
    };
    if ok == 0 || output.pbData.is_null() {
        return String::new();
    }
    let slice = unsafe { std::slice::from_raw_parts(output.pbData, output.cbData as usize) };
    let text = String::from_utf8(slice.to_vec()).unwrap_or_default();
    unsafe {
        LocalFree(output.pbData as _);
    }
    text
}

#[cfg(target_os = "macos")]
pub fn protect_named(account: &str, value: &str) -> Result<String, String> {
    use std::process::Command;

    if value.is_empty() {
        return Ok(String::new());
    }
    let st = Command::new("/usr/bin/security")
        .args([
            "add-generic-password",
            "-U",
            "-s",
            "OnseiMan",
            "-a",
            account,
            "-w",
            value,
        ])
        .status()
        .map_err(|e| format!("Keychain への保存に失敗しました: {e}"))?;
    if !st.success() {
        return Err("Keychain への保存に失敗しました".into());
    }
    Ok(format!("{KEYCHAIN_PREFIX}{account}"))
}

#[cfg(target_os = "macos")]
pub fn protect(value: &str) -> Result<String, String> {
    protect_named("default", value)
}

#[cfg(target_os = "macos")]
pub fn unprotect(stored: &str) -> String {
    use std::process::Command;

    let Some(account) = stored.strip_prefix(KEYCHAIN_PREFIX) else {
        return String::new();
    };
    let Ok(out) = Command::new("/usr/bin/security")
        .args([
            "find-generic-password",
            "-s",
            "OnseiMan",
            "-a",
            account,
            "-w",
        ])
        .output()
    else {
        return String::new();
    };
    if !out.status.success() {
        return String::new();
    }
    String::from_utf8_lossy(&out.stdout)
        .trim_end_matches(['\r', '\n'])
        .to_string()
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn protect(_value: &str) -> Result<String, String> {
    Err("この OS では API キーの暗号化保存に未対応です".into())
}

#[cfg(not(any(windows, target_os = "macos")))]
pub fn unprotect(_stored: &str) -> String {
    String::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[cfg(windows)]
    fn dpapi_roundtrip() {
        let enc = protect("sk-test-secret-123").unwrap();
        assert!(enc.starts_with(DPAPI_PREFIX));
        assert!(!enc.contains("sk-test-secret-123"));
        assert_eq!(unprotect(&enc), "sk-test-secret-123");
        assert_eq!(unprotect("plain"), "");
    }
}
