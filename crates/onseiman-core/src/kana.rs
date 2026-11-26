//! ひらがな・カタカナの相互変換（Python 版 `shared/kana.py` と同じ決まり）。

const HIRA_START: u32 = 0x3041;
const HIRA_END: u32 = 0x3096;
const KATA_OFFSET: u32 = 0x60;

pub fn is_hiragana(c: char) -> bool {
    let o = c as u32;
    (HIRA_START..=HIRA_END).contains(&o)
}

pub fn is_katakana_word_char(c: char) -> bool {
    ('\u{30A1}'..='\u{30FA}').contains(&c) || c == '\u{30FC}'
}

pub fn to_katakana(text: &str) -> String {
    text.chars()
        .map(|c| {
            let o = c as u32;
            if (HIRA_START..=HIRA_END).contains(&o) {
                char::from_u32(o + KATA_OFFSET).unwrap_or(c)
            } else {
                c
            }
        })
        .collect()
}

pub fn to_hiragana(text: &str) -> String {
    text.chars()
        .map(|c| {
            let o = c as u32;
            if (HIRA_START + KATA_OFFSET..=HIRA_END + KATA_OFFSET).contains(&o) {
                char::from_u32(o - KATA_OFFSET).unwrap_or(c)
            } else {
                c
            }
        })
        .collect()
}

pub fn is_kana_only(text: &str) -> bool {
    !text.is_empty()
        && text.chars().all(|c| {
            ('\u{3041}'..='\u{3096}').contains(&c)
                || ('\u{30A1}'..='\u{30FA}').contains(&c)
                || matches!(c, 'ー' | '・' | 'ゝ' | 'ゞ' | 'ヽ' | 'ヾ')
        })
}

/// かなだけの言葉なら、ひらがな・カタカナ両方の書き方を返す。
pub fn kana_variants(text: &str) -> Vec<String> {
    if !is_kana_only(text) {
        return vec![text.to_string()];
    }
    let mut out = Vec::new();
    for v in [text.to_string(), to_hiragana(text), to_katakana(text)] {
        if !out.contains(&v) {
            out.push(v);
        }
    }
    out
}

/// カタカナの一続きの語として現れた `needle` だけを `replacement` に置き換える。
/// （前後がカタカナ・長音でないこと。「アイ」が「アイデア」の中で化けない。）
pub fn replace_katakana_word(text: &str, needle: &str, replacement: &str) -> String {
    if needle.is_empty() || !text.contains(needle) {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut pos = 0;
    while let Some(rel) = text[pos..].find(needle) {
        let start = pos + rel;
        let end = start + needle.len();
        let prev_ok = text[..start]
            .chars()
            .next_back()
            .is_none_or(|c| !is_katakana_word_char(c));
        let next_ok = text[end..]
            .chars()
            .next()
            .is_none_or(|c| !is_katakana_word_char(c));
        if prev_ok && next_ok {
            out.push_str(&text[pos..start]);
            out.push_str(replacement);
            pos = end;
        } else {
            let next_char_len = text[start..].chars().next().map_or(1, char::len_utf8);
            out.push_str(&text[pos..start + next_char_len]);
            pos = start + next_char_len;
        }
    }
    out.push_str(&text[pos..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kana_roundtrip_and_boundary() {
        assert_eq!(to_katakana("すーぱーべーす"), "スーパーベース");
        assert_eq!(to_hiragana("スーパーベース"), "すーぱーべーす");
        assert!(is_kana_only("すーぱーべーす"));
        assert!(!is_kana_only("ComfyUI"));
        assert_eq!(replace_katakana_word("アイデア", "アイ", "AI"), "アイデア");
        assert_eq!(
            replace_katakana_word("アイを使う", "アイ", "AI"),
            "AIを使う"
        );
    }
}
