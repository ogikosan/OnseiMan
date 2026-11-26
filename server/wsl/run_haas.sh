#!/bin/bash
# GPU で聞き取る版の認識役を動かす（OnseiMan 専用の Linux の中で）。
# Windows の server\start_server_gpu_hidden.vbs から呼ばれる。外からの声は、Windows 側の中継
# （server\relay.py、ポート 8765）が受けて、ここ（127.0.0.1:8766）に渡す。
#   引数 1: OnseiMan のフォルダ（Linux から見た場所）
set -euo pipefail

ROOT="${1:?OnseiMan のフォルダを指定してください}"
BASE=/opt/onseiman
if [ ! -f "$BASE/ready" ]; then
  echo "まだ準備が済んでいません。Windows で server_gpu.bat を実行してください。" >&2
  exit 1
fi
export HF_HOME="$BASE/hf" HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
cd "$ROOT"
exec "$BASE/Haas-ASR/.venv/bin/python" "$ROOT/server/app.py" --engine haas --host 127.0.0.1 --port 8766
