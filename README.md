# OnseiMan

話した言葉を、カーソルのある場所に文字で入れる Windows 用の音声入力アプリです。
声を文字にする処理は、このパソコンの中（または家にある別のパソコン）だけで行います。音声をインターネットに送りません
（「AI で整える」をオンにしたときだけ、聞き取った文字を選んだ AI の会社に送ります）。

- 右Alt を押して話し、もう一度押すと、カーソルの場所に文字が入ります（キーは変えられます。2 つ目のキーやマウスのボタンも使えます）
- 聞き取りには、聞き間違いの少ない日本語の AI **Haas-ja** を使います。GPU は要らず、ふつうのパソコンの CPU で 1 回 1 秒以内です
- 「。」「、」が付き、「えーと」などの言いよどみを消します（数字や略語を読みやすくする変換も、設定でオンにできます）
- 辞書（よく間違える言葉）と定型文（合言葉で決まった文を入れる）。何台かのパソコンで自動で合わせられます
- 使いたい人は、AI（GPT・Claude・Gemini・自分で動かす AI）で文章を整えられます。整えるときだけ、文字（音声ではありません）を選んだ会社に送ります
- 家にある別のパソコン（Mac / Windows）に、声を文字にする役を任せることもできます
- 新しい版が出ると画面に知らせが出て、「今すぐ更新」で入れ替えられます（設定・辞書・履歴はそのまま）

## 入れ方

右の「Releases」から、お使いの OS に合うバイナリを取ってきます。

| 取ってくるもの | 使い方 |
|---|---|
| `onseiman-windows-x64.exe`（Windows クライアント） | 任意のフォルダに `onseiman.exe` として配置し、`client/web/` と一緒にして起動 |
| `onseiman-server-windows-x64.exe`（Windows 認識サーバー） | 別 PC で音声認識を担当させたい場合に起動（`server_windows.bat`） |
| `onseiman-server-macos-arm64`（Mac 認識サーバー） | Apple Silicon Mac で `server_mac.command` から自動起動登録 |

- Windows 10 / 11（64 ビット）・macOS（Apple Silicon）で単体バイナリとして動きます。Python などのランタイムは不要です
- はじめて起動したとき、聞き取りの部品（約 0.7GB）を自動でダウンロードします（SHA-256 検証付き）
- コード署名は行っていないため、初回実行時に Windows SmartScreen / macOS Gatekeeper の確認が出た場合は許可して進めてください

## 使い方

1. 文字を入れたい場所をクリック
2. **右Alt** を押して話す
3. もう一度 **右Alt** → 文字が入ります

うまく録れているかは、設定の「マイク」の「試す（5 秒）」で確かめられます。
聞き間違える言葉は、辞書の「言葉」に登録します。カタカナで書いた言い方を横に足すと、聞き取るときのヒント（読み）にもなります
（例: くっきり版 ← クッキリバン）。

AI で整えたいときは、設定の「AI で整える」で頼む AI を選び、API キーを貼り付けて「試す」を押します。

## 別のパソコンに任せる（なくても大丈夫）

声を文字にする役を、家の別のパソコンに任せられます。アプリの「設定」→「声を文字にする場所」で「別のパソコン」を選ぶと、
家のネットワークと Tailscale から自動で探します。任せる側のパソコンの準備は、そのパソコンの前で、そのパソコンを使っている人がします
（こちらから勝手に入れることはしません）。

| 任せる側 | 準備 |
|---|---|
| Mac（Apple のチップ） | `onseiman-server-macos-arm64` を `onseiman-server` として配置し `server_mac.command` を実行。Apple の GPU（Metal）で 1 回 0.1 秒ほど。Mac 起動時に自動起動します（やめるときは `server_mac_remove.command`） |
| Windows | `onseiman-server-windows-x64.exe` を配置して `server_windows.bat` を実行 |

- Mac で「開けません」と出たら、`server_mac.command` を右クリック→「開く」。それでも開けないときは「システム設定」→「プライバシーとセキュリティ」の「このまま開く」
- 認識役の Mac には、iPhone のショートカットから声を送って、文字にしてもらうこともできます（[はじめに.md（Mac）](packaging/mac/はじめに.md)）

## 開発（完全 Rust ワークスペース）

- 詳細仕様: [docs/src/spec.md](docs/src/spec.md)
- 検証: `cargo fmt --all -- --check && cargo clippy --workspace --all-targets -- -D warnings && cargo test --workspace`
- ビルド: `cargo build --release -p onseiman-app -p onseiman-server`

| 場所 | 中身 |
|---|---|
| `crates/onseiman-core` | 設定・辞書・言いよどみ除去・表記正規化・音声コマンド・区切り・履歴・学習候補・辞書同期・AI 仕上げ |
| `crates/onseiman-speech` | `speech.cpp` ワーカー連携・モデル自動取得（SHA-256 検証付き） |
| `crates/onseiman-server` | 認識サーバー（Windows x64 / macOS arm64） |
| `crates/onseiman-platform` | OS 固有土台（グローバルフック・録音・ミュート・トレイ・オーバーレイ・UIA 自動学習・貼付・DPAPI / Keychain・自動起動） |
| `crates/onseiman-app` | クライアント本体（`onseiman`、`client/web/` を `127.0.0.1` で配信） |
| `client/web/` | 管理画面 HTML / CSS / JS |
| `server/` | Mac サーバー自動起動登録スクリプト（`setup_macos.sh`） |
| `docs/` | 仕様書・紹介ページ |

`config.json`・`dict.json`・`logs/`（設定・辞書・履歴と録音）は利用者のデータなので、リポジトリには入れません。

## セキュリティ

守り方と、問題を見つけたときの知らせ方は [SECURITY.md](SECURITY.md) にあります。
認識役（任意）は合言葉なしで使えるので、信頼できるネットワーク（家の Wi-Fi・Tailscale）の中でだけ動かしてください。

## スペシャルサンクス

OnseiMan は、次の方々が公開しているものに支えられています。ありがとうございます。

- [Haas-ja-v1.0](https://huggingface.co/third-intelligence/Haas-ja-v1.0)（Third Intelligence、Apache-2.0）: 日本語の聞き取りの AI
- [Haas-ja-v1.0-GGUF](https://huggingface.co/sakasegawa/Haas-ja-v1.0-GGUF)（sakasegawa）: Haas-ja を軽くした版。OnseiMan はこの中の `Q8_0` を使っています
- [speech.cpp](https://github.com/nyosegawa/speech.cpp)（MIT）: Haas-ja を Python も GPU もなしで動かす道具
- [OpenHinoVoice](https://github.com/iiinno/OpenHinoVoice): マイクを試す機能や、辞書の読みをヒントにする仕組みの参考にさせてもらいました

## ライセンス

[Apache License 2.0](LICENSE)　Copyright 2026 ogikosan

聞き取りの AI と、使っているライブラリは、それぞれのライセンスに従います。
