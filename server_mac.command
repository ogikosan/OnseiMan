#!/bin/bash
# OnseiMan の認識サーバーを、この Mac に準備する（ダブルクリックで実行）。
# 準備が済むと、Mac を起動するたびに自動で動きます。やめるときは server_mac_remove.command。
cd "$(dirname "$0")" || exit 1
echo "OnseiMan のサーバーを、この Mac に準備します（はじめては 10 分ほどかかります）"
echo
bash server/setup_macos.sh "$@"
status=$?
echo
if [ $status -ne 0 ]; then
  echo "うまくいきませんでした。上の内容を、作った人に見せてください。"
else
  echo "終わりました。この窓は閉じて大丈夫です（サーバーは裏で動き続けます）。"
fi
read -n 1 -s -r -p "何かキーを押すと閉じます"
echo
exit $status
