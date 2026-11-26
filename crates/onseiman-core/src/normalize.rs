//! 標準の変換（漢数字→数字、全角英数→半角、カタカナ略語→英字）。
//! Python 版 `shared/normalize.py` と同じ決まり。

use std::collections::HashSet;

use crate::kana::{is_katakana_word_char, replace_katakana_word};

pub const BUILTIN_TERMS: &[(&str, &str)] = &[
    // ハードウェア・OS・ネットワーク
    ("エーアイ", "AI"),
    ("ピーシー", "PC"),
    ("シーピーユー", "CPU"),
    ("ジーピーユー", "GPU"),
    ("エヌピーユー", "NPU"),
    ("ティーピーユー", "TPU"),
    ("アールティーエックス", "RTX"),
    ("ブイラム", "VRAM"),
    ("ラム", "RAM"),
    ("エスエスディー", "SSD"),
    ("エイチディーディー", "HDD"),
    ("ユーエスビー", "USB"),
    ("エイチディーエムアイ", "HDMI"),
    ("ブルートゥース", "Bluetooth"),
    ("ワイファイ", "Wi-Fi"),
    ("エルエーエヌ", "LAN"),
    ("ブイピーエヌ", "VPN"),
    ("ディーエヌエス", "DNS"),
    ("アイピー", "IP"),
    ("ティーシーピー", "TCP"),
    ("ユーディーピー", "UDP"),
    ("エスエスエイチ", "SSH"),
    ("エスエスエル", "SSL"),
    ("ティーエルエス", "TLS"),
    ("エイチティーティーピーエス", "HTTPS"),
    ("エイチティーティーピー", "HTTP"),
    ("ピーディーエフ", "PDF"),
    ("ユーアールエル", "URL"),
    ("エーピーアイ", "API"),
    ("エスキューエル", "SQL"),
    ("ユーアイ", "UI"),
    ("ユーエックス", "UX"),
    ("アイディー", "ID"),
    ("ディーエム", "DM"),
    ("エスエヌエス", "SNS"),
    ("エルエルエム", "LLM"),
    ("ブイアール", "VR"),
    ("オーケー", "OK"),
    ("エヌジー", "NG"),
    ("ディーブイディー", "DVD"),
    ("アイフォン", "iPhone"),
    ("アイフォーン", "iPhone"),
    ("アイパッド", "iPad"),
    ("ウィンドウズ", "Windows"),
    ("マックオーエス", "macOS"),
    ("アイオーエス", "iOS"),
    ("アンドロイド", "Android"),
    ("リナックス", "Linux"),
    ("ウブントゥ", "Ubuntu"),
    ("ダブリューエスエル", "WSL"),
    ("クーダ", "CUDA"),
    // 開発・言語・ツール
    ("ギットハブ", "GitHub"),
    ("ギットラボ", "GitLab"),
    ("ギット", "Git"),
    ("パイソン", "Python"),
    ("ジャバスクリプト", "JavaScript"),
    ("タイプスクリプト", "TypeScript"),
    ("ネクストジェイエス", "Next.js"),
    ("ノードジェイエス", "Node.js"),
    ("ビュージェイエス", "Vue.js"),
    ("リアクト", "React"),
    ("ドッカー", "Docker"),
    ("クーバネティス", "Kubernetes"),
    ("テラフォーム", "Terraform"),
    ("ビジュアルスタジオコード", "Visual Studio Code"),
    ("ブイエスコード", "VS Code"),
    ("エックスコード", "Xcode"),
    ("インテリジェイ", "IntelliJ"),
    ("エイチティーエムエル", "HTML"),
    ("シーエスエス", "CSS"),
    ("ジェイソン", "JSON"),
    ("ヤムル", "YAML"),
    ("トムル", "TOML"),
    ("エックスエムエル", "XML"),
    ("シーエスブイ", "CSV"),
    ("ポストグレス", "PostgreSQL"),
    ("マイエスキューエル", "MySQL"),
    ("エスキューライト", "SQLite"),
    ("レディス", "Redis"),
    ("モンゴディービー", "MongoDB"),
    ("スーパーベース", "Supabase"),
    ("ファイアベース", "Firebase"),
    ("シーエルアイ", "CLI"),
    ("ジーユーアイ", "GUI"),
    ("エスディーケー", "SDK"),
    ("アイディーイー", "IDE"),
    ("オーエスエス", "OSS"),
    ("シーアイシーディー", "CI/CD"),
    ("ジーアールピーシー", "gRPC"),
    ("グラフキューエル", "GraphQL"),
    ("ウェブソケット", "WebSocket"),
    ("ワズム", "Wasm"),
    ("オーオース", "OAuth"),
    ("ジェイダブリューティー", "JWT"),
    ("エスエスオー", "SSO"),
    ("エムエフエー", "MFA"),
    ("エムシーピー", "MCP"),
    ("エルジーティーエム", "LGTM"),
    // クラウド・アプリ・SNS
    ("エーダブリューエス", "AWS"),
    ("ジーシーピー", "GCP"),
    ("アジュール", "Azure"),
    ("クラウドフレア", "Cloudflare"),
    ("バーセル", "Vercel"),
    ("ネットリファイ", "Netlify"),
    ("ユーチューブ", "YouTube"),
    ("ディスコード", "Discord"),
    ("スラック", "Slack"),
    ("ノーション", "Notion"),
    ("グーグルドライブ", "Google Drive"),
    ("グーグルマップ", "Google Maps"),
    ("グーグル", "Google"),
    ("ジーメール", "Gmail"),
    ("アマゾン", "Amazon"),
    ("ティックトック", "TikTok"),
    ("インスタグラム", "Instagram"),
    ("フェイスブック", "Facebook"),
    ("リンクトイン", "LinkedIn"),
    ("スポティファイ", "Spotify"),
    ("ネットフリックス", "Netflix"),
    ("エクセル", "Excel"),
    ("パワーポイント", "PowerPoint"),
    ("ズーム", "Zoom"),
    ("チームズ", "Teams"),
    ("チャットワーク", "ChatWork"),
    ("テレグラム", "Telegram"),
    ("フィグマ", "Figma"),
    ("ジラ", "Jira"),
    ("コンフルエンス", "Confluence"),
    ("オブシディアン", "Obsidian"),
    ("セールスフォース", "Salesforce"),
    ("ハブスポット", "HubSpot"),
    ("ストライプ", "Stripe"),
    ("ショッピファイ", "Shopify"),
    // 生成AI・音声
    ("チャットジーピーティー", "ChatGPT"),
    ("ジーピーティー", "GPT"),
    ("オープンエーアイ", "OpenAI"),
    ("アンソロピック", "Anthropic"),
    ("ジェミニ", "Gemini"),
    ("クロード", "Claude"),
    ("パープレキシティ", "Perplexity"),
    ("ディープシーク", "DeepSeek"),
    ("オラマ", "Ollama"),
    ("グロック", "Grok"),
    ("コパイロット", "Copilot"),
    ("ハギングフェイス", "Hugging Face"),
    ("ステーブルディフュージョン", "Stable Diffusion"),
    ("ミッドジャーニー", "Midjourney"),
    ("コンフィーユーアイ", "ComfyUI"),
    ("ウィスパー", "Whisper"),
    ("アールエージー", "RAG"),
    ("エーエスアール", "ASR"),
    ("ティーティーエス", "TTS"),
    ("ブイエーディー", "VAD"),
    // ビジネス略語
    ("ケーピーアイ", "KPI"),
    ("ケージーアイ", "KGI"),
    ("オーケーアール", "OKR"),
    ("アールオーアイ", "ROI"),
    ("ピーオーシー", "PoC"),
    ("サース", "SaaS"),
    ("パース", "PaaS"),
    ("イアース", "IaaS"),
    ("ビートゥービー", "BtoB"),
    ("ビートゥーシー", "BtoC"),
    ("エスエルエー", "SLA"),
    ("エヌディーエー", "NDA"),
    ("エムブイピー", "MVP"),
    ("エフエーキュー", "FAQ"),
    ("キューアール", "QR"),
    ("ディーエックス", "DX"),
    ("イーシー", "EC"),
    ("シーアールエム", "CRM"),
    ("イーアールピー", "ERP"),
    ("エスシーエム", "SCM"),
    ("ピーディーシーエー", "PDCA"),
    ("ダブリューアイピー", "WIP"),
    ("ティービーディー", "TBD"),
    ("イーティーエー", "ETA"),
];

const COUNTERS: &[&str] = &[
    "円",
    "人",
    "個",
    "回",
    "年",
    "歳",
    "才",
    "枚",
    "本",
    "件",
    "度",
    "時",
    "分",
    "秒",
    "日",
    "月",
    "週",
    "点",
    "位",
    "台",
    "冊",
    "匹",
    "頭",
    "杯",
    "階",
    "号",
    "倍",
    "割",
    "%",
    "％",
    "パーセント",
    "キロ",
    "メートル",
    "グラム",
    "センチ",
    "ミリ",
    "ドル",
    "ページ",
    "万",
    "億",
];

const KEEP_WORDS: &[&str] = &[
    "八百屋",
    "八百長",
    "嘘八百",
    "五十嵐",
    "十八番",
    "百人一首",
    "千葉",
    "千代田",
    "百貨店",
    "百科",
    "万引き",
    "万が一",
    "万一",
    "千差万別",
    "一石二鳥",
    "十人十色",
    "百聞",
    "百発百中",
    "九十九里",
    "三十路",
    "四十肩",
    "五十肩",
    "十字",
    "十二支",
    "一千一秒",
    "一点一点",
    "一か八か",
    "九九",
    "一から十まで",
    "一も二もなく",
];

fn digit_val(c: char) -> Option<u64> {
    match c {
        '〇' | '零' => Some(0),
        '一' => Some(1),
        '二' => Some(2),
        '三' => Some(3),
        '四' => Some(4),
        '五' => Some(5),
        '六' => Some(6),
        '七' => Some(7),
        '八' => Some(8),
        '九' => Some(9),
        _ => None,
    }
}

fn small_val(c: char) -> Option<u64> {
    match c {
        '十' => Some(10),
        '百' => Some(100),
        '千' => Some(1000),
        _ => None,
    }
}

fn big_val(c: char) -> Option<u64> {
    match c {
        '万' => Some(10_000),
        '億' => Some(100_000_000),
        '兆' => Some(1_000_000_000_000),
        _ => None,
    }
}

fn is_num_char(c: char) -> bool {
    digit_val(c).is_some() || small_val(c).is_some() || big_val(c).is_some()
}

fn is_kanji(c: char) -> bool {
    ('\u{3400}'..='\u{4DBF}').contains(&c)
        || ('\u{4E00}'..='\u{9FFF}').contains(&c)
        || ('\u{F900}'..='\u{FAFF}').contains(&c)
        || matches!(c, '々' | '〆')
}

fn kanji_to_int(run: &str) -> Option<u64> {
    if !run
        .chars()
        .any(|c| small_val(c).is_some() || big_val(c).is_some())
    {
        let mut n: u64 = 0;
        for c in run.chars() {
            n = n.checked_mul(10)?.checked_add(digit_val(c)?)?;
        }
        return Some(n);
    }
    let mut total: u64 = 0;
    let mut section: u64 = 0;
    let mut cur: Option<u64> = None;
    for c in run.chars() {
        if let Some(d) = digit_val(c) {
            if cur.is_some() {
                return None;
            }
            cur = Some(d);
        } else if let Some(s) = small_val(c) {
            section = section.checked_add(cur.unwrap_or(1).checked_mul(s)?)?;
            cur = None;
        } else {
            let b = big_val(c)?;
            section = section.checked_add(cur.unwrap_or(0))?;
            if section == 0 {
                return None;
            }
            total = total.checked_add(section.checked_mul(b)?)?;
            section = 0;
            cur = None;
        }
    }
    total.checked_add(section)?.checked_add(cur.unwrap_or(0))
}

fn format_japanese(mut n: u64) -> String {
    if n < 10_000 {
        return n.to_string();
    }
    let mut out = String::new();
    for (unit, name) in [
        (1_000_000_000_000u64, "兆"),
        (100_000_000u64, "億"),
        (10_000u64, "万"),
    ] {
        let q = n / unit;
        n %= unit;
        if q > 0 {
            out.push_str(&format!("{q}{name}"));
        }
    }
    if n > 0 {
        out.push_str(&n.to_string());
    }
    out
}

fn should_convert(run: &str, before: Option<char>, after: &str) -> bool {
    let first = match run.chars().next() {
        Some(c) => c,
        None => return false,
    };
    if big_val(first).is_some() {
        return false;
    }
    let has_unit = run
        .chars()
        .any(|c| small_val(c).is_some() || big_val(c).is_some());
    let has_zero = run.chars().any(|c| matches!(c, '〇' | '零'));
    let char_len = run.chars().count();
    if !has_unit && !has_zero {
        if char_len == 1 {
            let prev_kanji = before.is_some_and(is_kanji);
            let next_kanji = after.chars().next().is_some_and(is_kanji);
            return !prev_kanji && !next_kanji && !after.starts_with('つ');
        }
        return false;
    }
    if !has_unit && char_len < 2 {
        return false;
    }
    if char_len == 1 {
        if run == "十"
            && after.starts_with('分')
            && !before.is_some_and(|c| is_num_char(c) || c.is_ascii_digit())
        {
            return false;
        }
        return COUNTERS.iter().any(|u| after.starts_with(u));
    }
    true
}

fn mask_keep_words(text: &str) -> (String, Vec<&'static str>) {
    let mut words: Vec<&'static str> = KEEP_WORDS.to_vec();
    words.sort_by_key(|w| std::cmp::Reverse(w.chars().count()));
    let mut kept = Vec::new();
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while pos < text.len() {
        if let Some(&w) = words.iter().find(|&&w| text[pos..].starts_with(w)) {
            let marker = char::from_u32(0xE000 + kept.len() as u32).unwrap();
            kept.push(w);
            out.push(marker);
            pos += w.len();
        } else {
            let c = text[pos..].chars().next().unwrap();
            out.push(c);
            pos += c.len_utf8();
        }
    }
    (out, kept)
}

fn replace_decimals(text: &str) -> String {
    let mut s = text.to_string();
    // 1. ([0-9]+)点([_DIGITS]+) -> 1.2
    s = replace_ascii_ten_kanji(&s);
    // 2. (?<![_NUM_CHARS])([_DIGITS])点([_DIGITS]+) -> 1.2
    s = replace_kanji1_ten_kanji(&s);
    // 3. (?<![0-9.])([0-9]+)点([0-9]+)(?![0-9]) -> 1.2
    s = replace_ascii_ten_ascii(&s);
    // 4. ([0-9]+)点([_DIGITS]+)
    s = replace_ascii_ten_kanji(&s);
    // 5. (?<![_NUM_CHARS])([_DIGITS]+)点([0-9]+) -> 1.2
    s = replace_kanji_ten_ascii(&s);
    // 6. ([0-9](?:[0-9.]*[0-9])?)\s*パーセント -> \1%
    replace_percent(&s)
}

fn replace_ascii_ten_kanji(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find('点') {
        let ten = pos + rel;
        let left_end = ten;
        let mut left_start = left_end;
        while left_start > pos && text.as_bytes()[left_start - 1].is_ascii_digit() {
            left_start -= 1;
        }
        let right_start = ten + '点'.len_utf8();
        let mut right_end = right_start;
        for c in text[right_start..].chars() {
            if digit_val(c).is_some() {
                right_end += c.len_utf8();
            } else {
                break;
            }
        }
        if left_start < left_end && right_start < right_end {
            out.push_str(&text[pos..left_start]);
            out.push_str(&text[left_start..left_end]);
            out.push('.');
            for c in text[right_start..right_end].chars() {
                out.push_str(&digit_val(c).unwrap().to_string());
            }
            pos = right_end;
        } else {
            out.push_str(&text[pos..right_start]);
            pos = right_start;
        }
    }
    out.push_str(&text[pos..]);
    out
}

fn replace_kanji1_ten_kanji(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find('点') {
        let ten = pos + rel;
        let right_start = ten + '点'.len_utf8();
        let prev = text[pos..ten].chars().next_back();
        if let Some(d1) = prev.and_then(digit_val) {
            let d1_start = ten - prev.unwrap().len_utf8();
            let prev_prev_ok = text[..d1_start]
                .chars()
                .next_back()
                .is_none_or(|c| !is_num_char(c));
            let mut right_end = right_start;
            for c in text[right_start..].chars() {
                if digit_val(c).is_some() {
                    right_end += c.len_utf8();
                } else {
                    break;
                }
            }
            if prev_prev_ok && right_start < right_end {
                out.push_str(&text[pos..d1_start]);
                out.push_str(&d1.to_string());
                out.push('.');
                for c in text[right_start..right_end].chars() {
                    out.push_str(&digit_val(c).unwrap().to_string());
                }
                pos = right_end;
                continue;
            }
        }
        out.push_str(&text[pos..right_start]);
        pos = right_start;
    }
    out.push_str(&text[pos..]);
    out
}

fn replace_ascii_ten_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find('点') {
        let ten = pos + rel;
        let right_start = ten + '点'.len_utf8();
        let mut left_start = ten;
        while left_start > pos && text.as_bytes()[left_start - 1].is_ascii_digit() {
            left_start -= 1;
        }
        let before_ok =
            left_start == 0 || !matches!(text.as_bytes()[left_start - 1], b'0'..=b'9' | b'.');
        let mut right_end = right_start;
        while right_end < text.len() && text.as_bytes()[right_end].is_ascii_digit() {
            right_end += 1;
        }
        if before_ok && left_start < ten && right_start < right_end {
            out.push_str(&text[pos..left_start]);
            out.push_str(&text[left_start..ten]);
            out.push('.');
            out.push_str(&text[right_start..right_end]);
            pos = right_end;
        } else {
            out.push_str(&text[pos..right_start]);
            pos = right_start;
        }
    }
    out.push_str(&text[pos..]);
    out
}

fn replace_kanji_ten_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find('点') {
        let ten = pos + rel;
        let right_start = ten + '点'.len_utf8();
        let mut left_start = ten;
        for c in text[pos..ten].chars().rev() {
            if digit_val(c).is_some() {
                left_start -= c.len_utf8();
            } else {
                break;
            }
        }
        let before_ok = text[..left_start]
            .chars()
            .next_back()
            .is_none_or(|c| !is_num_char(c));
        let mut right_end = right_start;
        while right_end < text.len() && text.as_bytes()[right_end].is_ascii_digit() {
            right_end += 1;
        }
        if before_ok && left_start < ten && right_start < right_end {
            out.push_str(&text[pos..left_start]);
            for c in text[left_start..ten].chars() {
                out.push_str(&digit_val(c).unwrap().to_string());
            }
            out.push('.');
            out.push_str(&text[right_start..right_end]);
            pos = right_end;
        } else {
            out.push_str(&text[pos..right_start]);
            pos = right_start;
        }
    }
    out.push_str(&text[pos..]);
    out
}

fn replace_percent(text: &str) -> String {
    let needle = "パーセント";
    if !text.contains(needle) {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find(needle) {
        let start = pos + rel;
        let mut num_end = start;
        while num_end > pos {
            let c = text[pos..num_end].chars().next_back().unwrap();
            if c.is_whitespace() {
                num_end -= c.len_utf8();
            } else {
                break;
            }
        }
        let mut num_start = num_end;
        if num_end > pos && text.as_bytes()[num_end - 1].is_ascii_digit() {
            while num_start > pos && matches!(text.as_bytes()[num_start - 1], b'0'..=b'9' | b'.') {
                num_start -= 1;
            }
            // 先頭が '.' なら数字まで進める
            while num_start < num_end && text.as_bytes()[num_start] == b'.' {
                num_start += 1;
            }
        }
        if num_start < num_end {
            out.push_str(&text[pos..num_end]);
            out.push('%');
            pos = start + needle.len();
        } else {
            out.push_str(&text[pos..start + needle.len()]);
            pos = start + needle.len();
        }
    }
    out.push_str(&text[pos..]);
    out
}

pub fn convert_kanji_numbers(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let (masked, kept) = mask_keep_words(text);
    let mut out = String::with_capacity(masked.len());
    let mut pos = 0;
    while pos < masked.len() {
        let ch = masked[pos..].chars().next().unwrap();
        if is_num_char(ch) {
            let start = pos;
            let mut end = start;
            for c in masked[start..].chars() {
                if is_num_char(c) {
                    end += c.len_utf8();
                } else {
                    break;
                }
            }
            let run = &masked[start..end];
            let before = masked[..start].chars().next_back();
            let after = &masked[end..];
            if should_convert(run, before, after) {
                if let Some(val) = kanji_to_int(run) {
                    out.push_str(&format_japanese(val));
                    pos = end;
                    continue;
                }
            }
            out.push_str(run);
            pos = end;
        } else {
            out.push(ch);
            pos += ch.len_utf8();
        }
    }
    let mut result = replace_decimals(&out);
    for (i, w) in kept.iter().enumerate() {
        let marker = char::from_u32(0xE000 + i as u32).unwrap();
        result = result.replace(marker, w);
    }
    result
}

pub fn to_halfwidth_alnum(text: &str) -> String {
    text.chars()
        .map(|c| {
            let o = c as u32;
            if (0xFF10..=0xFF19).contains(&o)
                || (0xFF21..=0xFF3A).contains(&o)
                || (0xFF41..=0xFF5A).contains(&o)
            {
                char::from_u32(o - 0xFEE0).unwrap_or(c)
            } else if c == '％' {
                '%'
            } else {
                c
            }
        })
        .collect()
}

#[derive(Debug, Clone)]
pub struct TextNormalizer {
    disabled: HashSet<String>,
    rules: Vec<(&'static str, &'static str)>,
}

impl Default for TextNormalizer {
    fn default() -> Self {
        Self::new(std::iter::empty::<&str>())
    }
}

impl TextNormalizer {
    pub fn new<I, S>(disabled: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut n = Self {
            disabled: HashSet::new(),
            rules: Vec::new(),
        };
        n.set_disabled(disabled);
        n
    }

    pub fn set_disabled<I, S>(&mut self, disabled: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        self.disabled = disabled
            .into_iter()
            .map(|s| s.as_ref().to_string())
            .collect();
        let mut rules: Vec<(&'static str, &'static str)> = BUILTIN_TERMS
            .iter()
            .copied()
            .filter(|(k, _)| !self.disabled.contains(*k))
            .collect();
        rules.sort_by_key(|(k, _)| std::cmp::Reverse(k.chars().count()));
        self.rules = rules;
    }

    pub fn normalize(&self, text: &str) -> String {
        if text.is_empty() {
            return String::new();
        }
        let mut t = to_halfwidth_alnum(text);
        if t.chars().any(is_katakana_word_char) {
            for &(src, dst) in &self.rules {
                if t.contains(src) {
                    t = replace_katakana_word(&t, src, dst);
                }
            }
        }
        convert_kanji_numbers(&t)
    }
}
