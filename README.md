# OnseiMan

話した言葉を、カーソルのある場所に文字で入れる Windows 用の音声入力アプリです。
声を文字にする処理は、このパソコンの中（または家にある別のパソコン）だけで行います。音声をインターネットに送りません
（「AI で整える」をオンにしたときだけ、聞き取った文字を選んだ AI の会社に送ります）。

- 右Alt を押して話し、もう一度押すと、カーソルの場所に文字が入ります（キーは変えられます。2 つ目のキーやマウスのボタンも使えます）
- 聞き取りには、聞き間違いの少ない日本語の AI **Haas-ja** を使います。GPU は要らず、ふつうのパソコンの CPU で 1 回 1 秒以内です
- 「。」「、」が付き、「えーと」などの言いよどみを消し、数字や略語を読みやすくします（五千九十 → 5090、cpu → CPU）
- 辞書（よく間違える言葉）と定型文（合言葉で決まった文を入れる）。何台かのパソコンで自動で合わせられます
- 使いたい人は、AI（GPT・Claude・Gemini・自分で動かす AI）で文章を整えられます。整えるときだけ、文字（音声ではありません）を選んだ会社に送ります
- 家にある別のパソコン（Mac / Windows）に、声を文字にする役を任せることもできます
- 新しい版が出ると画面に知らせが出て、「今すぐ更新」で入れ替えられます（設定・辞書・履歴はそのまま）。
  前の版の、もう使わないデータは `cleanup_old.bat`（Mac は `cleanup_old_mac.command`）で消せます

## 入れ方

右の「Releases」から、どちらかを取ってきます。

| 取ってくるもの | 入れ方 |
|---|---|
| `OnseiMan-Setup-<版>.exe`（いちばん簡単） | ダブルクリックして「インストール」。スタートメニューに「OnseiMan」ができます。消すときは Windows の「アプリ」から |
| `OnseiMan-<版>-Windows.zip` | 好きな場所に展開して、`start_client.bat` をダブルクリック |

- Windows 10 / 11（64 ビット）で動きます。Python などを自分で入れる必要はありません。必要なものは、そのフォルダの中に自動で用意します（パソコン全体には何も入れません）
- はじめて起動したとき、聞き取りの部品（約 0.7GB）をダウンロードします（数分。途中で止まっても、次は続きから）
- インストーラーは、まだデジタル署名をしていません。「PC を保護しました」と出たら「詳細情報」→「実行」で進めてください

## 使い方

1. 文字を入れたい場所をクリック
2. **右Alt** を押して話す
3. もう一度 **右Alt** → 文字が入ります

うまく録れているかは、設定の「マイク」の「試す（5 秒）」で確かめられます。
聞き間違える言葉は、辞書の「言葉」に登録します。カタカナで書いた言い方を横に足すと、聞き取るときのヒント（読み）にもなります
（例: くっきり版 ← クッキリバン）。

AI で整えたいときは、設定の「AI で整える」で頼む AI を選び、API キーを貼り付けて「試す」を押します。

くわしくは [はじめに.md（Windows）](packaging/friend/はじめに.md) を見てください。
AI に入れてもらう場合は、[AGENTS.md（入れ方）](packaging/friend/AGENTS.md) を渡して「これを読んで入れて」と頼めます。
何が変わったかは [変更点.md](packaging/friend/変更点.md) にあります。

## 別のパソコンに任せる（なくても大丈夫）

声を文字にする役を、家の別のパソコンに任せられます。アプリの「設定」→「声を文字にする場所」で「別のパソコン」を選ぶと、
家のネットワークと Tailscale から自動で探します。任せる側のパソコンの準備は、そのパソコンの前で、そのパソコンを使っている人がします
（こちらから勝手に入れることはしません）。

| 任せる側 | 準備 |
|---|---|
| Mac（Apple のチップ） | `OnseiMan-<版>-Mac.zip` を展開して `server_mac.command` をダブルクリック。Apple の GPU で、1 回 0.1 秒ほど。Mac を起動するたびに自動で動きます（やめるときは `server_mac_remove.command`） |
| Windows | Windows 用の zip を展開して `server_windows.bat` をダブルクリック |

- Mac で「開けません」と出たら、`server_mac.command` を右クリック→「開く」。それでも開けないときは「システム設定」→「プライバシーとセキュリティ」の「このまま開く」
- 認識役の Mac には、iPhone のショートカットから声を送って、文字にしてもらうこともできます（[はじめに.md（Mac）](packaging/mac/はじめに.md)）

## 開発

- 作業の決まり: [AGENTS.md](AGENTS.md)、詳細仕様: [docs/src/spec.md](docs/src/spec.md)
- テスト: `.venv\Scripts\python.exe -m unittest discover -s tests`
- 配る zip（Windows 用・Mac 用）を作る: `python packaging\make_friend_package.py`（`build_dist\` にできる）
- インストーラーを作る: `python packaging\installer\build_installer.py`（Inno Setup 6 が要る）
- 公開する: `python packaging\release.py`（テスト・zip・インストーラー・タグ・GitHub への公開を全部）

| 場所 | 中身 |
|---|---|
| `client/` | Windows のアプリ（録音・貼り付け・表示・設定画面 `client/web/`） |
| `shared/` | 聞き取り（`speechcpp_engine.py`・`speechcpp_setup.py`）・辞書・文の整え方など、アプリと認識役で共通の部品 |
| `server/` | 認識役（Mac の準備: `setup_macos.sh` / Windows: `start_server.bat`。直下のダブルクリック用から呼ばれる） |
| `tools/` | 準備（`install.ps1`）と更新（`update.ps1`）など |
| `tests/` | 自動テスト |
| `packaging/` | 配る説明（`friend/` は Windows 用、`mac/` は Mac 用）と、zip・インストーラー（`installer/`）・公開（`release.py`）の作り方 |

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
