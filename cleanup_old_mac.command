#!/bin/bash
# 前の版の、もう使わないデータとファイルを消す（ダブルクリックで実行）。消すものと大きさを並べて、確かめてから消す。
# 預かった辞書（server_data）・ログ・いまの聞き取りの部品（tools/speech・models/haas-ja）には触らない。
cd "$(dirname "$0")" || exit 1
old=(
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30"
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2"
  "download_model.py"
  "shared/asr_engine.py"
  "shared/haas_engine.py"
  "server/bench_providers.py"
)
found=()
for p in "${old[@]}"; do
  [ -e "$p" ] && found+=("$p")
done
if [ ${#found[@]} -eq 0 ]; then
  echo "消すものはありません。"
else
  echo "前の版の、もう使わないものです:"
  for p in "${found[@]}"; do
    echo "  $p（$(du -sh "$p" | cut -f1)）"
  done
  echo "預かった辞書と、いまの聞き取りの部品には触りません。"
  read -r -p "消してよいですか？ (y / n) " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    for p in "${found[@]}"; do
      rm -rf "$p" && echo "消しました: $p"
    done
    echo "おわりました。"
  else
    echo "やめました。何も消していません。"
  fi
fi
read -n 1 -s -r -p "何かキーを押すと閉じます"
echo
