# OnseiMan 詳細仕様（AI 向け）

人間向けの概要は `docs/index.html`。ここは実装の要点と、変えるときに守る条件。

## 全体の流れ

1. 録音キー（既定 右Alt、`hotkey_mode=toggle`）を押す → 録音開始（`client/recorder.py`）。
   - マイクは開いたまま待機し、押す 0.4 秒前から録る（`keep_mic_open`, `preroll_sec`）。
   - 録音中はほかのアプリの音を消す（`client/audio_mute.py`、`mute_other_audio`）。落ちても次の起動で戻す。
2. 話しながら、切れ目（0.6 秒の無音）ごとに区切って裏で認識する（`shared/segmenter.py`, `client/live_session.py`）。
3. もう一度押す（またはカプセルの左の印）→ 残りを認識してつなぐ。黙ったら自動で確定する設定（`auto_stop_on_silence`）もあるが、初期設定はオフ。
4. 文を整える（`client/main.py: _format_recognized_text`）：言いよどみを消す → 辞書の置き換え → 標準の変換（漢数字・略語）→ 声のコマンド（改行・記号）→ 文末の「。」。言い直し（「あ、違う」等）があれば整理。
5. 入力先のウィンドウに Ctrl+V で貼る（`client/paste.py`）。「今のなし」で取り消し。

## 認識する場所

- `recognition_place`: `"local"`（このパソコン）/ `"server"`（別のパソコン）。空なら `server_urls` があれば server。
- サーバー：`server/app.py`（`GET /health`, `POST /transcribe`、既定ポート 8765）。
- クライアントは最後に成功したサーバーを先に使い、失敗したものは 60 秒後回し（`client/transcriber.py`）。
- 「サーバーをさがす」：Tailscale の相手と同じ /24 のホストの `:8765/health` を調べる（`client/discovery.py`）。
  同じパソコンが Tailscale と家のネットワークの両方で見つかったら 1 つにまとめ、Tailscale のアドレスを残す
  （判定: サーバーが返す `id`、または Tailscale の `CurAddr`/`Addrs` にある相手の家のアドレス）。
  両方を登録すると、外出先で家のアドレスの待ちが毎回かかるなど不安定になる。
- 使う順番: `preferred_server`（画面の「いつも使う」）→ 最後に成功したサーバー → ほかの登録順。
  失敗したサーバーは 60 秒後回し。初めて探したときは返事がいちばん速いものを「いつも使う」にする。

## 量の上限（重要）

Fun-ASR Nano は一度に扱える量（音声 + 辞書のヒント + 出力）に上限があり、超えると文が欠けるか空になる。実測：

| 一続きの長さ | ヒント 20 語 | 28 語 | 36 語 | 48 語 |
|---|---|---|---|---|
| 12 秒 | OK | OK | OK | ほぼ空 |
| 15 秒 | OK | OK | 欠ける | 空 |
| 18 秒 | OK | 欠ける | 空 | 空 |

対策（変えるときは実音声で再確認）：
- 区切りの上限 = `min(18, 25 - 0.4 × ヒント数)` 秒、最短 6 秒（`client/main.py: _max_segment_sec`）。
- ヒントは新しく登録した順に 40 語まで。ひらがなだけ・1 文字の言葉は渡さない（`shared/dictionary.py: active_hotwords`）。
- 区切りの結果が「5 秒以上で 1 秒 1 文字未満」なら、ヒントなしで認識し直して多いほうを使う（`_recognize_safely`）。

## 辞書（`dict.json`）

- 画面では「正しい言葉」ごとに 1 行。正しい言葉は自動で認識のヒントになる。聞き間違えた言い方（`replacements`）は任意。
- かなだけの言い方は、カタカナの一続きの語として出たときだけ置き換える。
- 3 文字以下の言い方は、画面で登録するときに確認を出す。自動学習でも自動登録しない（確認カードを出す）。
- 自動学習（`shared/correction_learner.py`）は、両方に同じ助詞が付いているだけなら外して覚える。

## 定型文（`dict.json` の `snippets`）

- 合言葉 → 入れる文。整えたあとの発話「全体」が合言葉と一致したときだけ展開する（文の途中では展開しない。前後の句読点は無視、かなはカタカナと同一視）。
- 展開した文は登録したとおりに入れる（文末の「。」削り・言い直しの整理・AI 整形にかけない）。履歴の認識元に「+定型文」が付く。
- 個人の情報（メールアドレス・住所）が入りうるので、友達用の一式には `dict.json` を入れない。

## 履歴の録音

- `GET /api/audio/<名前>.wav`（`logs/audio/` の wav だけ。Range 対応）。画面は音量を聞きやすい大きさに上げて再生する。
- 履歴の `has_audio` が真の行にだけ ▶ と「再試行」を出す。

## 表示

- 録音中・認識中・入力した・注意は、ひとつのカプセル（`client/pill.py`、レイヤードウィンドウ）で出す。フォーカスを奪わない。ドラッグで場所を固定（`overlay_position`）。
- 設定・辞書・履歴・使い方は Web 画面（`client/web/`、`client/web_ui.py`、既定ポート 18765）。

## 準備と更新（Windows）

- `install_client.bat` → `tools\install.ps1 -Target client`。uv（版と SHA-256 を固定）を `tools\bin` に取得し、
  専用の Python 3.11 を `tools\python` に置いて `.venv` を作る。Python が入っていなくてよい。最後に全部品の読み込みを確かめる。
  uv を取得できないときは、入っている Python 3.11 以上で代用する。サーバーは `-Target server`（`.venv_server`、モデルも取得）。
- `update.bat` → `tools\update.ps1`。GitHub の `main` の zip で上書きし、`install.ps1` で部品を確かめる。
  `config.json`・`dict.json`・`logs`・`models`・`.venv` には触らない。前の版は `backup\` に残す。

## データの場所

- ソースで動かすとき：プロジェクト直下（`config.json`, `dict.json`, `logs/`, `models/`）。
- exe 版：`%LOCALAPPDATA%\OnseiMan`（`client/paths.py`）。
- 録音は `logs/audio/` に新しい 100 件まで（履歴の「もう一度文字にする」「再試行」用）。

## 守り（変えるときは `tests/test_security.py`・`tests/test_server_guard.py` を通す）

- 設定画面（`client/web_ui.py`）: 127.0.0.1 のみ。Host が 127.0.0.1/localhost 以外は 403（DNS リバインディング）。
  POST は Origin が自分・Content-Type が JSON のものだけ（CSRF）。Sec-Fetch-Site が same-origin / none 以外は 403（ほかのサイトからの読み込み・埋め込み）。
  全応答に CSP（`script-src 'self'`・`frame-ancestors 'none'`・`connect-src 'self'`）・X-Frame-Options・nosniff・CORP。
  画面の HTML に直接スクリプトを書かない（`theme.js`・`app.js` に置く）。`innerHTML` に入れる値は `esc()` か数値にする。
- 認識サーバー（`server/app.py`）: 認証なし（信頼できるネットワーク前提）。RIFF/WAVE 以外は 400、30MB・300 秒超は 413。
  `/docs` などは出さない。認識した文はログに書かない。
- 認識データ: `MODEL_SHA256`（配布元の値）と一致したものだけ展開。展開は `filter="data"`。
- 履歴の録音・再試行: 名前は `logs/audio/` 直下の `.wav` だけ。
- 公開リポジトリなので、コミット前に `packaging/private-patterns.txt` の言葉が混ざっていないか調べる。
