#!/bin/bash
# GPU で聞き取る版（Haas-ja）を、OnseiMan 専用の Linux（WSL2 の onseiman-gpu）の中に用意する。
# Windows の server_gpu.bat（tools\setup_gpu_server.ps1）から、root で呼ばれる。
#   引数 1: OnseiMan のフォルダ（Linux から見た場所。例 /mnt/d/10_Tools/OnseiMan）
# 入れるものは、すべてこの Linux の中の /opt/onseiman だけ。Windows 側には何も入れない。
set -euo pipefail

ROOT="${1:?OnseiMan のフォルダを指定してください}"
BASE=/opt/onseiman
UV_VERSION=0.13.9
UV_SHA256=a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5
# 試して確かめた版に固定する
HAAS_REPO=https://github.com/third-intelligence/Haas-ASR.git
HAAS_COMMIT=acc614f68769dcbfae9dba96b48439c4a66295af
MODEL_ID=third-intelligence/Haas-ja-v1.0
MODEL_REVISION=660728d39019e5e065046154beca44ac9a66a540

export DEBIAN_FRONTEND=noninteractive
export UV_NO_CACHE=1 UV_NO_PROGRESS=1 UV_PYTHON_INSTALL_DIR="$BASE/python"
export HF_HOME="$BASE/hf" HF_HUB_DISABLE_TELEMETRY=1

rm -f "$BASE/ready"
mkdir -p "$BASE/bin"

echo "[1/5] Linux の部品を入れています..."
apt-get update -qq
# python3.12-dev: Triton（GPU の計算の部品）が、初めて動くときに組み立てるのに使う
apt-get install -y -qq git curl ca-certificates build-essential python3.12-dev ffmpeg >/dev/null

echo "[2/5] 準備の道具（uv $UV_VERSION）を用意しています..."
if [ ! -x "$BASE/bin/uv" ] || ! "$BASE/bin/uv" --version | grep -q "$UV_VERSION"; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz"
  echo "$UV_SHA256  $tmp/uv.tar.gz" | sha256sum -c --quiet -
  tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
  install -m 755 "$tmp"/uv-x86_64-unknown-linux-gnu/uv "$BASE/bin/uv"
  rm -rf "$tmp"
fi
UV="$BASE/bin/uv"

echo "[3/5] Haas-ja の部品を入れています（数 GB。時間がかかります）..."
if [ ! -d "$BASE/Haas-ASR/.git" ]; then
  rm -rf "$BASE/Haas-ASR"
  git clone -q "$HAAS_REPO" "$BASE/Haas-ASR"
fi
git -C "$BASE/Haas-ASR" fetch -q origin
git -C "$BASE/Haas-ASR" checkout -q "$HAAS_COMMIT"
cd "$BASE/Haas-ASR"
# 作った人が確かめた版の組み合わせ（uv.lock）のとおりに入れる
"$UV" sync --frozen
# OnseiMan の認識役に要るもの
"$UV" pip install --python "$BASE/Haas-ASR/.venv/bin/python" -r "$ROOT/server/requirements.txt"
PY="$BASE/Haas-ASR/.venv/bin/python"

echo "[4/5] Haas-ja の認識用のデータ（約 1.2GB）を用意しています..."
"$PY" -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL_ID', revision='$MODEL_REVISION')"

echo "[5/5] GPU で動くか確かめています..."
"$PY" -c "import torch, haas_asr, fastapi, uvicorn; assert torch.cuda.is_available(), 'GPU が見えません'; print('  GPU:', torch.cuda.get_device_name(0))"

date -Iseconds > "$BASE/ready"
echo "準備できました。"
