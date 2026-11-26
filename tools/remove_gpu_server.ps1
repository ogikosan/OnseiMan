# GPU で聞き取る版の認識役をやめる（server_gpu_remove.bat から呼ばれる）。
# 止めて、自動で動く登録を外し、OnseiMan 専用の Linux（onseiman-gpu）と、その中の Haas-ja を消す。
# OnseiMan のほかの部分（アプリ・設定・辞書・預かった辞書）と、ふだん使っている Linux には触らない。
# -Yes: 聞かずに進める（OnseiMan の設定画面から呼ぶとき。画面で確かめてある）
param([switch]$Yes)
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Distro = "onseiman-gpu"
$DistroDir = Join-Path $Root "wsl\$Distro"
$env:WSL_UTF8 = "1"

Write-Host "GPU で聞き取る版の認識役をやめます。"
Write-Host "  - 認識役を止めて、ログインしたときに自動で動く登録を外します"
Write-Host "  - OnseiMan 専用の Linux（$Distro、約 10GB）と、その中の Haas-ja を消します"
Write-Host "  OnseiMan のアプリ・設定・辞書・預かった辞書は消しません。"
if (-not $Yes) {
    $ans = Read-Host "よろしいですか？ (y / n)"
    if ($ans -notin @("y", "Y", "yes")) { Write-Host "やめました。何も変えていません。"; exit 0 }
}

Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith((Join-Path $Root ".venv_relay"), [StringComparison]::OrdinalIgnoreCase)
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$cur = (Get-ItemProperty -Path $runKey -Name "OnseiManServer" -ErrorAction SilentlyContinue).OnseiManServer
if ($cur -and $cur -like "*start_server_gpu_hidden.vbs*") {
    Remove-ItemProperty -Path $runKey -Name "OnseiManServer"
    Write-Host "自動で動く登録を外しました。"
}

$names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($names -contains $Distro) {
    & wsl.exe --terminate $Distro 2>$null | Out-Null
    & wsl.exe --unregister $Distro
    Write-Host "OnseiMan 専用の Linux を消しました。"
}
if (Test-Path $DistroDir) { Remove-Item -Path $DistroDir -Recurse -Force -ErrorAction SilentlyContinue }
$relay = Join-Path $Root ".venv_relay"
if (Test-Path $relay) { Remove-Item -Path $relay -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "おわりました。CPU で聞き取る版に戻すときは server_windows.bat を実行してください。"
exit 0
