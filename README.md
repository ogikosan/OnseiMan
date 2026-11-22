# OnseiMan

話した言葉を、カーソルのある場所に文字で入れる Windows 用の音声入力アプリです。
声を文字にする処理は、このパソコンの中だけで行います（音声をインターネットに送りません）。

- 右Alt を押して話し、もう一度押すと、カーソルの場所に文字が入ります
- 長く話しても、話しながら区切って文字にするので消えません
- 「えーと」などの言いよどみを消し、数字や略語を読みやすくします（五千九十 → 5090）
- 辞書（よく間違える言葉）と定型文（合言葉で決まった文を入れる）
- 家にある別のパソコン（Mac / Windows）に、声を文字にする役を任せることもできます

認識には [Fun-ASR Nano](https://github.com/FunAudioLLM/Fun-ASR)（[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) 版）を使います。

## どれを取ってくればいい？

右の「Releases」から、使うパソコンに合わせて 1 つ取ってきて展開します。

| 取ってくるもの | 中身 |
|---|---|
| `OnseiMan-<版>-Windows.zip` | Windows で使うアプリ。別の Windows を認識役にするときもこれ |
| `OnseiMan-<版>-Mac.zip` | Mac を認識役（声を文字にする役）にするものだけ。Mac で文字を入れるアプリではありません |

「Code」→「Download ZIP」で全部入りを取ってきても、同じように使えます。

## ダブルクリックするもの

Python などを自分で入れる必要はありません。必要なものは、そのフォルダの中に自動で用意します（パソコン全体には何も入れません）。

| やりたいこと | ダブルクリックするもの |
|---|---|
| Windows で使う（はじめて） | `install_client.bat` → `start_client.bat` |
| Windows で使う（2 回目から） | `start_client.bat` |
| 新しい版にする（Windows） | OnseiMan を終了してから `update.bat` |
| 別の Windows を認識役にする | その Windows で `server_windows.bat`（使うあいだは窓を開いたまま） |
| Mac を認識役にする | その Mac で `server_mac.command`（一度済めば、Mac を起動するたびに自動で動く） |
| Mac の認識役をやめる | `server_mac_remove.command` |

- Windows 10 / 11（64ビット）で動きます。初回だけ、認識用のデータ（約840MB）を自動でダウンロードします
- 認識役は、なくても使えます。ふだんのパソコンが遅いときに、家の別のパソコンに任せると速くなります。
  認識役のパソコンの準備は、そのパソコンの前で、そのパソコンを使っている人がします（こちらから勝手に入れることはしません）
- Mac で「開けません」と出たら、`server_mac.command` を右クリック→「開く」。
  それでも開けないときは「システム設定」→「プライバシーとセキュリティ」の「このまま開く」
- 認識役の Mac には、iPhone のショートカットから声を送って、文字にしてもらうこともできます（[はじめに.md（Mac）](packaging/mac/はじめに.md)）
- 新しい版にしても、設定・辞書・履歴はそのまま残ります（前の版は `backup\` に残ります）

くわしくは [はじめに.md（Windows）](packaging/friend/はじめに.md)・[はじめに.md（Mac）](packaging/mac/はじめに.md) を見てください。
AI に入れてもらう場合は、[AGENTS.md（入れ方）](packaging/friend/AGENTS.md) を渡して「これを読んで入れて」と頼めます。
何が変わったかは [変更点.md](packaging/friend/変更点.md) にあります。

## 開発

- 作業の決まり: [AGENTS.md](AGENTS.md)、詳細仕様: [docs/src/spec.md](docs/src/spec.md)、概要: [docs/index.html](docs/index.html)
- テスト: `.venv\Scripts\python.exe -m unittest discover -s tests`
- 配る zip（Windows 用・Mac 用）を作る: `python packaging\make_friend_package.py`（`build_dist\` にできる）

| 場所 | 中身 |
|---|---|
| `client/` | Windows のアプリ（録音・貼り付け・表示・設定画面 `client/web/`） |
| `shared/` | 認識エンジン・辞書・文の整え方など、アプリとサーバーで共通の部品 |
| `server/` | 認識サーバー（Mac の準備: `setup_macos.sh` / Windows: `start_server.bat`。直下のダブルクリック用から呼ばれる） |
| `tools/` | 準備（`install.ps1`）と更新（`update.ps1`） |
| `tests/` | 自動テスト |
| `packaging/` | 配る説明（`friend/` は Windows 用、`mac/` は Mac 用）と zip の作り方。`exe-installer/` は今は使っていない |

`config.json`・`dict.json`・`logs/`（設定・辞書・履歴と録音）は利用者のデータなので、リポジトリには入れません。

## セキュリティ

守り方と、問題を見つけたときの知らせ方は [SECURITY.md](SECURITY.md) にあります。
認識サーバー（任意）は合言葉なしで使えるので、信頼できるネットワーク（家の Wi-Fi・Tailscale）の中でだけ動かしてください。

## ライセンス

[Apache License 2.0](LICENSE)　Copyright 2026 ogikosan

認識モデル（Fun-ASR Nano）と、使っているライブラリ（sherpa-onnx など）は、それぞれのライセンスに従います。
