# このパソコンの「声を文字にする役」をやめる（OnseiMan の設定画面から呼ばれる。画面は出さない）。
# 止めて、ログインしたときに自動で動く登録を外す。-RemoveGpu なら GPU で聞き取る版の Linux も消す。
# 設定・辞書・預かった辞書には触らない。
param([switch]$RemoveGpu)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$env:WSL_UTF8 = "1"

Write-Host "声を文字にする役を止めています..."
# このフォルダの認識役（CPU 版）と中継（GPU 版）
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.Name -like "python*" -and
    ($_.CommandLine -match 'server[\\/]app\.py' -or $_.CommandLine -match 'server[\\/]relay\.py') -and
    ($_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
     ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() }
if ($names -contains "onseiman-gpu") { & wsl.exe -d onseiman-gpu -u root -- pkill -f "server/app.py --engine haas" 2>$null }

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$cur = (Get-ItemProperty -Path $runKey -Name "OnseiManServer" -ErrorAction SilentlyContinue).OnseiManServer
if ($cur -and $cur.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
    Remove-ItemProperty -Path $runKey -Name "OnseiManServer"
    Write-Host "ログインしたときに自動で動く登録を外しました。"
}

if ($RemoveGpu) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "remove_gpu_server.ps1") -Yes
}
Write-Host "止めました。"
exit 0
