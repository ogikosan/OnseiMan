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

## 聞き取り（Haas-ja を speech.cpp で動かす。`shared/speechcpp_engine.py`・`shared/speechcpp_setup.py`）

- 聞き取りはこれ 1 つ（アプリの「このパソコン」と、認識役の両方）。予備の聞き取りは持たない。
- speech.cpp（nyosegawa/speech.cpp、MIT）の `speech worker <gguf> --device cpu|gpu` を 1 つ起動し、JSON Lines（protocol 4）で
  やりとりする。録音は 16bit の PCM を base64 にして 4 秒ずつ `chunk` で送り、`transcribe` で文を受け取る。`progress` は読み飛ばす。
  worker が落ちたら 1 回だけ起動し直す。
- 優先語は `keywords`（1 行に 1 語、「書き方=読み,読み」）。読みは辞書の「聞き間違えた言い方」のうちカナだけのもの
  （`CustomDictionary.keyword_readings`、1 語 5 つまで）。認識役には `keywords` 項目（JSON）で送る。断られたら優先語なしでやり直す。
- 英字はすべて小文字で出るので、よく使う略語を大文字にする（`shared/acronyms.py`）。
- 部品は版を固定して確認の値で確かめる: speech.cpp 0.11.2（Windows は Vulkan 版、Mac は Metal 版。`tools/speech/`）、
  Haas-616M-ja-v1.0-Q8_0.gguf（0.66GB、HF のリビジョン固定。`models/haas-ja/`）。途中で止まったら続きから取る（Range）。
  zip は取り出し先の外に書けない。
- 動かす場所: Windows は CPU（内蔵 GPU では文の終わりが欠けたことがある）、Mac は Apple の GPU（`default_device`）。
- 実測（いつもの 14 本、129 秒分）: reo-minipc の CPU 6.7 秒（1 本 0.24〜1.1 秒）、Mac M4 Pro の GPU 0.96 秒。
- Haas は 1 回に 1 つの発話を渡す作り（いくつもの文が入った長い録音では、文がまるごと抜けることがある）。
  区切りの上限は 15 秒（`MAX_SEGMENT_SEC`）。認識役がもっと短い長さを返せば、それに合わせる（`_max_segment_sec`）。
  短い話は区切らずに通しで聞き取る（上限の 85% まで。区切ると前後のつながりが切れて聞き間違いが増える）。
- アプリ: はじめて起動したとき、部品が無ければ準備の画面（`client/model_setup.py`）で取ってくる。履歴の backend は `Local-Haas`。
- 認識役: `server/app.py`。部品が無ければ起動しない（準備の手順 `install.ps1 -Target server`・`setup_macos.sh` が取ってくる）。
  `/health` は `engine: haas`・`max_segment_sec: 15`。アプリは `engine: haas` の認識役を先に並べ、はじめての「いつも使う」にする。
- 辞書の優先語は新しく登録した順に 40 語まで。ひらがなだけ・1 文字の言葉は渡さない（`shared/dictionary.py: active_hotwords`）。

## インストーラー（`packaging/installer/`、Inno Setup 6）

- アプリだけを入れる（管理者の権限なし、はじめは `%LOCALAPPDATA%\Programs\OnseiMan`）。ファイルを置いたあと
  `tools/installer_run.ps1` で `install.ps1 -Target client` を動かす。聞き取りの部品は、はじめて起動したときに取ってくる。
- `RedirectionGuard=no` が要る（有効だと子の手順にも引き継がれ、uv が Python のジャンクションを作れず os error 448 で失敗する）。
- アンインストールで消すのは作り直せる大きなもの（専用の Python・部品・聞き取りの部品・控え）。設定・辞書・履歴は残す。
- 公開は `python packaging\release.py`（テスト → zip 2 つ → インストーラー → main → タグ → GitHub に公開を全部）。

## 辞書（`dict.json`）

- 画面では「正しい言葉」ごとに 1 行。正しい言葉は自動で認識のヒントになる。聞き間違えた言い方（`replacements`）は任意。
- かなだけの言い方は、カタカナの一続きの語として出たときだけ置き換える。
- 3 文字以下の言い方は、画面で登録するときに確認を出す。自動学習でも自動登録しない（確認カードを出す）。
- 自動学習（`shared/correction_learner.py`）は、両方に同じ助詞が付いているだけなら外して覚える。

## 定型文（`dict.json` の `snippets`）

- 合言葉 → 入れる文。整えたあとの発話「全体」が合言葉と一致したときだけ展開する（文の途中では展開しない。前後の句読点は無視、かなはカタカナと同一視）。
- 展開した文は登録したとおりに入れる（文末の「。」削り・言い直しの整理・AI 整形にかけない）。履歴の認識元に「+定型文」が付く。
- 個人の情報（メールアドレス・住所）が入りうるので、友達用の一式には `dict.json` を入れない。

## 辞書をほかのパソコンと合わせる（`shared/dict_sync.py`）

- 設定 `dict_share`: `""`（合わせない）/ `"server"`（認識役のパソコンの `/dict/<名前>`）/ `"folder"`（同期フォルダの `OnseiMan-dict-<名前>.json`）。
  名前は `dict_share_name`（英数字と `-` `_`、既定 `shared`）。サーバーは「いつも使う」もの、無ければ一覧の先頭。
- 合わせるのは `hotwords`・`replacements`・`fillers`・`builtin_off`・`snippets`。ほかの項目（`_comment` など）はそのパソコンのまま。
- 三方向の合わせ込み: 前回合わせたときの辞書（`logs/dict_sync.json`）と比べ、両方の足した・消した・変えたを 1 件ずつ合わせる。
  同じ言葉を両方で変えたら、このパソコンの変更を使う。初めて・預け先が変わったときは、消さずに足し合わせる。
- 預け先は版の番号を持ち、読んだあとに書き換えられていたら書き込みを断る（サーバーは 409）。読み直して合わせ直す。
  合わせている途中に画面で辞書を直したら、書かずに読み直す（直した言葉を消さない）。
- 起動時・設定や辞書を保存したとき・辞書のファイルが変わったとき・1 分ごとに合わせる。
- サーバーは `server_data/dicts/<名前>.json` に置く（利用者のデータ。更新で消さない）。2MB まで、合わせる項目だけ残す。

## 履歴の録音

- `GET /api/audio/<名前>.wav`（`logs/audio/` の wav だけ。Range 対応）。画面は音量を聞きやすい大きさに上げて再生する。
- 履歴の `has_audio` が真の行にだけ ▶ と「再試行」を出す。

## 表示

- 録音中・認識中・入力した・注意は、ひとつのカプセル（`client/pill.py`、レイヤードウィンドウ）で出す。フォーカスを奪わない。ドラッグで場所を固定（`overlay_position`）。
- 設定・辞書・履歴・使い方は Web 画面（`client/web/`、`client/web_ui.py`、既定ポート 18765）。

## 準備と更新（Windows）

- `install_client.bat` → `tools\install.ps1 -Target client`。uv（版と SHA-256 を固定）を `tools\bin` に取得し、
  専用の Python 3.11 を `tools\python` に置いて `.venv` を作る。Python が入っていなくてよい。最後に全部品の読み込みを確かめる。
  uv を取得できないときは、入っている Python 3.11 以上で代用する。サーバーは `-Target server`（`.venv_server`、聞き取りの部品も取得）。
- `update.bat` → `tools\update.ps1`。GitHub の `main` の zip で上書きし、`install.ps1` で部品を確かめる。
  `config.json`・`dict.json`・`logs`・`models`・`.venv` には触らない。前の版は `backup\` に残す（`tools\bin`・`tools\python` は残さない）。
  動いているかの確かめは、このフォルダから動いている OnseiMan だけ。git で管理しているフォルダ（開発用）は上書きしない。
- 新しい版のお知らせ（`client/github_update.py`）: 起動時と 1 日 1 回、GitHub の `main` の `client/version.py` を見る。
  新しければ画面の上に帯（何が変わったかは `変更点.md` の新しい版の節）、トレイは「新しい版にする（版）」。
  「今すぐ更新」は確認してから `update.ps1 -WaitPid <この OnseiMan> -Restart` を新しい窓で動かして終了する。
  update.ps1 は終わるのを待ってから入れ替え、起動し直す（失敗しても前の版のまま起動し直す）。開発用のフォルダ（git）では上書きせず、作業中の変更も手元だけの記録もないときだけ `origin/<いまのブランチ>` を早送りで足す（git pull --ff-only と同じ）。そうでなければ何も変えずに理由を出す。
- 認識役が古いかのお知らせ（`shared/server_fingerprint.py`）: 認識役は `/health` で `version` と `fingerprint` を返す。
  指紋は認識役が使うファイル（`SERVER_FILES`）の中身から作る（改行の違いは無視）。アプリは手元の同じファイルから指紋を作り、
  違っていて、しかも認識役の版が古い（または版を返さない）ときだけ「認識役のパソコンが古い版です」の帯を出す。
  Mac 用 zip には版を返すために `client/version.py` だけ入れる。認識役が使うファイルを足したら `SERVER_FILES` にも足す。

- Windows の認識役を窓なしで常に動かすときは、ログイン時の自動起動に `wscript.exe "<OnseiMan>\server\start_server_hidden.vbs"` を登録する（準備前なら `start_server.bat` を開いて準備から。CPU の数の半分、2〜8 で動かし、記録は `logs\server.log`）。

## データの場所

- ソースで動かすとき：プロジェクト直下（`config.json`, `dict.json`, `logs/`, `models/`）。
- exe 版：`%LOCALAPPDATA%\OnseiMan`（`client/paths.py`）。
- 辞書を合わせた記録は `logs/dict_sync.json`。サーバーが預かった辞書は `server_data/dicts/`。
- 録音は `logs/audio/` に新しい 100 件まで（履歴の「もう一度文字にする」「再試行」用）。

## 守り（変えるときは `tests/test_security.py`・`tests/test_server_guard.py` を通す）

- 設定画面（`client/web_ui.py`）: 127.0.0.1 のみ。Host が 127.0.0.1/localhost 以外は 403（DNS リバインディング）。
  POST は Origin が自分・Content-Type が JSON のものだけ（CSRF）。Sec-Fetch-Site が same-origin / none 以外は 403（ほかのサイトからの読み込み・埋め込み）。
  全応答に CSP（`script-src 'self'`・`frame-ancestors 'none'`・`connect-src 'self'`）・X-Frame-Options・nosniff・CORP。
  画面の HTML に直接スクリプトを書かない（`theme.js`・`app.js` に置く）。`innerHTML` に入れる値は `esc()` か数値にする。
- 認識サーバー（`server/app.py`）: 認証なし（信頼できるネットワーク前提）。RIFF/WAVE と M4A（先頭 4〜8 バイトが `ftyp`）以外は 400、30MB・300 秒超は 413。M4A は afconvert（無ければ ffmpeg、どちらも無ければ 415）で 16kHz モノラル WAV にする（15 秒・30MB で打ち切り）。18 秒（優先語が多いほど短く）を超える音声は、話の切れ目で区切って認識し、つないで返す。
  `/docs` などは出さない。認識した文はログに書かない。
- 認識データ: `MODEL_SHA256`（配布元の値）と一致したものだけ展開。展開は `filter="data"`。
- 履歴の録音・再試行: 名前は `logs/audio/` 直下の `.wav` だけ。
- 公開リポジトリなので、コミット前に `packaging/private-patterns.txt` の言葉が混ざっていないか調べる。
