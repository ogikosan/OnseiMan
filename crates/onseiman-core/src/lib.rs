//! OnseiMan の、OS に寄らない中身。Python 版の `shared/` と同じ決まりで動く。

pub mod acronyms;
pub mod audio;
pub mod config;
pub mod dict_format;
pub mod dict_sync;
pub mod dictionary;
pub mod fillers;
pub mod history;
pub mod kana;
pub mod learner;
pub mod llm_refine;
pub mod normalize;
pub mod segmenter;
pub mod voice_commands;

/// OnseiMan の版（Cargo.toml の workspace.package.version）
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
