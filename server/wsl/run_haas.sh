#!/bin/bash
# GPU で聞き取る版の認識役を動かす（OnseiMan 専用の Linux の中で）。
# Windows の server\start_server_gpu_hidden.vbs から呼ばれる。外からの声は、Windows 側の中継
# （server\relay.py、ポート 8765）が受けて、ここ（8766）に渡す。Windows の 127.0.0.1 から届かない
# パソコンもある（WSL の転送が不安定。T3 Code でも同じ扱い）ので、Linux のアドレスでも受ける。
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
exec "$BASE/Haas-ASR/.venv/bin/python" "$ROOT/server/app.py" --engine haas --host 0.0.0.0 --port 8766
