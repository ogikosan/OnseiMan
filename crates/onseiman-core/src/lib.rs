//! OnseiMan の、OS に寄らない中身。Python 版の `shared/` と同じ決まりで動く。

pub mod acronyms;
pub mod audio;
pub mod dict_format;
pub mod segmenter;

/// OnseiMan の版（Cargo.toml の workspace.package.version）
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
