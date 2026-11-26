//! 言いよどみ（フィラー）の除去（Python 版 `shared/fillers.py` と同じ決まり）。

pub const DEFAULT_FILLERS: &[&str] = &[
    "えーと",
    "えっと",
    "えーっと",
    "ええと",
    "えと",
    "えー",
    "えぇ",
    "あのー",
    "あの",
    "そのー",
    "その",
    "うーん",
    "うんうん",
    "うん",
    "んー",
    "ん",
    "あー",
    "まあ",
    "まー",
    "なんか",
];

fn is_stretch(c: char) -> bool {
    matches!(
        c,
        'ー' | 'ぁ' | 'ぃ' | 'ぅ' | 'ぇ' | 'ぉ' | 'っ' | '〜' | '~'
    )
}

fn is_delim(c: char) -> bool {
    c.is_whitespace()
        || matches!(
            c,
            '、' | '。'
                | '，'
                | '．'
                | ','
                | '.'
                | '！'
                | '？'
                | '!'
                | '?'
                | '「'
                | '」'
                | '『'
                | '』'
                | '（'
                | '）'
                | '('
                | ')'
        )
}

fn is_leading_boundary(c: char) -> bool {
    c.is_whitespace() || matches!(c, '。' | '．' | '.' | '！' | '？' | '!' | '?')
}

fn is_punct_after(c: char) -> bool {
    c.is_whitespace() || matches!(c, '、' | '，' | ',')
}

/// `word` の各文字のあとに `is_stretch` が 0 個以上続いた最長一致の終了位置（バイト長）を返す。
/// Python の `"".join(re.escape(ch) + "[ーぁぃぅぇぉっ〜~]*" for ch in word)` の欲張り一致（バックトラックあり）と同じ。
fn match_token(s: &str, word: &[char]) -> Option<usize> {
    let (&wch, rest) = match word.split_first() {
        Some(x) => x,
        None => return Some(0),
    };
    let c = s.chars().next()?;
    if c != wch {
        return None;
    }
    let after_ch = c.len_utf8();
    let mut ends = vec![after_ch];
    let mut p = after_ch;
    for sc in s[after_ch..].chars() {
        if is_stretch(sc) {
            p += sc.len_utf8();
            ends.push(p);
        } else {
            break;
        }
    }
    for end in ends.into_iter().rev() {
        if let Some(sub) = match_token(&s[end..], rest) {
            return Some(end + sub);
        }
    }
    None
}

fn consume_punct_after(s: &str, mut pos: usize) -> usize {
    for c in s[pos..].chars() {
        if is_punct_after(c) {
            pos += c.len_utf8();
        } else {
            break;
        }
    }
    pos
}

#[derive(Debug, Clone)]
pub struct FillerRemover {
    fillers: Vec<Vec<char>>,
    stretched: Vec<Vec<char>>,
}

impl Default for FillerRemover {
    fn default() -> Self {
        Self::new(DEFAULT_FILLERS.iter().copied())
    }
}

impl FillerRemover {
    pub fn new<I, S>(fillers: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut r = Self {
            fillers: Vec::new(),
            stretched: Vec::new(),
        };
        r.set_fillers(fillers);
        r
    }

    pub fn set_fillers<I, S>(&mut self, fillers: I)
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let mut uniq: Vec<String> = Vec::new();
        for w in fillers {
            let t = w.as_ref().trim().to_string();
            if !t.is_empty() && !uniq.contains(&t) {
                uniq.push(t);
            }
        }
        // 長い語を先に試す（Python の sorted(..., key=len, reverse=True) に合わせる）
        uniq.sort_by(|a, b| {
            b.chars()
                .count()
                .cmp(&a.chars().count())
                .then_with(|| a.cmp(b))
        });
        self.fillers = uniq.iter().map(|w| w.chars().collect()).collect();
        self.stretched = uniq
            .iter()
            .filter(|w| w.contains('ー') || w.contains('っ'))
            .map(|w| w.chars().collect())
            .collect();
    }

    pub fn remove(&self, text: &str) -> String {
        if text.is_empty() || self.fillers.is_empty() {
            return text.to_string();
        }
        let mut result = text.to_string();
        for _ in 0..10 {
            let before = result.clone();
            result = self.pass_delimited(&result);
            if !self.stretched.is_empty() {
                result = self.pass_leading(&result);
            }
            if result == before {
                break;
            }
        }
        tidy_punctuation(&result)
    }

    fn pass_delimited(&self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let mut pos = 0;
        while pos < text.len() {
            let ch = text[pos..].chars().next().unwrap();
            let at_start = pos == 0;
            // (1) 文頭でそのままフィラーが来る場合
            if at_start {
                if let Some(end) = self.match_any_delimited(text, 0) {
                    pos = consume_punct_after(text, end);
                    continue;
                }
            }
            // (2) 区切り文字の直後にフィラーが来る場合（区切り文字自体は残す）
            if is_delim(ch) {
                let after_delim = pos + ch.len_utf8();
                if let Some(end) = self.match_any_delimited(text, after_delim) {
                    out.push(ch);
                    pos = consume_punct_after(text, end);
                    continue;
                }
            }
            out.push(ch);
            pos += ch.len_utf8();
        }
        out
    }

    fn match_any_delimited(&self, text: &str, start: usize) -> Option<usize> {
        let slice = &text[start..];
        for w in &self.fillers {
            if let Some(len) = match_token(slice, w) {
                let end = start + len;
                let followed_ok = text[end..].chars().next().is_none_or(is_delim);
                if followed_ok {
                    return Some(end);
                }
            }
        }
        None
    }

    fn pass_leading(&self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let mut pos = 0;
        while pos < text.len() {
            let ch = text[pos..].chars().next().unwrap();
            if pos == 0 {
                if let Some(end) = self.match_any_stretched(text, 0) {
                    pos = consume_punct_after(text, end);
                    continue;
                }
            }
            if is_leading_boundary(ch) {
                let after = pos + ch.len_utf8();
                if let Some(end) = self.match_any_stretched(text, after) {
                    out.push(ch);
                    pos = consume_punct_after(text, end);
                    continue;
                }
            }
            out.push(ch);
            pos += ch.len_utf8();
        }
        out
    }

    fn match_any_stretched(&self, text: &str, start: usize) -> Option<usize> {
        let slice = &text[start..];
        for w in &self.stretched {
            if let Some(len) = match_token(slice, w) {
                return Some(start + len);
            }
        }
        None
    }
}

fn is_comma(c: char) -> bool {
    matches!(c, '、' | '，' | ',')
}

fn is_sentence_end(c: char) -> bool {
    matches!(c, '。' | '．' | '.' | '！' | '？' | '!' | '?')
}

fn tidy_punctuation(text: &str) -> String {
    // 1. [、，,]+([。．.！？!?]) -> \1
    let mut s = String::with_capacity(text.len());
    let chars: Vec<char> = text.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        if is_comma(chars[i]) {
            let mut j = i;
            while j < chars.len() && is_comma(chars[j]) {
                j += 1;
            }
            if j < chars.len() && is_sentence_end(chars[j]) {
                s.push(chars[j]);
                i = j + 1;
                continue;
            }
        }
        s.push(chars[i]);
        i += 1;
    }

    // 2. ([。．.！？!?])[、，,]+ -> \1
    let chars: Vec<char> = s.chars().collect();
    let mut s2 = String::with_capacity(s.len());
    let mut i = 0;
    while i < chars.len() {
        s2.push(chars[i]);
        if is_sentence_end(chars[i]) {
            i += 1;
            while i < chars.len() && is_comma(chars[i]) {
                i += 1;
            }
            continue;
        }
        i += 1;
    }

    // 3. 、{2,} -> 、, 。{2,} -> 。
    let mut s3 = String::with_capacity(s2.len());
    let mut prev: Option<char> = None;
    for c in s2.chars() {
        if (c == '、' && prev == Some('、')) || (c == '。' && prev == Some('。')) {
            continue;
        }
        s3.push(c);
        prev = Some(c);
    }

    // 4. ^[\s、，,。．.]+ を削る
    let trimmed = s3.trim_start_matches(|c: char| {
        c.is_whitespace() || matches!(c, '、' | '，' | ',' | '。' | '．' | '.')
    });

    // 5. \s{2,} -> " "
    let mut s4 = String::with_capacity(trimmed.len());
    let mut in_ws = false;
    for c in trimmed.chars() {
        if c.is_whitespace() {
            if !in_ws {
                s4.push(' ');
                in_ws = true;
            }
        } else {
            s4.push(c);
            in_ws = false;
        }
    }
    s4.trim().to_string()
}
