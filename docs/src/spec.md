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

## Haas-ja を speech.cpp で動かす（はじめの聞き取り。`shared/speechcpp_engine.py`・`shared/speechcpp_setup.py`）

- speech.cpp（nyosegawa/speech.cpp、MIT）の `speech worker <gguf> --device cpu|gpu` を 1 つ起動し、JSON Lines（protocol 4）で
  やりとりする。録音は 16bit の PCM を base64 にして 4 秒ずつ `chunk` で送り、`transcribe` で文を受け取る。`progress` は読み飛ばす。
  優先語は `keywords`（1 行に 1 語）。断られたら優先語なしでやり直す。worker が落ちたら 1 回だけ起動し直す。
- 部品は版を固定して確認の値で確かめる: speech.cpp 0.11.2（Windows は Vulkan 版、Mac は Metal 版。`tools/speech/`）、
  Haas-616M-ja-v1.0-Q8_0.gguf（0.66GB、HF のリビジョン固定。`models/haas-ja/`）。
- 実測（いつもの 14 本、129 秒分）: reo-minipc の CPU 6.7 秒（1 本 0.24〜1.1 秒）、内蔵 GPU（Vulkan）3.9 秒
  （1 本だけ文の終わりが欠けたので Windows は CPU にする）、Mac M4 Pro の GPU（Metal）0.96 秒、CPU 3.0 秒。文は NeMo 版とほぼ同じ。
- Haas は 1 回に 1 つの発話を渡す作り（長い録音だと文がまるごと抜けることがある）ので、区切りの上限は 15 秒。
- アプリ: `local_engine`（haas / funasr、はじめは haas）。起動して 20 秒後に部品を裏で用意し、揃ったら手元の聞き取りを作り直す
  （`_prepare_haas`）。揃うまでは Fun-ASR。履歴の backend は `Local-Haas`。
- 認識役: `--engine auto`（はじめの値）は部品があれば speechcpp、なければ funasr。`/health` は `engine: haas`・`runtime: speechcpp`。
  準備の手順（install.ps1 の server、setup_macos.sh）で部品も用意する。

## 認識役の 2 つの版（`server/app.py --engine`）

| | CPU で聞き取る版（`funasr`、既定） | GPU で聞き取る版（`haas`） |
|---|---|---|
| 聞き取り | Fun-ASR Nano int8（sherpa-onnx） | Haas-ja-v1.0（NeMo, bf16。`shared/haas_engine.py`） |
| 動く所 | Mac・Windows・CPU | Linux と NVIDIA の GPU（Windows では WSL2 の `onseiman-gpu`） |
| 準備 | `server_windows.bat` / `server_mac.command` | `server_gpu.bat`（`tools/setup_gpu_server.ps1` → `server/wsl/setup_haas.sh`） |
| 一度に送る長さ | 下の表のとおり（ヒントで短くなる） | 25 秒（`/health` の `max_segment_sec`） |
| 1 回（4070 Ti 実測） | 0.3〜0.6 秒（Mac の CPU） | 0.04〜0.06 秒。GPU の記憶 約 1.9GB |

- GPU 版は Linux の中で `127.0.0.1:8766` に立ち、Windows 側の中継 `server/relay.py`（`.venv_relay`、標準の部品だけ）が
  `0.0.0.0:8765` で受けて渡す（WSL2 の中のポートはほかのパソコンから見えないため）。起動は
  `server/start_server_gpu_hidden.vbs`（HKCU の Run `OnseiManServer`）。やめるのは `server_gpu_remove.bat`。
- GPU 版の読み込みでは、読み込みの直後に `torch.cuda.empty_cache()` して一時的な記憶（約 1.9GB）を返す。
  CUDA グラフ（速くする仕組み）は残す。聞き取りのたびに返すと CUDA グラフとぶつかって落ちる（実測）。
- Haas は英字をすべて小文字で書くので、`fix_acronyms` でよく使う略語を大文字にする。優先語は `haas_asr.Vocabulary` に渡す。
- 部品の版は固定（Haas-ASR のコミット、モデルのリビジョン、uv の版と SHA-256。`setup_haas.sh`）。
- アプリは、使っている認識役が `engine: haas` なら区切りの上限をその `max_segment_sec` にする（`_max_segment_sec`）。

## このパソコンを認識役にする（裏で自動。`client/host_setup.py`、`client/main.py: _auto_gpu_host`）

- 設定画面には出さない（話す側の「声を文字にする場所」と混ざって分かりにくかった。頼まれる側の準備は必ず要るので、自動でやる）。
- **はじめは切**（`gpu_host_auto: false`、1.2.25 から）。speech.cpp の Haas-ja が CPU でも速いので、GPU のパソコンに
  WSL2 と約 10GB を勝手に入れない。true にしたときだけ、次の動きをする。
- NVIDIA の GPU（ドライバー 580 以上）があるパソコンで起動すると、45 秒後に GPU 版の準備を始める（未準備のときだけ）。
  失敗したら 24 時間はやり直さない（`logs/gpu_host_auto.json`）。切るのは `config.json` の `gpu_host_auto: false` か、
  フォルダに `gpu_host_off`（古い版が設定を保存し直しても消えない）。
- 話す側は、さがしたときに `engine: haas` の認識役を先に並べ、はじめての「いつも使う」にする（`discovery.merge_same_machines`、app.js）。
- 以下の API と手順は残してある（画面からは呼ばない）:

- T3 Code（pingdotgg/t3code）の「共有とアクセス」にならい、自分では入れず、決まった手順を裏で動かして状態を読む。
- `GET /api/host` → 状態（すぐ返す。GPU・ファイアウォール・アドレス・Tailscale・認識役の返事は裏のスレッドで確かめて覚えておく）。
  `POST /api/host/start {kind}`（`tools/installer_run.ps1 -Server cpu|gpu -NoPause` を窓なしで。記録は `logs/host_setup.log`。
  記録は cp932 と UTF-8 が混ざるので 1 行ずつ見分けて読む）、`/stop {remove_gpu}`（`tools/stop_host_server.ps1`）、
  `/firewall`（`tools/admin_setup.ps1 -Firewall` を管理者で。tools\python の Python に、ポート 8765・プライベートだけ許可。
  自動でできた「止める」は消す）、`/tailscale {action: install|login}`。
- `installer_run.ps1` は、切り替えのときに前の役を止めてから始める。GPU 版で WSL2 を入れたら 3010（再起動のあと RunOnce で続き）。
- アドレスの一覧は、仮のネットワーク（vEthernet・WSL・Docker・VMware・VirtualBox など）を外し、Tailscale を先にする。
- インストーラー（`packaging/installer/`、Inno Setup 6）はアプリだけを入れる。`RedirectionGuard=no` が要る
  （有効だと子の手順にも引き継がれ、uv が Python のジャンクションを作れず os error 448 で失敗する）。
- GPU 版の中継は、127.0.0.1:8766 で届かなければ Linux のアドレス（`hostname -I`）に渡す（WSL の転送が不安定なパソコンがある。
  T3 Code も同じ扱い）。そのため Linux の中では 0.0.0.0:8766 で受ける。

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
  uv を取得できないときは、入っている Python 3.11 以上で代用する。サーバーは `-Target server`（`.venv_server`、モデルも取得）。
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
