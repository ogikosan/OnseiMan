# GPU で聞き取る版（Haas-ja）の認識役を、このパソコンに用意して起動する（server_gpu.bat から呼ばれる）。
# - NVIDIA の GPU と WSL2 が要る。Haas-ja は Linux でしか動かないので、OnseiMan 専用の Linux（onseiman-gpu）を
#   このフォルダの中（wsl\onseiman-gpu）に作り、その中だけに部品を入れる。ふだん使っている Linux には触らない。
# - ほかのパソコンからは、いつもの認識役と同じく、ポート 8765 で見える（Windows 側の中継 server\relay.py が受けて渡す）。
# - ログインしたときに、自動で動くように登録する（やめるときは server_gpu_remove.bat）。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Distro = "onseiman-gpu"
$DistroDir = Join-Path $Root "wsl\$Distro"
$Vbs = Join-Path $Root "server\start_server_gpu_hidden.vbs"
$ReadyWin = Join-Path $Root ".venv_relay\onseiman-gpu-ready.txt"
$env:WSL_UTF8 = "1"

function Stop-Setup([string]$Message) {
    Write-Host ""
    Write-Host $Message
    exit 1
}

function ConvertTo-LinuxPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    return "/mnt/" + $full.Substring(0, 1).ToLower() + ($full.Substring(2) -replace "\\", "/")
}

Write-Host "[OnseiMan] GPU で聞き取る版の認識役を用意します（初めてのときは 10〜20 分かかります）"

# 1) NVIDIA の GPU
if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) {
    Stop-Setup "NVIDIA の GPU が見つかりません。GPU で聞き取る版は、NVIDIA の GPU（RTX など）が要ります。`nGPU がないパソコンでは、CPU で聞き取る版（server_windows.bat）を使ってください。"
}
$gpu = & nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader 2>$null | Select-Object -First 1
Write-Host "  GPU: $gpu"

# 2) WSL2
$wslText = ""
try { $wslText = (& wsl.exe --version 2>$null | Out-String) } catch { }
if (-not $wslText -or $LASTEXITCODE -ne 0) {
    Stop-Setup "WSL2 が入っていません。管理者の PowerShell で次を実行し、パソコンを再起動してから、もう一度 server_gpu.bat を実行してください。`n  wsl --install --no-distribution"
}

# 3) すでに動いている、このフォルダの認識役を止める（同じポートを使うため）
Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and ($_.ExecutablePath.StartsWith((Join-Path $Root ".venv_relay"), [StringComparison]::OrdinalIgnoreCase) -or
        $_.ExecutablePath.StartsWith((Join-Path $Root ".venv_server"), [StringComparison]::OrdinalIgnoreCase))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 4) OnseiMan 専用の Linux
$names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if ($names -notcontains $Distro) {
    Write-Host "[1/4] OnseiMan 専用の Linux（Ubuntu 24.04）を用意しています..."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $DistroDir) | Out-Null
    & wsl.exe --install Ubuntu-24.04 --name $Distro --location $DistroDir --no-launch --web-download
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup "Linux を用意できませんでした。PowerShell で「wsl --update」を実行してから、もう一度 server_gpu.bat を実行してください。"
    }
} else {
    Write-Host "[1/4] OnseiMan 専用の Linux はできています"
    & wsl.exe -d $Distro -u root -- pkill -f "server/app.py --engine haas" 2>$null
}

# 5) Linux の中に Haas-ja
$lin = ConvertTo-LinuxPath $Root
Write-Host "[2/4] Linux の中に、Haas-ja を用意しています..."
& wsl.exe -d $Distro -u root -- bash "$lin/server/wsl/setup_haas.sh" "$lin"
if ($LASTEXITCODE -ne 0) {
    Stop-Setup "Haas-ja を用意できませんでした。インターネットにつながっているか確かめて、もう一度 server_gpu.bat を実行してください。`nそれでもだめなときは、この画面の内容を作った人に見せてください。"
}

# 6) Windows 側の中継
Write-Host "[3/4] Windows 側の中継を用意しています..."
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install.ps1") -Target relay
if ($LASTEXITCODE -ne 0) { Stop-Setup "中継を用意できませんでした。もう一度 server_gpu.bat を実行してください。" }
Set-Content -Path $ReadyWin -Value "ok" -Encoding ASCII

# 7) ログインしたときに自動で動くようにする（CPU で聞き取る版を登録していたら、こちらに置き換わる）
Write-Host "[4/4] ログインしたときに自動で動くように登録しています..."
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
Set-ItemProperty -Path $runKey -Name "OnseiManServer" -Value ('wscript.exe "' + $Vbs + '"')

# 8) 起動して、返事を待つ
Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $Vbs + '"')
Write-Host "起動しています（Haas-ja の読み込みに 10〜30 秒かかります）..."
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/health -TimeoutSec 2).Content
        if ($h -match '"status":"ok"') { $ok = $true; break }
    } catch { }
}
Write-Host ""
Write-Host "============================================================"
if ($ok) {
    Write-Host "GPU で聞き取る版の認識役が動いています。"
    Write-Host "つなぐ側は、設定の「別のパソコン」→「サーバーをさがす」で自動で見つかります。"
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
        $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -notlike "172.*"
    } | ForEach-Object { Write-Host ("  見つからないときに手で書くアドレス: http://" + $_.IPAddress + ":8765") }
    Write-Host "Windows のファイアウォールの確認が出たら「許可」を選んでください。"
    Write-Host "ログインするたびに、窓を出さずに自動で動きます。やめるときは server_gpu_remove.bat。"
} else {
    Write-Host "認識役が返事をしませんでした。logs\server_gpu.log を作った人に見せてください。"
    Write-Host "============================================================"
    exit 1
}
Write-Host "============================================================"
exit 0
