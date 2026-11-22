#!/bin/bash
# この Mac で動いている OnseiMan のサーバーを止めて、自動で動く登録を外す（ダブルクリックで実行）。
# OnseiMan のフォルダ（プログラムと認識用のデータ）は消しません。いらなければ、フォルダごとゴミ箱へ。
cd "$(dirname "$0")" || exit 1
for name in com.onseiman.asr com.onseiman.ollama; do
  plist="$HOME/Library/LaunchAgents/${name}.plist"
  if [ -f "$plist" ]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "止めて、登録を外しました: ${name}"
  fi
done
echo
echo "OnseiMan のサーバーは、もう自動では動きません。"
echo "もう一度使うときは server_mac.command を実行してください。"
read -n 1 -s -r -p "何かキーを押すと閉じます"
echo
