//! 音声コマンド（改行・記号・「今のなし」）の展開（Python 版 `shared/voice_commands.py` と同じ決まり）。

const NOUN_VERB_SUFFIXES: &[&str] = &[
    "コード",
    "文字",
    "記号",
    "位置",
    "処理",
    "設定",
    "なし",
    "あり",
    "付き",
    "つき",
    "幅",
    "数",
    "分",
    "前",
    "後",
    "先",
    "元",
    "する",
    "して",
    "した",
    "します",
    "しない",
    "しよ",
    "しよう",
    "でき",
    "され",
    "せず",
    "せよ",
    "を",
    "が",
    "は",
    "の",
    "に",
    "で",
    "と",
    "へ",
    "から",
    "まで",
    "も",
    "や",
    "だけ",
    "など",
    "って",
];

const COMPOUND_PREFIXES: &[&str] = &["自動", "強制", "手動", "連続", "不要な", "余分な", "末尾の"];

const NEWLINE_WORDS: &[&str] = &["改行", "かいぎょう", "カイギョウ"];

const SYMBOL_NOT_CMD: &[&str] = &[
    "を", "が", "は", "の", "も", "って", "付き", "つき", "する", "して", "した",
];

fn is_lead_sep(c: char) -> bool {
    c.is_whitespace() || matches!(c, '、' | '，' | ',')
}

fn is_trail_sep(c: char) -> bool {
    c.is_whitespace() || matches!(c, '、' | '，' | '。' | '．' | '.' | ',')
}

pub fn apply_voice_commands(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let stripped =
        text.trim_matches(|c: char| matches!(c, ' ' | '、' | '，' | '。' | '．' | '.' | ','));
    if NEWLINE_WORDS.contains(&stripped) {
        return "\n".to_string();
    }

    // 改行コマンドの展開
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while pos < text.len() {
        // lead を読む
        let lead_start = pos;
        let mut w_start = lead_start;
        for c in text[lead_start..].chars() {
            if is_lead_sep(c) {
                w_start += c.len_utf8();
            } else {
                break;
            }
        }
        if let Some(&word) = NEWLINE_WORDS
            .iter()
            .find(|&&w| text[w_start..].starts_with(w))
        {
            let w_end = w_start + word.len();
            let after_word = &text[w_end..];
            let has_bad_suffix = NOUN_VERB_SUFFIXES.iter().any(|s| after_word.starts_with(s));
            let has_bad_prefix = COMPOUND_PREFIXES
                .iter()
                .any(|p| text[..w_start].ends_with(p));
            if !has_bad_suffix && !has_bad_prefix {
                let mut trail_end = w_end;
                for c in text[w_end..].chars() {
                    if is_trail_sep(c) {
                        trail_end += c.len_utf8();
                    } else {
                        break;
                    }
                }
                out.push('\n');
                pos = trail_end;
                continue;
            }
        }
        let c = text[pos..].chars().next().unwrap();
        out.push(c);
        pos += c.len_utf8();
    }

    // \n[ \t、，]+ -> \n
    let mut cleaned = String::with_capacity(out.len());
    let mut after_nl = false;
    for c in out.chars() {
        if after_nl && matches!(c, ' ' | '\t' | '、' | '，') {
            continue;
        }
        after_nl = c == '\n';
        cleaned.push(c);
    }

    let sym = apply_symbol_commands(&cleaned);
    sym.trim_matches(|c| matches!(c, ' ' | '\t')).to_string()
}

fn replace_symbol_rule(text: &str, patterns: &[&str], consume_lead: bool, symbol: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while pos < text.len() {
        let mut p_start = pos;
        if consume_lead {
            for c in text[pos..].chars() {
                if is_trail_sep(c) {
                    p_start += c.len_utf8();
                } else {
                    break;
                }
            }
        }
        if let Some(&pat) = patterns.iter().find(|&&p| text[p_start..].starts_with(p)) {
            let p_end = p_start + pat.len();
            if !SYMBOL_NOT_CMD.iter().any(|s| text[p_end..].starts_with(s)) {
                let mut trail_end = p_end;
                for c in text[p_end..].chars() {
                    if is_trail_sep(c) {
                        trail_end += c.len_utf8();
                    } else {
                        break;
                    }
                }
                out.push_str(symbol);
                pos = trail_end;
                continue;
            }
        }
        let c = text[pos..].chars().next().unwrap();
        out.push(c);
        pos += c.len_utf8();
    }
    out
}

pub fn apply_symbol_commands(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    const KAGI_CLOSE: &[&str] = &[
        "かぎかっこ閉じ",
        "かぎかっことじ",
        "かぎかっこトジ",
        "かぎカッコ閉じ",
        "かぎカッコとじ",
        "かぎカッコトジ",
        "かぎ括弧閉じ",
        "かぎ括弧とじ",
        "かぎ括弧トジ",
        "カギかっこ閉じ",
        "カギかっことじ",
        "カギかっこトジ",
        "カギカッコ閉じ",
        "カギカッコとじ",
        "カギカッコトジ",
        "カギ括弧閉じ",
        "カギ括弧とじ",
        "カギ括弧トジ",
        "鍵かっこ閉じ",
        "鍵かっことじ",
        "鍵かっこトジ",
        "鍵カッコ閉じ",
        "鍵カッコとじ",
        "鍵カッコトジ",
        "鍵括弧閉じ",
        "鍵括弧とじ",
        "鍵括弧トジ",
    ];
    const KAGI_OPEN: &[&str] = &[
        "かぎかっこ開き",
        "かぎかっこひらき",
        "かぎかっこヒラキ",
        "かぎカッコ開き",
        "かぎカッコひらき",
        "かぎカッコヒラキ",
        "かぎ括弧開き",
        "かぎ括弧ひらき",
        "かぎ括弧ヒラキ",
        "カギかっこ開き",
        "カギかっこひらき",
        "カギかっこヒラキ",
        "カギカッコ開き",
        "カギカッコひらき",
        "カギカッコヒラキ",
        "カギ括弧開き",
        "カギ括弧ひらき",
        "カギ括弧ヒラキ",
        "鍵かっこ開き",
        "鍵かっこひらき",
        "鍵かっこヒラキ",
        "鍵カッコ開き",
        "鍵カッコひらき",
        "鍵カッコヒラキ",
        "鍵括弧開き",
        "鍵括弧ひらき",
        "鍵括弧ヒラキ",
        "かぎかっこ",
        "かぎカッコ",
        "かぎ括弧",
        "カギかっこ",
        "カギカッコ",
        "カギ括弧",
        "鍵かっこ",
        "鍵カッコ",
        "鍵括弧",
    ];
    const PAREN_CLOSE: &[&str] = &[
        "かっこ閉じ",
        "かっことじ",
        "かっこトジ",
        "カッコ閉じ",
        "カッコとじ",
        "カッコトジ",
        "括弧閉じ",
        "括弧とじ",
        "括弧トジ",
    ];
    const PAREN_OPEN: &[&str] = &[
        "かっこ開き",
        "かっこひらき",
        "かっこヒラキ",
        "カッコ開き",
        "カッコひらき",
        "カッコヒラキ",
        "括弧開き",
        "括弧ひらき",
        "括弧ヒラキ",
    ];
    const QUESTION: &[&str] = &[
        "はてなマーク",
        "ハテナマーク",
        "クエスチョンマーク",
        "疑問符",
    ];
    const EXCLAM: &[&str] = &[
        "びっくりマーク",
        "ビックリマーク",
        "エクスクラメーションマーク",
        "感嘆符",
    ];

    let mut t = text.to_string();
    t = replace_symbol_rule(&t, KAGI_CLOSE, true, "」");
    t = replace_symbol_rule(&t, KAGI_OPEN, false, "「");
    t = replace_symbol_rule(&t, PAREN_CLOSE, true, "）");
    t = replace_symbol_rule(&t, PAREN_OPEN, false, "（");
    t = replace_symbol_rule(&t, QUESTION, true, "？");
    t = replace_symbol_rule(&t, EXCLAM, true, "！");
    t
}

/// 発話全体が「今のなし」「取り消し」などの取り消し指示なら true。
pub fn is_undo_command(text: &str) -> bool {
    if text.is_empty() {
        return false;
    }
    let core: String = text
        .chars()
        .filter(|c| {
            !c.is_whitespace()
                && !matches!(
                    c,
                    '、' | '，' | '。' | '．' | ',' | '.' | '!' | '！' | '?' | '？'
                )
        })
        .collect();
    if matches!(core.as_str(), "取り消し" | "取り消して" | "とりけし") {
        return true;
    }
    let rest = if let Some(r) = core.strip_prefix("今の") {
        r
    } else if let Some(r) = core.strip_prefix("いまの") {
        r
    } else {
        return false;
    };
    let rest = rest
        .strip_prefix('は')
        .or_else(|| rest.strip_prefix('を'))
        .unwrap_or(rest);
    matches!(
        rest,
        "なし"
            | "無し"
            | "ナシ"
            | "取り消し"
            | "取り消して"
            | "とりけし"
            | "消して"
            | "けして"
            | "削除"
            | "削除して"
    )
}
