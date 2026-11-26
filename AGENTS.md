# プロジェクト固有の作業指示（OnseiMan）

親の AGENTS.md を継承する。

## 実行と検証
- 起動（Windows クライアント）：`start_client.bat`（= `.venv\Scripts\pythonw.exe client\main.py`）。2 重起動は防止されるので、変更を試すときは動いている OnseiMan をトレイの「終了」で止めてから起動する。
- コード変更時の必須確認：`.venv\Scripts\python.exe -m unittest discover -s tests`（全件 OK）。
- 認識まわりを変えたとき：付属の音声で実際に認識させて確かめる（`models/.../test_wavs/`。長い発話は複数をつないで試す）。
- 画面（`client/web/`）を変えたとき：`node -e "new Function(require('fs').readFileSync('client/web/app.js','utf8'))"` で文法確認し、ヘッドレス Edge で撮影して見た目を確かめる。動いている OnseiMan と同じポートを取り合うので、試験用の Web サーバーは `_preferred_port = 0` で起動する。
- 配る zip（Windows 用・Mac 用の 2 つ）：`python packaging\make_friend_package.py`（個人の情報が混ざっていたら作らない。確認する言葉は `packaging/private-patterns.txt`、公開しない）。
- 公開：GitHub `ogikosan/OnseiMan`（公開リポジトリ）。利用者はアプリの「今すぐ更新」か `update.bat` で `main` の最新版に入れ替える。
  新しい版の公開は `python packagingelease.py`（版を上げて変更点.md に節を書いてから。テスト → zip 2 つ → インストーラー →
  main を進める → タグ → GitHub に公開、を必ず全部やる。手で一部だけ出さない）。
  新しい版を出す手順：`client/version.py` を上げる → `packaging/friend/変更点.md` に追記 → テスト全件 OK →
  `develop` にコミット → `main` を早送りで合わせる → タグ `vX.Y.Z` → `git push origin main develop refs/tags/vX.Y.Z`。
  公開リポジトリなので、コミット前に `packaging/private-patterns.txt` の言葉が混ざっていないか必ず調べる。
- バッチ（`*.bat`）・PowerShell（`*.ps1`）を変えたとき：
  - `*.bat` は英数字だけ・CRLF。日本語の表示や処理は `tools\*.ps1`（BOM つき UTF-8・CRLF）に書く。`tests/test_scripts.py` が形を確かめる。
  - パスを含む編集を、シェルのヒアドキュメント経由の Python で行わない（`\a` `\b` `\r` が制御文字に化けて、2026-11-23 に利用者の環境で失敗した）。
    Write / Edit ツールか、ファイルに書いたスクリプトで編集する。
  - 編集したあと `python packaging\normalize_scripts.py` で形をそろえる。
  - 形のテストだけで済ませず、まっさらなフォルダに展開して実際に実行する（Python が PATH に無い状態で `install_client.bat`）。
    古い版からの `update.bat` も、GitHub のタグの ZIP から実際に通す。
  - `requirements.txt` は英数字だけ（日本語の Windows の pip は cp932 として読む）。
- Rust 版（作りかけ。`Cargo.toml`・`crates/`、ブランチ `feature/rust`、計画と進み具合は `docs/src/rust-migration-plan.md`）を変えたとき：
  `cargo test --workspace` と `cargo clippy --workspace --all-targets`（注意なしで通す）。Rust は `D:\10_Tools\rust` に入っている
  （`RUSTUP_HOME=D:\10_Tools\rust\rustup`・`CARGO_HOME=D:\10_Tools\rust\cargo` を決め、`D:\10_Tools\rust\cargo\bin` を PATH に足して使う）。
  Python 版と同じ決まりのもの（区切り・辞書の形など）を変えたら、`crates/onseiman-core/tests/golden/make_golden.py` で正解を書き出し直す。
- 文書のみ：リンク・内容・実際の仕様との整合性。

## 該当時に読む資料
- 機能変更：`docs/src/spec.md`
- 人間向けの説明が変わる場合：`docs/index.html`、アプリ内の「使い方」（`client/web/index.html` の `view-help`）、`packaging/friend/はじめに.md`
- サーバー（Mac / Windows）：`server/`。入口は直下の `server_mac.command`（中身は `server/setup_macos.sh`）・`server_mac_remove.command`・`server_windows.bat`

## 固有の制約
- 利用者のデータ：`config.json`・`dict.json`・`logs/`（履歴・録音・学習候補を含む）は消さない・上書きしない。辞書を整理するときは `logs/` にバックアップを取ってから。
- 聞き取りの部品 `models/haas-ja`（0.66GB）・`tools/speech` と `.venv` は作り直しに時間がかかる。消さない。
- 聞き取り（Haas-ja）は 1 回に 1 つの発話を渡す作り（長い録音では文がまるごと抜けることがある）。区切りの長さを変えるときは、実音声で欠けないか必ず確かめる（`docs/src/spec.md` の「聞き取り」）。
- キーボードの監視（`client/input_hook.py`）を変えるときは、落ちてもキーボードが効かなくならないこと（見張り `client/watchdog.py`）を壊さない。
