# OnseiMan セットアップ手順（AI エージェント向け）

利用者に代わって OnseiMan を入れるための手順。利用者向けの説明は `はじめに.md`。

## 構成

- **クライアント（Windows・必須）**: `client/main.py`。録音・貼り付け・画面（タスクトレイ常駐、右Alt で録音）。
  既定では **このパソコンで認識** する（`config.json` の `recognition_place: "local"`）。モデルは初回起動時に自動取得。
- **認識サーバー（任意）**: `server/app.py`。別のパソコン（Mac / Windows）で動かすと速くなる。
  FastAPI で `:8765` に `GET /health` と `POST /transcribe` を公開。モデルは Fun-ASR Nano（sherpa-onnx, int8, 約 840MB）。
- クライアントとサーバーは同じ LAN か Tailscale で通信する。クライアントの「サーバーをさがす」は
  Tailscale の相手と同じ /24 のホストの `:8765/health` を調べて見つける。

## 1. クライアント（Windows・必須）

1. Python の用意は不要。`install_client.bat`（中身は `tools\install.ps1`）が、uv を使って専用の Python 3.11 を
   `tools\python` に用意し、`.venv` を作る（パソコン全体には何も入れない）。
2. リポジトリ直下で `install_client.bat` を実行（`.venv` に `client/requirements.txt` を入れ、最後に読み込みを確かめる）。
   「準備できました」と出れば成功。失敗したら画面の内容を読む（インターネット接続が必要）。
3. `start_client.bat` で起動（`pythonw client\main.py`）。初回に `config.json` と `dict.json` を作り、
   このパソコンで認識するためのモデル（約 840MB）を、進み具合の画面を出してダウンロードする。
4. 動作確認: 何かの入力欄をクリック → 右Alt を押して話す → もう一度右Alt → 文字が入る。ログは `logs/onseiman.log`。

ここまでで使える。サーバーは、利用者が「速くしたい」「別のパソコンで動かしたい」と言ったときだけ入れる。

## 2. 認識サーバー（任意）

利用者の許可を得たパソコンにだけ入れる。

- **Mac**: Python 3.11 を用意（無ければ `brew install python@3.11`）→ リポジトリ直下で `bash server/setup_macos.sh`。
  `.venv_server` を作って依存とモデルを入れ、launchd（`~/Library/LaunchAgents/com.onseiman.asr.plist`）で常駐登録する。
  `--with-ai` を付けると Ollama + qwen2.5:7b（約 4.7GB）も入れる（「AI で文章を整える」を使うときだけ）。
- **Windows**: `server\install_server_windows.bat`（Python 不要。`.venv_server` を作り、モデルも取得）→
  `server\start_server.bat`（`0.0.0.0:8765`）。
  ファイアウォールで Python の受信を許可する。
- 確認: そのパソコンで `curl http://127.0.0.1:8765/health` が `{"status":"ok",...}` を返すこと。

## 3. クライアントをサーバーにつなぐ

設定画面 →「声を文字にする場所」→「別のパソコン」を選ぶと、自動で探して、見つかったものを使う設定にして保存する。
同じパソコンが Tailscale と家のネットワークの両方で見つかったら 1 つ（Tailscale 側）にまとめる。
手で書く場合は `config.json` の `recognition_place` を `"server"`、`server_urls` を `["http://<IP>:8765"]` にする。

## 新しい版に入れ替える

1. 動いている OnseiMan を止める（トレイの「終了」。止まらなければ `client\main.py` を動かしている pythonw を終了）。
2. 新しい zip の `OnseiMan/` の中身を、今のフォルダに上書きコピーする。
   zip には `config.json` / `dict.json` / `logs/` / `models/` / `.venv` が入っていないので、利用者のデータは残る。
3. `install_client.bat` を再実行（`client/requirements.txt` の追加分を入れる）。
4. `start_client.bat` で起動し、トレイのメニューの先頭に新しい版の番号が出ることを確かめる。
5. サーバー（Mac）側は、`server/` か `shared/` が変わったときだけ同じように上書きし、
   `launchctl kickstart -k gui/$(id -u)/com.onseiman.asr` で再起動する。

## 注意

- `config.json` / `dict.json` / `logs/` は利用者のデータ。上書き・削除しない。
- 利用者の許可なく、ほかのパソコンにサーバーを入れない。
