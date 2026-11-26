# Rust への移行計画

2026-11-26 作成。Rust で作り直すと決めた（同日）。作業は `feature/rust` ブランチ。進み具合はいちばん下。

## ねらい

- 配るものを exe 1 つにする。いまは、はじめに Python と部品を数分かけて用意している（`tools/install.ps1`・uv・PyPI）。
- 起動を速くし、使う記憶を減らす。
- 入れるたびに PyPI から最新の部品を取ってくる危険をなくす（部品は作るときに決めた版で固める）。

変えないもの: 使い方、`config.json`・`dict.json`・`logs/` の形、認識役の受け口（`/health`・`/transcribe`・`/dict/<名前>`）。
古いアプリと新しい認識役、新しいアプリと古い認識役の組み合わせでも動くようにする。

## 画面の作り方

- **設定・辞書・履歴の画面は、いまの HTML（`client/web/`）をそのまま使う。** Rust の本体が 127.0.0.1 で出して、いまと同じ窓
  （Edge のアプリ表示）で開く。日本語入力の心配がなく、画面の作り直しも要らない。守り（Host・Origin の確認、CSP）もそのまま移す。
  あとで、本体が自分の窓（WebView2）を持つ形に替えることはできる（画面の HTML は同じまま）。
- **録音中の表示（カプセル）とトレイは、Windows の仕組みを直接呼んで作る**（`windows` という部品）。
- Rune（anomalyco/rune）は使わない（2026-11-26 に調べた: 公開から 1 か月の試作段階で、Windows の日本語入力が「不完全」と
  自分で書いている。直してほしい点を送れない）。
- お手本: [OpenHinoVoice](https://github.com/iiinno/OpenHinoVoice)（MIT）。同じ種類の Windows アプリが Rust で動いている。
  画面の枠組みは使わず、`windows`（Win32 を直接）・`cpal`（録音）・`ureq`（通信）・egui（設定の窓だけ）で作ってある。
  キーの横取り（`hook.rs`）・貼り付け（`paste.rs`）・カプセル表示（`overlay.rs`）・手直しの監視（`uia.rs`）を、作るときに読む。
  コードを借りるときは、出どころとライセンスを書く。

## 全体の形

1 つの Cargo ワークスペースに、役割ごとの箱（crate）を置く。Mac と Windows の両方を OSS として配る前提で、OS 固有の仕組みは `onseiman-platform` に閉じ込める。

| 箱 | 中身 | いまの Python |
|---|---|---|
| `onseiman-core` | 設定・辞書・文の整え方・区切り・辞書の同期・履歴・AI で整える・更新の確認。OS に寄らない（区切り・辞書の形・WAV は済み） | `shared/*`・`client/config.py`・`logger.py`・`llm_refine.py`・`github_update.py`（約 2,500 行） |
| `onseiman-speech` | speech.cpp を動かす（Mac は Metal、Windows は CPU）。はじめは、いまと同じ「worker を 1 つ起動して JSON Lines でやりとり」（落ちても本体が巻き込まれない） | `shared/speechcpp_engine.py`・`speechcpp_setup.py`（約 370 行） |
| `onseiman-server` | 認識役（Mac・Windows 共通）。受け口はいまと同じ | `server/app.py`（約 360 行） |
| `onseiman-platform` | OS ごとの仕組みを共通の口で包む（`windows` / `macos`）。キーの横取り（Win32 Hook / macOS CGEventTap）・録音（`cpal`）・貼り付け・カーソルの場所・ほかの音を消す・手直しの監視・自動起動（Run / LaunchAgent）・API キーの鍵（DPAPI / Keychain） | `client/input_hook.py`・`recorder.py`・`paste.py`・`caret.py`・`audio_mute.py`・`uia_watcher.py`・`startup.py`・`secret_store.py`（約 2,700 行） |
| `onseiman-app` | アプリ本体（Mac・Windows 共通）: 流れのまとめ役・トレイ／メニューバー・カプセル表示・画面（いまの HTML を出す） | `client/main.py`・`pill.py`・`web_ui.py`・`web/*`（約 3,500 行 + 画面の JS 約 1,300 行） |

消すもの: `client/ui.py`・`settings_ui.py`・`theme.py`（使っていない古い Tk の窓、約 1,900 行）、`tools/install.ps1`・uv・専用の Python。

## ビルドと配り方（OSS 前提）

- **GitHub Actions（`.github/workflows/rust.yml`）で Windows（`windows-latest` / x64）と Mac（`macos-15` / Apple Silicon）の両方を自動でテスト・ビルドする。**
  - 開発者の手元の Mac に依存せず、だれでも再現できる形で実行ファイルと `SHA256SUMS.txt` を作る。
  - 公開（タグ `vX.Y.Z`）のときに、Windows 版・Mac 版のバイナリを GitHub Releases へ並べる。

## 進める順番

小さく出して、そのつど実機で確かめる。Python 版は、Rust 版が同じことをできると確かめるまで動かし続ける。

### 1. 認識役を Rust にする（Windows で確かめ済み。GitHub Actions で Mac 版も自動ビルド）

いちばん小さく、効果がすぐ出る（Mac の準備が「Python を入れて 10 分」から「置いて起動」になる）。

- `onseiman-speech` と `onseiman-server` を作る。守り（1.2.33 の RequestGuard・上限・Host の確認）もそのまま移す
- `tests/test_server_guard.py`・`test_dict_sync.py` の認識役の部分を、Rust 版に向けてそのまま流す（受け口が同じなので使い回せる）
- GitHub Actions で Mac（Apple Silicon）と Windows の実行ファイルを自動ビルドし、Mac の `server_mac.command`・`launchd` も Rust 版の実行ファイルを使う形に直す

終わりの条件: いまのアプリ（Python 版）から Mac / Windows の Rust 版認識役を使って、聞き取り・辞書の同期・iPhone のショートカットが前と同じに動く。

### 2. 中身の部品（`onseiman-core`）

- 文の整え方は細かい決まりが多い。Python のテスト（約 250 件）から「入れた文 → 出る文」の組を書き出して、Rust でも同じ答えになるかを機械的に比べる
- `config.json`・`dict.json`・`history.jsonl` は同じ形で読み書きする（Python 版と行き来できる）

終わりの条件: 書き出した組がすべて一致。

### 3. OS の土台（`onseiman-platform`）と、画面なしのアプリ（Windows → Mac）

- まず共通トレイトを切り、Windows 側（キーの横取り → 録音 → 聞き取り → 貼り付け）を通して 1 日ふつうに使えることを確認する
- 同じトレイトに合わせて macOS 側（`CGEventTap`・`cpal`・クリップボード＋Cmd+V・`LaunchAgent`・Keychain・アクセシビリティ権限の案内）を実装する

終わりの条件: Windows・Mac それぞれで、キーを押して話し、カーソルの場所へ文字が入る。

### 4. 画面（共通 HTML ＋ カプセル表示）

- 設定・辞書・履歴・使い方は、いまの HTML（`client/web/`）を Rust の本体から 127.0.0.1 で出す（Mac / Windows 共通）
- カプセル表示とトレイ／メニューバーを Windows・Mac それぞれで実装する

### 5. 配り方と切り替え

- GitHub Releases から Windows 版・Mac 版を `SHA256SUMS.txt` つきで配る
- Python 版（1.2.x）を使っている人の「今すぐ更新」から Rust 版に移れる橋渡しの版を 1 つ出す。設定・辞書・履歴はそのまま
- しばらくは Python 版に戻せるようにしておく（`backup`）
- 版の番号は 2.0.0 から

## 大きさの目安

日数は、実機で確かめる時間（人の手）で大きく変わるので、ここでは大きさだけを書く。

| 段階 | 大きさ | 主な危険 |
|---|---|---|
| 1. 認識役（Mac・Windows） | 小 | GitHub Actions での Mac ビルド・`server_mac.command` 切り替え |
| 2. 中身の部品（共通） | 中 | 文の整え方の細かい違い |
| 3. OS の土台（Windows / Mac） | 大 | 実機でしか出ない不具合（管理者の窓・権限・IME・マイク切替） |
| 4. 画面（共通 HTML ＋ カプセル） | 中 | カプセル表示がフォーカスを奪わないこと・画面の守りの移し替え |
| 5. 配り方と切り替え | 中 | 使っている人の移行でデータを壊さない・macOS の署名／公証 |

## やらないこと

- いちどに全部を書き直して入れ替えること（どこで壊れたか分からなくなる）
- 予備の聞き取りや、Python 版と Rust 版を行き来する複雑な仕組み（戻す手段は `backup` だけ）

## 作るための道具

- Rust は `D:\10_Tools\rust` の中だけに入れてある（パソコン全体の設定は変えていない。消すときはこのフォルダを消す）。
  使うときは、先に `RUSTUP_HOME=D:\10_Tools\rust\rustup`・`CARGO_HOME=D:\10_Tools\rust\cargo` を決めて、
  `D:\10_Tools\rust\cargo\bin` を PATH に足す。
- 確かめ: `cargo test --workspace`、`cargo clippy --workspace --all-targets`（どちらも注意なしで通す）
- Python 版と同じ答えかの突き合わせ: `crates/onseiman-core/tests/golden/make_golden.py` が Python 版を動かして正解を書き出し、
  Rust のテスト（`tests/golden.rs`）がそれと比べる。Python 版の決まりを変えたら、書き出し直す。

## 進み具合

### 2026-11-26: 段階 1（認識役）を Windows で確かめた

- できたもの: `onseiman-core`（区切り・略語・辞書の形・WAV）、`onseiman-speech`（worker とのやりとり・部品の用意）、
  `onseiman-server`（受け口と守り）。exe は 3.4MB、動いているときの記憶は 12MB（Python 版は 62MB）。
- 自動テスト 31 件（うち認識役を本当に起動するもの 11 件: 断り方・Host の確認・送信の上限・長い録音の区切り・
  worker の起動し直し・辞書の預かり）。
- Python 版との突き合わせ:
  - 区切り方: 6 通りの音で、切る場所が完全に同じ（足し算の順番まで numpy に合わせた）
  - 本物の聞き取り: 同じ録音を両方の認識役に送って、短い録音 15 本・80 秒の長い録音・優先語つき、すべて同じ文
  - いまのアプリ（Python）の部品から Rust の認識役を使って、サーバー探し・聞き取り・辞書の同期（2 台分）が通った
- Python 版と違うところ（わざと）:
  - 縮めてある WAV（ADPCM など）は受け付けない。読むのは PCM と小数の WAV だけ（OnseiMan のアプリと、M4A から直したものはこの形）
  - `/health` の `fingerprint` は `rust-<版>`。版が Python 版のアプリより新しいので、「認識役が古い」の知らせは出ない
- 残り: GitHub Actions（`.github/workflows/rust.yml`）で Windows x64・macOS arm64 の自動テスト＆ビルドを回し、Mac の認識役（`server_mac.command`・`launchd`）を Rust バイナリ版へ切り替える。

### 2026-11-26: 段階 2（`onseiman-core` の文の整え方・辞書）を移植して Python 版と突き合わせた

- できたもの（すべて追加ライブラリなし・純 Rust）:
  - `kana.rs`（かな相互変換・カタカナ語境界）
  - `fillers.rs`（言いよどみ除去・句読点整理）
  - `normalize.rs`（漢数字→数字・全角英数→半角・カタカナ略語 47 語・慣用表現保護）
  - `voice_commands.rs`（改行・記号コマンド・「今のなし」判定）
  - `dictionary.rs`（`CustomDictionary`：優先語 40 語上限・置き換え・定型文・読み抽出・文末「。」削り）
- Python 版との突き合わせ: `tests/golden/stage2.json` の全ケースで Python 版と完全一致（`cargo test --workspace` 38 件通過、`clippy -D warnings` 注意なし）。
- 段階 2（後半）も完了: `config.rs`（`config.json` 読み書き）・`history.rs`（`history.jsonl` と使用量集計）・`dict_sync.rs`（三方向マージ）・`llm_refine.rs`（OpenAI / Anthropic / Gemini / Ollama、リダイレクト禁止＋URL エンコード）。

### 2026-11-26: 段階 3（`onseiman-platform`）と段階 4（`onseiman-app`）の土台を構築

- `onseiman-platform`（Windows x64 / macOS arm64 共通クレート）:
  - `binding.rs`（キー・マウスサイドボタン設定パーサー）
  - `hotkey.rs`（Windows `WH_KEYBOARD_LL` / `WH_MOUSE_LL`・macOS `CGEventTap` グローバルフック＋Primary / Extra / AI / Cancel 対応）
  - `recorder.rs`（`cpal` による Windows WASAPI / macOS CoreAudio 共通マイク録音＋16kHz 線形リサンプリング＋リアルタイム音量レベル通知）
  - `audio_mute.rs`（録音中の他アプリ音量一時ミュート＋自動復元：Windows WASAPI `ISimpleAudioVolume` / macOS `osascript`）
  - `overlay.rs`（Windows 非アクティブ最前面カプセルオーバーレイ：録音中・AI 録音中・認識中＋音量バー）
  - `tray.rs`（Windows `Shell_NotifyIconW` システムトレイアイコン＋右クリックメニュー）
  - `clipboard.rs`・`paste.rs`（Win32 `CF_UNICODETEXT`＋`SendInput` Ctrl+V／macOS `pbcopy`＋`osascript` Cmd+V）
  - `secret_store.rs`（Windows DPAPI `dpapi:` 互換／macOS Keychain `keychain:`）
  - `startup.rs`（Windows `HKCU\...\Run`／macOS `LaunchAgent` plist）
- `onseiman-app`（Rust クライアント本体 `onseiman`）:
  - `transcriber.rs`（LAN `onseiman-server` 優先＋ローカル `onseiman-speech` フォールバック）
  - `discovery.rs`（LAN `/24` 並列 `/health` 探索）
  - `pipeline.rs`（言いよどみ除去・辞書置換・正規化・定型文・音声コマンド・文末「。」削り・LLM 仕上げ）
  - `web_ui.rs`（Axum ローカル Web UI サーバー：`client/web/` 配信＋Host/Origin/Sec-Fetch-Site/JSON 限定 POST/CSP 守り＋`/api/find_servers`・`/api/retranscribe`・`/api/retry` を含む各種 `/api/*`）
  - `main.rs`（トレイ・オーバーレイ・他アプリミュート・無音自動停止・ホットキー録音 → 認識 → 貼り付け → 履歴保存＋`--check` 診断モード）
- `server/setup_macos.sh`: Rust 版 `onseiman-server` バイナリがあれば Python / uv なしでそのまま `launchd` 登録するよう対応。
- CI（`.github/workflows/rust.yml`）: `windows-latest`（x64）と `macos-15`（arm64）の両方で `fmt` / `clippy -D warnings` / `test` / `onseiman-server` + `onseiman` リリースビルド＋`SHA256SUMS` 生成を自動化。
