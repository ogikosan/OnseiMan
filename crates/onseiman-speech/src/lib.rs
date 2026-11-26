//! speech.cpp（MIT）の worker で Haas-ja-v1.0（Apache-2.0）を動かす。
//!
//! - `engine`: `speech worker <モデル>` を 1 つ起動して、AI を読み込んだまま待たせ、JSON Lines でやりとりする
//! - `setup`: speech.cpp と Haas-ja のデータを、OnseiMan のフォルダの中に用意する（決まった版・確認用の値つき）

pub mod engine;
pub mod setup;

pub use engine::{keyword_lines, EngineError, SpeechEngine, MAX_SEGMENT_SEC};
