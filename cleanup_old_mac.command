#!/bin/bash
# 前の版の、もう使わないデータとファイルを消す（ダブルクリックで実行）。消すものと大きさを並べて、確かめてから消す。
# 預かった辞書（server_data）・ログ・いまの聞き取りの部品（tools/speech・models/haas-ja）には触らない。
# Rust 版（onseiman-server / onseiman）が入っている場合は、不要になった旧 Python 環境（.venv_server・tools/python・tools/bin 等）も片付け対象に含める。
cd "$(dirname "$0")" || exit 1
old=(
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30"
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2"
  "download_model.py"
  "shared/asr_engine.py"
  "shared/haas_engine.py"
  "server/bench_providers.py"
)
has_rust=0
for bin in "onseiman-server" "onseiman-server-macos-arm64" "onseiman" "onseiman-macos-arm64"; do
  if [ -x "$bin" ]; then
    has_rust=1
    break
  fi
done
if [ "$has_rust" -eq 1 ]; then
  old+=(
    ".venv_server"
    "tools/python"
    "tools/bin"
    "server/__pycache__"
    "shared/__pycache__"
  )
fi
found=()
for p in "${old[@]}"; do
  [ -e "$p" ] && found+=("$p")
done
if [ ${#found[@]} -eq 0 ]; then
  echo "消すものはありません。"
  if [ "$has_rust" -eq 0 ] && [ -e ".venv_server" ]; then
    echo "※ Rust 版（onseiman-server）へ切り替えたあとに再実行すると、旧 Python 環境（.venv_server・tools/python 等）も片付けられます。"
  fi
else
  echo "前の版の、もう使わないものです:"
  if [ "$has_rust" -eq 1 ]; then
    echo "（Rust 版が入っているため、不要になった旧 Python 環境も対象に含めています）"
  fi
  for p in "${found[@]}"; do
    echo "  $p（$(du -sh "$p" | cut -f1)）"
  done
  echo "預かった辞書と、いまの聞き取りの部品（models/haas-ja・tools/speech）には触りません。"
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
