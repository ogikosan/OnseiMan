//! 聞き取った文の英字の略語を大文字にする（Haas-ja は英字をすべて小文字で書く: cpu → CPU）。

/// 大文字で書く略語。優先語に入れた言葉は、その書き方が優先される
pub const ACRONYMS: &[&str] = &[
    "AI", "API", "CPU", "GPU", "NPU", "TPU", "LLM", "PC", "OS", "USB", "SSD", "HDD", "URL", "PDF",
    "CSV", "SNS", "SSH", "WSL", "CUDA", "VRAM", "RAM", "IT", "IP", "LAN", "VPN", "DNS", "TCP",
    "UDP", "HTTP", "HTTPS", "SSL", "TLS", "DM", "QR", "UI", "UX", "GUI", "CLI", "SDK", "IDE", "PR",
    "CM", "NG", "OK", "HP", "ID", "PIN", "FAQ", "HTML", "CSS", "JSON", "YAML", "TOML", "XML",
    "SQL", "DB", "ORM", "AWS", "GCP", "OSS", "MCP", "CI", "CD", "JWT", "SSO", "MFA", "RBAC",
    "CORS", "CSRF", "XSS", "DGX", "RTX", "GB", "MB", "TB", "KB", "FPS", "HDR", "OLED", "HDMI",
    "DVD", "BIOS", "UEFI", "SIMD", "WASM", "TV", "PS", "VR", "AR", "GPT", "RAG", "NLP", "ASR",
    "TTS", "VAD", "SSE", "KPI", "KGI", "OKR", "ROI", "SLA", "SLO", "NDA", "MVP", "LGTM", "WIP",
    "TBD", "ETA", "DX", "EC", "CRM", "ERP", "SCM", "PDCA", "B2B", "B2C",
];

/// 小文字で出た略語を大文字にする（llm → LLM）。前後が英字のとき（apple など）は触らない。
///
/// 英字が続くひとかたまりが、略語の小文字とぴったり同じときだけ直す。
pub fn fix_acronyms(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut run = String::new();
    let flush = |run: &mut String, out: &mut String| {
        if run.is_empty() {
            return;
        }
        if ACRONYMS.iter().any(|a| a.to_ascii_lowercase() == *run) {
            out.push_str(&run.to_ascii_uppercase());
        } else {
            out.push_str(run);
        }
        run.clear();
    };
    for c in text.chars() {
        if c.is_ascii_alphabetic() {
            run.push(c);
        } else {
            flush(&mut run, &mut out);
            out.push(c);
        }
    }
    flush(&mut run, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::fix_acronyms;

    #[test]
    fn lower_acronyms_become_upper() {
        assert_eq!(fix_acronyms("cpuとgpuの違い"), "CPUとGPUの違い");
        assert_eq!(fix_acronyms("llm を api で呼ぶ"), "LLM を API で呼ぶ");
        assert_eq!(fix_acronyms("rtx5090"), "RTX5090");
    }

    #[test]
    fn words_containing_acronyms_are_untouched() {
        assert_eq!(fix_acronyms("apple okay pcb"), "apple okay pcb");
        assert_eq!(fix_acronyms("Ok CPU Api"), "Ok CPU Api"); // 小文字でぴったり同じときだけ
        assert_eq!(fix_acronyms(""), "");
    }
}
