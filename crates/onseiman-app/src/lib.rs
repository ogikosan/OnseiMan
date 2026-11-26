#![recursion_limit = "256"]
//! OnseiMan クライアント本体（Rust 版）。
//!
//! ホットキー録音 → LAN / ローカル認識 → テキスト整形（辞書・言いよどみ・正規化・AI 仕上げ）
//! → カーソル位置へ貼り付け → 履歴保存、および `client/web/` 管理画面を提供する。

pub mod chatgpt_auth;
pub mod discovery;
pub mod live_session;
pub mod pipeline;
pub mod transcriber;
pub mod web_ui;
