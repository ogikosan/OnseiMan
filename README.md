# OnseiMan

話した言葉を、カーソルのある場所に文字で入れる Windows 用の音声入力アプリです。
声を文字にする処理は、このパソコンの中だけで行います（音声をインターネットに送りません）。

- 右Alt を押して話し、もう一度押すと、カーソルの場所に文字が入ります
- 長く話しても、話しながら区切って文字にするので消えません
- 「えーと」などの言いよどみを消し、数字や略語を読みやすくします（五千九十 → 5090）
- 辞書（よく間違える言葉）と定型文（合言葉で決まった文を入れる）
- 家にある別のパソコン（Mac / Windows）に、声を文字にする役を任せることもできます

認識には [Fun-ASR Nano](https://github.com/FunAudioLLM/Fun-ASR)（[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 版）を使います。

## 入れ方

Windows 10 / 11（64ビット）と Python 3.11 が必要です。

1. 右上の「Code」→「Download ZIP」でダウンロードして、好きな場所に展開する
2. `install_client.bat` をダブルクリック
3. `start_client.bat` をダブルクリック（初回だけ、認識用のデータ 約840MB を自動でダウンロードします）

くわしくは [はじめに.md](packaging/friend/はじめに.md) を見てください。
AI に入れてもらう場合は、[AGENTS.md（入れ方）](packaging/friend/AGENTS.md) を渡して「これを読んで入れて」と頼めます。

## 新しい版にする

OnseiMan を終了してから `update.bat` をダブルクリックします。
プログラムだけが新しくなり、設定・辞書・履歴はそのまま残ります（前の版は `backup\` に残ります）。

## 開発

- 作業の決まり: [AGENTS.md](AGENTS.md)、詳細仕様: [docs/src/spec.md](docs/src/spec.md)、概要: [docs/index.html](docs/index.html)
- テスト: `.venv\Scripts\python.exe -m unittest discover -s tests`
- 友達に zip で渡す: `python packaging\make_friend_package.py`

| 場所 | 中身 |
|---|---|
| `client/` | Windows のアプリ（録音・貼り付け・表示・設定画面 `client/web/`） |
| `shared/` | 認識エンジン・辞書・文の整え方など、アプリとサーバーで共通の部品 |
| `server/` | 認識サーバー（Mac: `setup_macos.sh` / Windows: `install_server_windows.bat`） |
| `tools/` | 更新（`update.ps1`） |
| `tests/` | 自動テスト |
| `packaging/` | 友達に渡す説明と zip の作り方。`exe-installer/` は今は使っていない |

`config.json`・`dict.json`・`logs/`（設定・辞書・履歴と録音）は利用者のデータなので、リポジトリには入れません。

## セキュリティ

守り方と、問題を見つけたときの知らせ方は [SECURITY.md](SECURITY.md) にあります。
認識サーバー（任意）は合言葉なしで使えるので、信頼できるネットワーク（家の Wi-Fi・Tailscale）の中でだけ動かしてください。

## ライセンス

[Apache License 2.0](LICENSE)　Copyright 2026 ogikosan

認識モデル（Fun-ASR Nano）と、使っているライブラリ（sherpa-onnx など）は、それぞれのライセンスに従います。
