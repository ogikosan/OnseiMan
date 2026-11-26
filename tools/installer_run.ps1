# インストーラーが、ファイルを置いたあとに呼ぶ準備（進み具合はこの窓に出る）。アプリに要る部品を入れる。
# 聞き取りの部品（speech.cpp と Haas-ja）は、はじめて起動したときに、準備の画面で用意する。
# -NoPause: うまくいかなくても Enter を待たない（インストーラーを画面なしで動かすとき）
# 終了コード: 0 = 済んだ / それ以外 = うまくいかなかった
param([switch]$NoPause)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir "install.log") -Append | Out-Null

Write-Host "============================================================"
Write-Host " OnseiMan の準備をしています。この窓は、終わると自動で閉じます。"
Write-Host "============================================================"
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1") -Target client
$code = $LASTEXITCODE
Write-Host ""
if ($code -ne 0) {
    Write-Host "準備がうまくいきませんでした。インターネットにつながっているか確かめて、もう一度インストーラーを実行してください。"
    Write-Host "くわしい記録: $logDir\install.log"
    Stop-Transcript | Out-Null
    if (-not $NoPause) { Read-Host "Enter を押すと閉じます" }
    exit 1
}
Write-Host "準備が済みました。"
Stop-Transcript | Out-Null
if (-not $NoPause) { Start-Sleep -Seconds 3 }
exit 0
