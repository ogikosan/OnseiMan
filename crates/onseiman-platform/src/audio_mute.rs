//! 録音中に他アプリの再生音を一時ミュートし、終了時に必ず元へ戻す RAII ガード。
//!
//! - Windows: WASAPI (`IMMDeviceEnumerator` → `IAudioSessionManager2` → `ISimpleAudioVolume`) で
//!   自プロセス以外のアクティブな再生セッションを一時ミュートし、`Drop` 時に復元する。
//! - macOS: `osascript` でシステム出力のミュート状態を取得・一時ミュートし、`Drop` 時に復元する。

#[cfg(windows)]
mod imp {
    use windows::core::{Interface, GUID};
    use windows::Win32::Media::Audio::{
        eMultimedia, eRender, IAudioSessionControl2, IAudioSessionManager2, IMMDeviceEnumerator,
        ISimpleAudioVolume, MMDeviceEnumerator,
    };
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_ALL, COINIT_MULTITHREADED,
    };

    pub struct AudioMuteGuard {
        muted_sessions: Vec<ISimpleAudioVolume>,
        com_inited: bool,
    }

    impl AudioMuteGuard {
        pub fn mute_others() -> Self {
            unsafe {
                let com_inited = CoInitializeEx(None, COINIT_MULTITHREADED).is_ok();
                let mut muted_sessions = Vec::new();
                let self_pid = std::process::id();

                let Ok(enumerator): Result<IMMDeviceEnumerator, _> =
                    CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                else {
                    return Self {
                        muted_sessions,
                        com_inited,
                    };
                };
                let Ok(device) = enumerator.GetDefaultAudioEndpoint(eRender, eMultimedia) else {
                    return Self {
                        muted_sessions,
                        com_inited,
                    };
                };
                let Ok(mgr) = device.Activate::<IAudioSessionManager2>(CLSCTX_ALL, None) else {
                    return Self {
                        muted_sessions,
                        com_inited,
                    };
                };
                let Ok(session_enum) = mgr.GetSessionEnumerator() else {
                    return Self {
                        muted_sessions,
                        com_inited,
                    };
                };
                let count = session_enum.GetCount().unwrap_or(0);
                for i in 0..count {
                    let Ok(ctrl) = session_enum.GetSession(i) else {
                        continue;
                    };
                    let Ok(ctrl2) = ctrl.cast::<IAudioSessionControl2>() else {
                        continue;
                    };
                    let pid = ctrl2.GetProcessId().unwrap_or(0);
                    if pid == 0 || pid == self_pid {
                        continue;
                    }
                    let Ok(vol) = ctrl.cast::<ISimpleAudioVolume>() else {
                        continue;
                    };
                    let already_muted = vol.GetMute().map(|b| b.as_bool()).unwrap_or(true);
                    if !already_muted && vol.SetMute(true, &GUID::zeroed()).is_ok() {
                        muted_sessions.push(vol);
                    }
                }
                Self {
                    muted_sessions,
                    com_inited,
                }
            }
        }
    }

    impl Drop for AudioMuteGuard {
        fn drop(&mut self) {
            unsafe {
                for vol in self.muted_sessions.drain(..) {
                    let _ = vol.SetMute(false, &GUID::zeroed());
                }
                if self.com_inited {
                    CoUninitialize();
                }
            }
        }
    }
}

#[cfg(target_os = "macos")]
mod imp {
    use std::process::Command;

    pub struct AudioMuteGuard {
        was_muted: bool,
        changed: bool,
    }

    impl AudioMuteGuard {
        pub fn mute_others() -> Self {
            let out = Command::new("/usr/bin/osascript")
                .args(["-e", "output muted of (get volume settings)"])
                .output();
            let was_muted = out
                .ok()
                .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "true")
                .unwrap_or(true);
            let mut changed = false;
            if !was_muted {
                if let Ok(st) = Command::new("/usr/bin/osascript")
                    .args(["-e", "set volume with output muted"])
                    .status()
                {
                    changed = st.success();
                }
            }
            Self { was_muted, changed }
        }
    }

    impl Drop for AudioMuteGuard {
        fn drop(&mut self) {
            if self.changed && !self.was_muted {
                let _ = Command::new("/usr/bin/osascript")
                    .args(["-e", "set volume without output muted"])
                    .status();
            }
        }
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod imp {
    pub struct AudioMuteGuard;
    impl AudioMuteGuard {
        pub fn mute_others() -> Self {
            Self
        }
    }
}

pub use imp::AudioMuteGuard;
