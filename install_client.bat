@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [OnseiMan] アプリを準備します（数分かかります）...
if not exist ".venv" (
    python -m venv .venv
)

call .venv\Scriptsctivate.bat
python -m pip install --upgrade pip
pip install -r clientequirements.txt

echo.
echo ============================================================
echo 準備できました。start_client.bat をダブルクリックで起動します。
echo  - 別のパソコンで認識するときは、設定の「声を文字にする場所」で
echo    「別のパソコン」を選び、「サーバーをさがす」を押してください。
echo  - このパソコンで認識するときは、初回起動時にデータ（約840MB）を自動で取得します。
echo ============================================================
pause
