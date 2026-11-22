@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

echo [OnseiMan Server] 認識サーバーをセットアップします...
if not exist ".venv_server" (
    python -m venv .venv_server
)

call .venv_server\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r server\requirements.txt

echo.
echo sherpa-onnx (CUDA 12.x 対応版) のインストールを試行します...
pip install sherpa-onnx --pre -f https://k2-fsa.github.io/sherpa/onnx/cuda.html
if errorlevel 1 (
    echo [警告] CUDA wheel の自動取得に失敗しました。通常版 sherpa-onnx を入れます。
    pip install -U sherpa-onnx
)

echo.
echo Fun-ASR Nano Multilingual モデル (約850MB) をダウンロードします...
python download_model.py

echo.
echo セットアップ完了！ server\start_server.bat で起動できます。
pause
