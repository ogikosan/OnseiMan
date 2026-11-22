#!/usr/bin/env bash
set -euo pipefail

# OnseiMan macOS (Apple Silicon / M4 Pro) オールインワン セットアップスクリプト
# - Fun-ASR Nano Multilingual API サーバー (ポート 8765)
# - Ollama + qwen2.5:7b (ポート 11434, LAN 公開, Metal GPU)
# - launchd によるログイン時自動常駐

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BASE_DIR"
export PATH="/opt/homebrew/opt/python@3.11/libexec/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "=== [1/4] Python 3.11 仮想環境と ASR サーバー依存パッケージを導入します ==="
PYTHON_BIN="$(command -v python3.11 || command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "python3 が見つかりません。Homebrew で python@3.11 を入れてください。"
  exit 1
fi
echo "Using Python: $PYTHON_BIN ($("$PYTHON_BIN" --version))"

if [ ! -d ".venv_server" ]; then
  "$PYTHON_BIN" -m venv .venv_server
fi
source .venv_server/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn python-multipart numpy soundfile sherpa-onnx

echo "=== [2/4] Fun-ASR Nano Multilingual モデル (約803MB) を取得します ==="
python download_model.py

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
  # LAN から接続できるよう OLLAMA_HOST=0.0.0.0 で起動
  OLLAMA_BIN="$(command -v ollama)"
  mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"

  cat > "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.onseiman.ollama</string>
  <key>ProgramArguments</key>
  <array>
    <string>${OLLAMA_BIN}</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key><string>0.0.0.0:11434</string>
    <key>OLLAMA_KEEP_ALIVE</key><string>30m</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${BASE_DIR}/logs/ollama.log</string>
  <key>StandardErrorPath</key><string>${BASE_DIR}/logs/ollama.log</string>
</dict>
</plist>
EOF

  launchctl unload "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist" 2>/dev/null || true
  launchctl load "$HOME/Library/LaunchAgents/com.onseiman.ollama.plist"
  sleep 2
  ollama pull qwen2.5:7b
fi
fi

echo "=== [4/4] OnseiMan ASR サーバーを launchd で常駐登録します ==="
THREADS="$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu)"
if [ "$THREADS" -gt 8 ]; then THREADS=8; fi
mkdir -p "$HOME/Library/LaunchAgents" "$BASE_DIR/logs"
cat > "$HOME/Library/LaunchAgents/com.onseiman.asr.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.onseiman.asr</string>
  <key>WorkingDirectory</key><string>${BASE_DIR}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BASE_DIR}/.venv_server/bin/python</string>
    <string>${BASE_DIR}/server/app.py</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8765</string>
    <string>--provider</string><string>cpu</string>
    <string>--threads</string><string>${THREADS}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${BASE_DIR}/logs/server.log</string>
  <key>StandardErrorPath</key><string>${BASE_DIR}/logs/server.log</string>
</dict>
</plist>
EOF

launchctl unload "$HOME/Library/LaunchAgents/com.onseiman.asr.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.onseiman.asr.plist"
sleep 3

echo "=== 疎通確認 ==="
curl -s http://127.0.0.1:8765/health && echo ""
curl -s http://127.0.0.1:11434/api/tags 2>/dev/null | head -c 200 && echo "" || true
echo "セットアップ完了！"
