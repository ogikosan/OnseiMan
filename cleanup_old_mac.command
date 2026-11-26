#!/bin/bash
# 前の版の、もう使わないデータとファイルを消す（ダブルクリックで実行）。
# 通常のインストール先（.git がないフォルダ）では、Rust 版（onseiman-server）が未配置なら GitHub Releases から
# SHA-256 検証つきで自動取得し、launchd 切り替え → 不要な旧 Python 環境削除まで一括で行う。
# 預かった辞書（server_data）・ログ・聞き取り部品（tools/speech・models/haas-ja）には触らない。
cd "$(dirname "$0")" || exit 1
REPO="ogikosan/OnseiMan"
IS_GIT=0
[ -d ".git" ] && IS_GIT=1

has_rust=0
for bin in "onseiman-server" "onseiman-server-macos-arm64" "onseiman" "onseiman-macos-arm64"; do
  if [ -x "$bin" ]; then
    has_rust=1
    break
  fi
done
will_fetch=0
if [ "$IS_GIT" -eq 0 ] && [ "$has_rust" -eq 0 ] && [ "$(uname -m)" = "arm64" ]; then
  will_fetch=1
fi

old=(
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30"
  "models/sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2"
  "download_model.py"
  "shared/asr_engine.py"
  "shared/haas_engine.py"
  "server/bench_providers.py"
)
if [ "$has_rust" -eq 1 ] || [ "$will_fetch" -eq 1 ]; then
  old+=(
    ".venv_server"
    "tools/python"
    "tools/bin"
    "server/__pycache__"
    "shared/__pycache__"
  )
  if [ "$IS_GIT" -eq 0 ]; then
    old+=(
      "crates"
      "Cargo.toml"
      "Cargo.lock"
      ".github"
      "tests"
      "docs"
      "packaging"
    )
  fi
fi

found=()
for p in "${old[@]}"; do
  [ -e "$p" ] && found+=("$p")
done

if [ ${#found[@]} -eq 0 ] && [ "$will_fetch" -eq 0 ]; then
  echo "消すものはありません。"
else
  if [ "$will_fetch" -eq 1 ]; then
    echo "Rust 版サーバー（onseiman-server）を取得して切り替え、不要な旧 Python 環境を整理します:"
  else
    echo "前の版の、もう使わないものです:"
  fi
  for p in "${found[@]}"; do
    echo "  $p（$(du -sh "$p" | cut -f1)）"
  done
  echo "預かった辞書（server_data）・ログ・聞き取り部品（models/haas-ja・tools/speech）には触りません。"
  read -r -p "進めてよいですか？ (y / n) " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    if [ "$will_fetch" -eq 1 ]; then
      echo "Rust 版サーバーを GitHub Releases から取得しています..."
      BASE_URL="https://github.com/$REPO/releases/latest/download"
      TMP_DIR="$(mktemp -d)"
      if ! curl --proto '=https' --tlsv1.2 -fsSL -o "$TMP_DIR/onseiman-server" "$BASE_URL/onseiman-server-macos-arm64" || \
         ! curl --proto '=https' --tlsv1.2 -fsSL -o "$TMP_DIR/SHA256SUMS.txt" "$BASE_URL/SHA256SUMS-macos-arm64.txt"; then
        rm -rf "$TMP_DIR"
        echo "ダウンロードに失敗しました。旧環境はそのまま残して終了します。"
        exit 1
      fi
      expected="$(awk '($2 == "onseiman-server-macos-arm64" || $2 == "*onseiman-server-macos-arm64") && $1 ~ /^[0-9a-fA-F]{64}$/ {print tolower($1); exit}' "$TMP_DIR/SHA256SUMS.txt")"
      actual="$(shasum -a 256 "$TMP_DIR/onseiman-server" | awk '{print tolower($1)}')"
      if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        rm -rf "$TMP_DIR"
        echo "SHA-256 の検証に失敗しました。旧環境はそのまま残して終了します。"
        exit 1
      fi
      mv -f "$TMP_DIR/onseiman-server" onseiman-server
      rm -rf "$TMP_DIR"
      chmod +x onseiman-server
      xattr -d com.apple.quarantine onseiman-server 2>/dev/null || true
      echo "Rust 版サーバーを配置しました（SHA-256 確認 OK）。launchd を切り替えます..."
      bash server/setup_macos.sh || exit 1
    fi
    for p in "${found[@]}"; do
      rm -rf "$p" && echo "消しました: $p"
    done
    echo "おわりました。"
  else
    echo "やめました。何も変更していません。"
  fi
fi
read -n 1 -s -r -p "何かキーを押すと閉じます"
echo
