# CLAUDE.md（プロジェクト固有）

作業の決まりは同じフォルダの `AGENTS.md` を読む（実行・検証・触ってはいけないもの）。

## 概要

話した言葉をカーソルの場所に入力する Windows 用の音声入力アプリ。認識は Fun-ASR Nano（sherpa-onnx）で、このパソコンか、別のパソコン（Mac など）のサーバーで行う。

## よく使うコマンド

```text
start_client.bat                                   起動
.venv\Scripts\python.exe -m unittest discover -s tests   テスト
python packaging\make_friend_package.py            友達に渡す zip を作る
```

## 注意点

- `config.json` / `dict.json` / `logs/` は利用者のデータ。消さない。
- 認識モデルには一度に扱える量の上限がある。区切りの長さと辞書のヒント数を変えたら実音声で確かめる。
