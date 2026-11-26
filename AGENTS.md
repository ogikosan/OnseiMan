# プロジェクト固有の作業指示（OnseiMan）

親の AGENTS.md を継承する。

## 構成（完全 Rust ワークスペース）
- `crates/onseiman-core`：設定・辞書・言いよどみ除去・表記正規化・音声コマンド・区切り・履歴・辞書同期・AI 仕上げ
- `crates/onseiman-speech`：`speech.cpp` ワーカー連携・モデル自動取得（SHA-256 検証付き）
- `crates/onseiman-server`：認識サーバー（Windows x64 / macOS arm64）
- `crates/onseiman-platform`：OS 固有土台（Win32 Hook / macOS CGEventTap・`cpal` 録音・他アプリミュート・トレイ・非アクティブカプセル・貼付・DPAPI / Keychain・自動起動）
- `crates/onseiman-app`：クライアント本体（`onseiman`、`client/web/` を `127.0.0.1` で配信）
- `client/web/`：管理画面 HTML/CSS/JS

## 実行と検証
- Rust ツールチェーン：`D:\10_Tools\rust`（`RUSTUP_HOME=D:\10_Tools\rust\rustup`・`CARGO_HOME=D:\10_Tools\rust\cargo`、`D:\10_Tools\rust\cargo\bin` を PATH に追加）。
- コード変更時の必須確認：
  - `cargo fmt --all -- --check`
  - `cargo clippy --workspace --all-targets -- -D warnings`
  - `cargo test --workspace`
- 画面（`client/web/`）を変えたとき：`node -e "new Function(require('fs').readFileSync('client/web/app.js','utf8'))"` で文法確認する。
- ビルド＆公開：GitHub Actions（`.github/workflows/rust.yml`）が Windows x64・macOS arm64 の両方について `fmt`・`clippy`・`test`・リリースバイナリ＋`SHA256SUMS` 生成を自動実行する。
- 公開リポジトリ（`ogikosan/OnseiMan`）なので、コミット前に `packaging/private-patterns.txt` の語句が追跡ファイルに含まれていないか必ず確認する。
- バッチ（`*.bat`）は英数字のみ・CRLF。PowerShell（`tools\*.ps1`）は BOM 付き UTF-8・CRLF。Mac スクリプト（`*.command`・`server/setup_macos.sh`）は LF・実行権限（`100755`）。

## 固有の制約
- 利用者のデータ：`config.json`・`dict.json`・`logs/` は消さない・上書きしない。
- 聞き取りの部品 `models/haas-ja`・`tools/speech` は作り直しに時間がかかるため消さない。
- 聞き取り（Haas-ja）は 1 回に 1 つの発話を渡す作り。区切りの長さを変えるときは実音声で欠けないか必ず確認する。
