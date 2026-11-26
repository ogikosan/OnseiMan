//! speech.cpp（MIT）と Haas-ja-v1.0（Apache-2.0）の GGUF を、OnseiMan のフォルダの中に用意する。
//!
//! - speech.cpp: Windows は Vulkan 版（CPU でも動く）、Mac は Metal 版。置き場所は `tools/speech/`。
//! - Haas-ja-v1.0 の Q8_0（0.66GB）: `models/haas-ja/`。
//! - どちらも決まった版に固定し、ダウンロードしたあと確認用の値（SHA-256）を確かめる。
//! - パソコン全体には何も入れない（PATH も変えない）。

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

use sha2::{Digest, Sha256};

pub const VERSION: &str = "0.11.2";
/// 配布元（GitHub の Releases の SHA256SUMS）が示す確認用の値
const TOOL_WINDOWS: (&str, &str) = (
    "speech-0.11.2-windows-x64-vulkan.zip",
    "2cf46f0351a2a90b9ce0bfb83cf63ac209ca31c88df9a5bc0126898a20a58586",
);
const TOOL_MAC: (&str, &str) = (
    "speech-0.11.2-macos-arm64-metal.zip",
    "1984a2742131bfa0048b932853c94859618bdc383e5dbbea1a1006fd8baece23",
);
pub const MODEL_FILE: &str = "Haas-616M-ja-v1.0-Q8_0.gguf";
const MODEL_REVISION: &str = "6b965d5007ba780585de51549d57a8c1c0d67c7e";
/// Hugging Face が示す確認用の値（LFS の oid）
const MODEL_SHA256: &str = "cd5f60b2f1c411404b83fca4a65e3ce30a26d9e951b6a346529e65f22758afeb";
pub const MODEL_BYTES: u64 = 656_381_472;

/// (何をしているか, 済んだバイト, 全部のバイト)
pub type Progress<'a> = &'a mut dyn FnMut(&str, u64, u64);

/// この機械で使う speech.cpp の (zip の名前, 確認用の値)。使えない機械なら None。
pub fn tool_for_this_machine() -> Option<(&'static str, &'static str)> {
    if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some(TOOL_WINDOWS)
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        Some(TOOL_MAC)
    } else {
        None
    }
}

pub fn tool_dir(root: &Path) -> PathBuf {
    root.join("tools").join("speech")
}

pub fn model_path(root: &Path) -> PathBuf {
    root.join("models").join("haas-ja").join(MODEL_FILE)
}

fn walk(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        if p.is_dir() {
            walk(&p, out);
        } else {
            out.push(p);
        }
    }
}

pub fn find_exe(root: &Path) -> Option<PathBuf> {
    let name = if cfg!(windows) {
        "speech.exe"
    } else {
        "speech"
    };
    let mut files = Vec::new();
    walk(&tool_dir(root), &mut files);
    files.sort();
    files
        .into_iter()
        .find(|p| p.file_name().is_some_and(|n| n == name))
}

/// 部品が揃っているか（大きさだけ確かめる。中身はダウンロードのときに確かめてある）。
pub fn ready(root: &Path) -> bool {
    find_exe(root).is_some()
        && fs::metadata(model_path(root))
            .map(|m| m.is_file() && m.len() == MODEL_BYTES)
            .unwrap_or(false)
}

fn sha256_of(path: &Path) -> std::io::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

/// 取ってきて、確認の値が合えば dest に置く。途中で止まったものは、次は続きから取る。
fn download(
    url: &str,
    dest: &Path,
    sha256: &str,
    label: &str,
    progress: Progress,
) -> Result<(), String> {
    let io = |e: std::io::Error| format!("{label}: {e}");
    if let Some(parent) = dest.parent() {
        fs::create_dir_all(parent).map_err(io)?;
    }
    let part = dest.with_file_name(format!(
        "{}.part",
        dest.file_name().unwrap_or_default().to_string_lossy()
    ));
    let mut have = fs::metadata(&part).map(|m| m.len()).unwrap_or(0);
    let agent = ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(30))
        .timeout_read(Duration::from_secs(60))
        .user_agent("OnseiMan")
        .build();
    let mut req = agent.get(url);
    if have > 0 {
        req = req.set("Range", &format!("bytes={have}-"));
    }
    let resp = req
        .call()
        .map_err(|e| format!("{label}: ダウンロードできませんでした（{e}）"))?;
    if have > 0 && resp.status() != 206 {
        have = 0; // 続きから取れない場所だった。はじめから
    }
    let total = have
        + resp
            .header("Content-Length")
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(0);
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .append(have > 0)
        .truncate(have == 0)
        .open(&part)
        .map_err(io)?;
    let mut reader = resp.into_reader();
    let mut buf = vec![0u8; 1 << 20];
    let mut done = have;
    loop {
        let n = reader.read(&mut buf).map_err(io)?;
        if n == 0 {
            break;
        }
        file.write_all(&buf[..n]).map_err(io)?;
        done += n as u64;
        progress(label, done, total);
    }
    drop(file);
    if sha256_of(&part).map_err(io)? != sha256 {
        let _ = fs::remove_file(&part);
        return Err(format!(
            "{label}: ダウンロードしたものが正しいものと一致しませんでした"
        ));
    }
    fs::rename(&part, dest).map_err(io)
}

/// zip を dest の中に開く。dest の外へ出る名前が入っていたら、何も書かずにやめる。
pub fn extract_zip(zip_path: &Path, dest: &Path) -> Result<(), String> {
    let bad = || "zip の中身が正しくありません".to_string();
    let mut archive = zip::ZipArchive::new(File::open(zip_path).map_err(|e| e.to_string())?)
        .map_err(|_| bad())?;
    let mut targets = Vec::with_capacity(archive.len());
    for i in 0..archive.len() {
        let f = archive.by_index(i).map_err(|_| bad())?;
        targets.push((f.enclosed_name().ok_or_else(bad)?, f.is_dir()));
    }
    for (i, (rel, is_dir)) in targets.into_iter().enumerate() {
        let target = dest.join(rel);
        if is_dir {
            fs::create_dir_all(&target).map_err(|e| e.to_string())?;
            continue;
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent).map_err(|e| e.to_string())?;
        }
        let mut f = archive.by_index(i).map_err(|_| bad())?;
        let mut out = File::create(&target).map_err(|e| e.to_string())?;
        std::io::copy(&mut f, &mut out).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[cfg(unix)]
fn make_executable(dir: &Path) {
    use std::os::unix::fs::PermissionsExt;
    // zip から出したファイルは実行してよい印が消えるので付け直す
    let mut files = Vec::new();
    walk(dir, &mut files);
    for p in files {
        if let Ok(meta) = fs::metadata(&p) {
            let mut perm = meta.permissions();
            perm.set_mode(perm.mode() | 0o111);
            let _ = fs::set_permissions(&p, perm);
        }
    }
}

#[cfg(not(unix))]
fn make_executable(_dir: &Path) {}

/// 揃っていなければ取ってくる。この機械で使えないとき（Windows x64・Apple の Mac 以外）は Ok(false)。
pub fn ensure(root: &Path, progress: Progress) -> Result<bool, String> {
    let Some((name, sha)) = tool_for_this_machine() else {
        return Ok(false);
    };
    if find_exe(root).is_none() {
        let tmp = tempfile::Builder::new()
            .prefix("onseiman-speech-")
            .tempdir()
            .map_err(|e| e.to_string())?;
        let z = tmp.path().join(name);
        let url =
            format!("https://github.com/nyosegawa/speech.cpp/releases/download/v{VERSION}/{name}");
        download(&url, &z, sha, "聞き取りの道具（speech.cpp）", progress)?;
        let d = tool_dir(root);
        fs::create_dir_all(&d).map_err(|e| e.to_string())?;
        extract_zip(&z, &d)?;
        make_executable(&d);
        if find_exe(root).is_none() {
            return Err("speech.cpp の実行ファイルが見つかりません".into());
        }
    }
    let m = model_path(root);
    if !fs::metadata(&m)
        .map(|x| x.is_file() && x.len() == MODEL_BYTES)
        .unwrap_or(false)
    {
        let url = format!("https://huggingface.co/sakasegawa/Haas-ja-v1.0-GGUF/resolve/{MODEL_REVISION}/{MODEL_FILE}");
        download(
            &url,
            &m,
            MODEL_SHA256,
            "Haas-ja の聞き取りのデータ（約 0.66GB）",
            progress,
        )?;
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_zip(path: &Path, names: &[&str]) {
        let mut z = zip::ZipWriter::new(File::create(path).unwrap());
        for n in names {
            z.start_file(*n, zip::write::SimpleFileOptions::default())
                .unwrap();
            z.write_all(b"x").unwrap();
        }
        z.finish().unwrap();
    }

    #[test]
    fn zip_is_extracted_inside_dest() {
        let tmp = tempfile::tempdir().unwrap();
        let z = tmp.path().join("ok.zip");
        make_zip(&z, &["speech/speech.exe", "speech/lib/a.dll"]);
        let dest = tmp.path().join("tools").join("speech");
        fs::create_dir_all(&dest).unwrap();
        extract_zip(&z, &dest).unwrap();
        assert!(dest.join("speech").join("lib").join("a.dll").is_file());
    }

    #[test]
    fn zip_that_escapes_is_refused_and_writes_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        for bad in ["../evil.txt", "../speech_evil/pwn.txt", "ok/../../evil.txt"] {
            let z = tmp.path().join("bad.zip");
            make_zip(&z, &["ok.txt", bad]);
            let dest = tmp.path().join("tools").join("speech");
            fs::create_dir_all(&dest).unwrap();
            assert!(extract_zip(&z, &dest).is_err(), "{bad}");
            assert!(
                !dest.join("ok.txt").exists(),
                "{bad}: 先のファイルも書かない"
            );
            assert!(!tmp.path().join("tools").join("evil.txt").exists());
            assert!(!tmp.path().join("tools").join("speech_evil").exists());
        }
    }

    #[test]
    fn not_ready_when_nothing_is_there() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(!ready(tmp.path()));
        assert!(find_exe(tmp.path()).is_none());
    }
}
