//! ホットキー名の解釈（Python 版 `client/input_hook.py` の `parse_binding` と同じ名前に対応）。

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MouseButton {
    X1,
    X2,
    Middle,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Binding {
    /// Windows の仮想キーコード（macOS でも共通の識別子として変換して使う）
    Key(u32),
    Mouse(MouseButton),
}

pub fn parse_binding(name: &str) -> Result<Binding, String> {
    let raw = name.trim().to_lowercase();
    if raw.is_empty() {
        return Err("キー名が空です".into());
    }
    match raw.as_str() {
        "mouse_x1" | "mouse_side1" | "xbutton1" => return Ok(Binding::Mouse(MouseButton::X1)),
        "mouse_x2" | "mouse_side2" | "xbutton2" => return Ok(Binding::Mouse(MouseButton::X2)),
        "mouse_middle" | "mbutton" => return Ok(Binding::Mouse(MouseButton::Middle)),
        _ => {}
    }
    if let Some(hex) = raw.strip_prefix("vk:") {
        let h = hex.strip_prefix("0x").unwrap_or(hex);
        let code = u32::from_str_radix(h, 16).map_err(|_| format!("不正なキー指定: {name}"))?;
        return Ok(Binding::Key(code));
    }
    let vk = match raw.as_str() {
        "esc" | "escape" => 0x1B,
        "ralt" | "right_alt" => 0xA5,
        "lalt" | "left_alt" => 0xA4,
        "alt" => 0x12,
        "rctrl" | "right_ctrl" => 0xA3,
        "lctrl" | "left_ctrl" => 0xA2,
        "ctrl" => 0x11,
        "rshift" | "right_shift" => 0xA1,
        "lshift" | "left_shift" => 0xA0,
        "shift" => 0x10,
        "rwin" => 0x5C,
        "lwin" => 0x5B,
        "capslock" | "caps_lock" => 0x14,
        "muhenkan" => 0x1D,
        "henkan" => 0x1C,
        "katakana" | "kana" => 0xF2,
        "pause" => 0x13,
        "scroll_lock" | "scrolllock" => 0x91,
        "insert" => 0x2D,
        "space" => 0x20,
        "f1" => 0x70,
        "f2" => 0x71,
        "f3" => 0x72,
        "f4" => 0x73,
        "f5" => 0x74,
        "f6" => 0x75,
        "f7" => 0x76,
        "f8" => 0x77,
        "f9" => 0x78,
        "f10" => 0x79,
        "f11" => 0x7A,
        "f12" => 0x7B,
        "f13" => 0x7C,
        "f14" => 0x7D,
        "f15" => 0x7E,
        "f16" => 0x7F,
        _ => return Err(format!("未対応のキーです: {name}")),
    };
    Ok(Binding::Key(vk))
}

pub fn format_vk_name(vk: u32) -> String {
    match vk {
        0x1B => "esc".into(),
        0xA5 => "ralt".into(),
        0xA4 => "lalt".into(),
        0xA3 => "rctrl".into(),
        0xA2 => "lctrl".into(),
        0xA1 => "rshift".into(),
        0xA0 => "lshift".into(),
        0x14 => "capslock".into(),
        0x1D => "muhenkan".into(),
        0x1C => "henkan".into(),
        0x13 => "pause".into(),
        0x91 => "scroll_lock".into(),
        0x70..=0x7F => format!("f{}", vk - 0x6F),
        _ => format!("vk:0x{vk:02x}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_known_bindings() {
        assert_eq!(
            parse_binding("mouse_x1").unwrap(),
            Binding::Mouse(MouseButton::X1)
        );
        assert_eq!(
            parse_binding("mouse_side2").unwrap(),
            Binding::Mouse(MouseButton::X2)
        );
        assert_eq!(parse_binding("ralt").unwrap(), Binding::Key(0xA5));
        assert_eq!(parse_binding("muhenkan").unwrap(), Binding::Key(0x1D));
        assert_eq!(parse_binding("f2").unwrap(), Binding::Key(0x71));
        assert_eq!(parse_binding("vk:0x2D").unwrap(), Binding::Key(0x2D));
        assert_eq!(format_vk_name(0xA5), "ralt");
        assert!(parse_binding("unknown_super_key").is_err());
    }
}
