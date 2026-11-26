#!/usr/bin/env bash
# OnseiMan の認識サーバーを Mac に準備する（server_mac.command から呼ばれる）。
# - Python を自分で入れなくてよい。OnseiMan 専用の Python を、このフォルダの中（tools/python）に用意する。
#   用意には uv（https://github.com/astral-sh/uv）を使う。決まった版を、確認用の値（SHA-256）を確かめてから使う。
# - 聞き取りの部品（speech.cpp の Metal 版と Haas-ja、約0.7GB）をダウンロードし、Mac を起動するたびに自動で動くよう登録する（launchd）。
# - --with-ai を付けると、文章を整える AI（Ollama + qwen2.5:7b、約4.7GB）も入れる。
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

PORT=8765
VENV="$BASE_DIR/.venv_server"
PY="$VENV/bin/python"
UV="$BASE_DIR/tools/bin/uv"
UV_VERSION="0.13.9"
case "$(uname -m)" in
  arm64)  UV_ARCH="aarch64-apple-darwin"; UV_SHA256="5aeca67f2283f75235de40d0bd9c0a5bbc5fce23b6d48b3bf9fa1b2db08382ca" ;;
  x86_64) UV_ARCH="x86_64-apple-darwin";  UV_SHA256="617a3211261b0fc739562f0071aa34e9739797c510208ede8e2977c30903d041" ;;
  *)      UV_ARCH="";                     UV_SHA256="" ;;
esac
export UV_PYTHON_INSTALL_DIR="$BASE_DIR/tools/python"
export UV_NO_PROGRESS=1
# 取ってきた部品の控えを、ユーザーのフォルダ（~/.cache/uv など）に残さない
export UV_NO_CACHE=1
export PIP_NO_CACHE_DIR=1

get_uv() {
  [ -x "$UV" ] && return 0
  [ -n "$UV_ARCH" ] || return 1
  echo "準備の道具をダウンロードしています..."
  local tmp
  tmp="$(mktemp -d)"
  if ! curl -fsSL -o "$tmp/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_ARCH.tar.gz"; then
    rm -rf "$tmp"
    return 1
  fi
  local got
  got="$(shasum -a 256 "$tmp/uv.tar.gz" | awk '{print $1}')"
  if [ "$got" != "$UV_SHA256" ]; then
    echo "ダウンロードしたものが正しいものと一致しませんでした。"
    rm -rf "$tmp"
    return 1
  fi
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  mkdir -p "$(dirname "$UV")"
  cp "$tmp/uv-$UV_ARCH/uv" "$UV"
  chmod +x "$UV"
  rm -rf "$tmp"
}

echo "=== [1/4] 必要なものを用意します ==="
if get_uv; then
  if [ ! -x "$PY" ]; then
    echo "OnseiMan 専用の Python を用意しています..."
    "$UV" venv --managed-python --python 3.11 "$VENV"
  fi
  "$UV" pip install --python "$PY" -r server/requirements.txt
else
  # 道具を取れないとき: Mac に入っている Python 3.11 以上で代わりに用意する
  PYTHON_BIN=""
  for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PYTHON_BIN="$(command -v "$cand")"
      break
    fi
  done
  if [ -z "$PYTHON_BIN" ]; then
    echo "必要なものを用意できませんでした。インターネットにつながっているか確かめて、もう一度実行してください。"
    exit 1
  fi
  [ -x "$PY" ] || "$PYTHON_BIN" -m venv "$VENV"
  "$PY" -m pip install -r server/requirements.txt
fi
if ! "$PY" -c "import fastapi, uvicorn, numpy, soundfile"; then
  echo "用意したものが正しく動きませんでした。この画面の内容を、作った人に見せてください。"
  exit 1
fi

echo "=== [2/4] 聞き取りの部品（speech.cpp と Haas-ja、約0.7GB）を用意します ==="
if ! "$PY" shared/speechcpp_setup.py "$BASE_DIR"; then
  echo "用意できませんでした。インターネットにつながっているか確かめて、もう一度実行してください。"
  exit 1
fi

xml_esc() {
  printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}
BASE_DIR_XML="$(xml_esc "$BASE_DIR")"
PY_XML="$(xml_esc "$PY")"

if [ "${1:-}" != "--with-ai" ]; then
  echo "=== [3/4] AI 整形（Ollama, 約4.7GB）は入れません（入れるときは --with-ai を付けて実行） ==="
else
  echo "=== [3/4] Ollama と軽量日本語モデル (qwen2.5:7b) を準備します ==="
  if ! command -v ollama >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      brew install ollama
    else
      echo "Homebrew が見つかりません。https://ollama.com から Ollama をインストールしてください。"
    fi
  fi
  if command -v ollama >/dev/null 2>&1; then
    # 同じネットワークから使えるよう OLLAMA_HOST=0.0.0.0 で起動
    OLLAMA_BIN="$(command -v ollama)"
    OLLAMA_BIN_XML="$(xml_esc "$OLLAMA_BIN")"
    mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
    cat > "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.onseiman.ollama</string>
  <key>ProgramArguments</key>
  <array>
    <string>${OLLAMA_BIN_XML}</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>30m</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${BASE_DIR_XML}/logs/ollama.log</string>
  <key>StandardErrorPath</key><string>${BASE_DIR_XML}/logs/ollama.log</string>
</dict>
</plist>
EOF
    launchctl unload "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist" 2>/dev/null || true
    launchctl load "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist"
    sleep 2
    ollama pull qwen2.5:7b
  fi
fi

echo "=== [4/4] Mac を起動するたびに自動で動くよう登録します ==="
THREADS="$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu)"
if [ "$THREADS" -gt 8 ]; then THREADS=8; fi
mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
cat > "$HOME/Library/LaunchAgents/com.onseiman.asr.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.onseiman.asr</string>
  <key>WorkingDirectory</key><string>${BASE_DIR_XML}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PY_XML}</string>
    <string>${BASE_DIR_XML}/server/app.py</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>${PORT}</string>
    <string>--threads</string><string>${THREADS}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${BASE_DIR_XML}/logs/server.log</string>
  <key>StandardErrorPath</key><string>${BASE_DIR_XML}/logs/server.log</string>
</dict>
</plist>
EOF
launchctl unload "$HOME/Library/LaunchAgents/com.onseiman.asr.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.onseiman.asr.plist"

echo "サーバーの起動を待っています..."
ok=""
for _ in $(seq 1 90); do
  if curl -fs "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"ok"'; then ok="yes"; break; fi
  sleep 2
done
echo
if [ -z "$ok" ]; then
  echo "サーバーが起動しませんでした。logs/server.log の内容を、作った人に見せてください。"
  exit 1
fi
echo "準備できました。この Mac で OnseiMan のサーバーが動いています（Mac を起動するたびに自動で動きます）。"
echo "Windows の OnseiMan で、設定の「声を文字にする場所」→「別のパソコン」を選ぶと、自動で見つかります。"
echo "見つからないときに手で書くアドレス:"
for ifc in en0 en1; do
  ip="$(ipconfig getifaddr "$ifc" 2>/dev/null || true)"
  if [ -n "$ip" ]; then echo "  家のネットワーク: http://${ip}:${PORT}"; fi
done
if command -v tailscale >/dev/null 2>&1; then
  tip="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
  if [ -n "$tip" ]; then echo "  Tailscale:        http://${tip}:${PORT}"; fi
fi
exit 0
