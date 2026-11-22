# プロジェクト固有の作業指示（OnseiMan）

親の AGENTS.md を継承する。

## 実行と検証
- 起動（Windows クライアント）：`start_client.bat`（= `.venv\Scripts\pythonw.exe client\main.py`）。2 重起動は防止されるので、変更を試すときは動いている OnseiMan をトレイの「終了」で止めてから起動する。
- コード変更時の必須確認：`.venv\Scripts\python.exe -m unittest discover -s tests`（全件 OK）。
- 認識まわりを変えたとき：付属の音声で実際に認識させて確かめる（`models/.../test_wavs/`。長い発話は複数をつないで試す）。
- 画面（`client/web/`）を変えたとき：`node -e "new Function(require('fs').readFileSync('client/web/app.js','utf8'))"` で文法確認し、ヘッドレス Edge で撮影して見た目を確かめる。動いている OnseiMan と同じポートを取り合うので、試験用の Web サーバーは `_preferred_port = 0` で起動する。
- 友達に渡す一式：`python packaging\make_friend_package.py`（個人の情報が混ざっていたら作らない）。
- 文書のみ：リンク・内容・実際の仕様との整合性。

## 該当時に読む資料
- 機能変更：`docs/src/spec.md`
- 人間向けの説明が変わる場合：`docs/index.html`、アプリ内の「使い方」（`client/web/index.html` の `view-help`）、`packaging/friend/はじめに.md`
- サーバー（Mac / Windows）：`server/`、`server/setup_macos.sh`

## 固有の制約
- 利用者のデータ：`config.json`・`dict.json`・`logs/`（履歴・録音・学習候補を含む）は消さない・上書きしない。辞書を整理するときは `logs/` にバックアップを取ってから。
- モデル `models/`（約 1GB）と `.venv` は作り直しに時間がかかる。消さない。
- 音声認識モデル（Fun-ASR Nano）は一度に扱える量に上限がある（音声・辞書のヒント・出力の合計）。区切りの長さと辞書のヒントの数を増やすときは、実音声で欠けないか必ず確かめる（`docs/src/spec.md` の「量の上限」）。
- キーボードの監視（`client/input_hook.py`）を変えるときは、落ちてもキーボードが効かなくならないこと（見張り `client/watchdog.py`）を壊さない。
- `packaging/exe-installer/` は exe と配布サイトの道具（2026-11-22 に配布サイトは削除済み・現在は不使用）。
