# 前の版の、もう使わないデータとファイルを消す（cleanup_old.bat から呼ばれる）。
# - 通常のインストール先（.git がないフォルダ）では、Rust 版（onseiman.exe）が未配置なら GitHub Releases の最新版から
#   SHA-256 検証つきで自動取得し、旧 Python 版の停止 → 不要ファイル削除 → 自動起動の向き先更新 → Rust 版起動まで一括で行う。
# - 設定・辞書・履歴（config.json・dict.json・logs）・預かった辞書（server_data）・管理画面（client\web）・
#   聞き取りの部品（tools\speech・models\haas-ja）には絶対に触らない。
# -List: 並べるだけで、ダウンロードも削除もしない
param([switch]$List)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$env:WSL_UTF8 = "1"
$Repo = "ogikosan/OnseiMan"

$isGitRepo = Test-Path -LiteralPath (Join-Path $Root ".git")
$rustExe = Join-Path $Root "onseiman.exe"
$rustServerExe = Join-Path $Root "onseiman-server.exe"
$hasRustBin = (Test-Path -LiteralPath $rustExe) -or (Test-Path -LiteralPath $rustServerExe)
$willFetchRust = (-not $isGitRepo) -and (-not $hasRustBin)

# 1.2.27 でやめたもの
$paths = @(
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30",
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2.part",
    "download_model.py",
    "shared\asr_engine.py",
    "server\bench_providers.py",
    ".venv_relay",
    "wsl",
    "gpu_host_off",
    "server_gpu.bat",
    "server_gpu_remove.bat",
    "server\relay.py",
    "server\start_server_gpu_hidden.vbs",
    "server\wsl",
    "shared\haas_engine.py",
    "client\host_setup.py",
    "tools\setup_gpu_server.ps1",
    "tools\remove_gpu_server.ps1",
    "tools\admin_setup.ps1",
    "tools\check_gpu.ps1",
    "tools\stop_host_server.ps1",
    "logs\host_setup.log",
    "logs\gpu_host_auto.json",
    "logs\install_admin.log"
)

# 通常インストール先（.git なし）では Rust 版への切り替えと同時に旧 Python 環境・ソース一式も片付ける
if ($hasRustBin -or $willFetchRust) {
    $paths += @(
        ".venv",
        ".venv_server",
        "tools\python",
        "tools\bin",
        "__pycache__",
        "client\__pycache__",
        "shared\__pycache__",
        "server\__pycache__"
    )
    if (-not $isGitRepo) {
        $paths += @(
            "crates",
            "Cargo.toml",
            "Cargo.lock",
            ".github",
            "shared",
            "server",
            "tests",
            "docs",
            "packaging",
            "backup",
            "browser_profile",
            "install_client.bat",
            "server_mac.command",
            "server_mac_remove.command",
            "cleanup_old_mac.command",
            ".gitattributes",
            ".gitignore",
            "CLAUDE.md",
            "AGENTS.md",
            "SECURITY.md"
        )
    }
}

function Get-SizeText([string]$p) {
    $item = Get-Item -LiteralPath $p -Force
    $bytes = if ($item.PSIsContainer) { (Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum } else { $item.Length }
    if ($bytes -ge 1GB) { return "{0:N1}GB" -f ($bytes / 1GB) }
    if ($bytes -ge 1MB) { return "{0:N0}MB" -f ($bytes / 1MB) }
    return "{0:N0}KB" -f [math]::Max(1, $bytes / 1KB)
}

function Install-RustBinary([string]$DestExe) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $baseUrl = "https://github.com/$Repo/releases/latest/download"
    $tmpExe = "$DestExe.download"
    $tmpSum = "$DestExe.sha256"
    Write-Host "Rust 版（onseiman-windows-x64.exe）を GitHub Releases から取得しています..."
    try {
        Invoke-WebRequest -Uri "$baseUrl/onseiman-windows-x64.exe" -OutFile $tmpExe -UseBasicParsing
        Invoke-WebRequest -Uri "$baseUrl/SHA256SUMS-windows-x64.txt" -OutFile $tmpSum -UseBasicParsing
    } catch {
        Remove-Item -LiteralPath $tmpExe, $tmpSum -Force -ErrorAction SilentlyContinue
        Write-Host "ダウンロードに失敗しました: $($_.Exception.Message)"
        return $false
    }
    $expected = ""
    foreach ($line in (Get-Content -LiteralPath $tmpSum -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?onseiman-windows-x64\.exe\s*$') {
            $expected = $Matches[1].ToLowerInvariant()
            break
        }
    }
    $actual = (Get-FileHash -LiteralPath $tmpExe -Algorithm SHA256).Hash.ToLowerInvariant()
    Remove-Item -LiteralPath $tmpSum -Force -ErrorAction SilentlyContinue
    if (-not $expected -or $actual -ne $expected) {
        Remove-Item -LiteralPath $tmpExe -Force -ErrorAction SilentlyContinue
        Write-Host "SHA-256 の検証に失敗しました（期待: $expected / 実際: $actual）。切り替えを中止します。"
        return $false
    }
    Move-Item -LiteralPath $tmpExe -Destination $DestExe -Force
    Write-Host "Rust 版を配置しました: onseiman.exe（SHA-256 確認 OK）"
    return $true
}

$found = @($paths | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path -LiteralPath $_ })
# 通常インストール先で Rust 版へ切り替えるときは client\*.py（client\web は残す）も対象に含める
$clientPyFiles = @()
if (($hasRustBin -or $willFetchRust) -and (-not $isGitRepo)) {
    $clientDir = Join-Path $Root "client"
    if (Test-Path -LiteralPath $clientDir) {
        $clientPyFiles = @(Get-ChildItem -LiteralPath $clientDir -File -Filter "*.py" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        $reqTxt = Join-Path $clientDir "requirements.txt"
        if (Test-Path -LiteralPath $reqTxt) { $clientPyFiles += $reqTxt }
    }
}

$distro = $false
$names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() }
if ($names -contains "onseiman-gpu") { $distro = $true }
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runSrv = (Get-ItemProperty -Path $runKey -Name "OnseiManServer" -ErrorAction SilentlyContinue).OnseiManServer
$runGpu = $runSrv -and $runSrv -like "*start_server_gpu_hidden.vbs*"

if (-not $found -and -not $clientPyFiles -and -not $distro -and -not $runGpu -and -not $willFetchRust) {
    Write-Host "消すものはありません。"
    exit 0
}

if ($willFetchRust) {
    Write-Host "Rust 版（onseiman.exe）へ切り替えて、不要な旧 Python 環境と古いファイルを整理します。"
} else {
    Write-Host "前の版の、もう使わないものです:"
}
foreach ($p in $found) { Write-Host ("  " + $p.Substring($Root.Length + 1) + "（" + (Get-SizeText $p) + "）") }
if ($clientPyFiles.Count -gt 0) {
    Write-Host ("  client\*.py など旧 Python ソース（" + $clientPyFiles.Count + " ファイル ※ client\web は残します）")
}
if ($distro) { Write-Host "  GPU を使う版の Linux（onseiman-gpu）" }
if ($runGpu) { Write-Host "  ログインしたときに GPU を使う版を動かす登録" }
Write-Host "設定（config.json）・辞書（dict.json）・履歴（logs）・管理画面（client\web）・聞き取り部品（models\haas-ja・tools\speech）には触りません。"
if ($List) { exit 0 }

$ans = Read-Host "進めてよいですか？ (y / n)"
if ($ans -notin @("y", "Y", "yes")) { Write-Host "やめました。何も変更していません。"; exit 0 }

# 1) Rust 版がまだなければダウンロード＆検証（失敗したら旧環境は絶対に消さない）
if ($willFetchRust) {
    if (-not (Install-RustBinary $rustExe)) {
        Write-Host "旧環境はそのまま残して終了します。"
        exit 1
    }
    $hasRustBin = $true
}

# 2) 動いている旧 Python 版・管理画面 Edge を停止
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and
    $_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
    (
        ($_.Name -like "python*" -and $_.CommandLine -match 'relay\.py|client\\main\.py|client\.watchdog|server\\app\.py') -or
        ($_.Name -eq "msedge.exe" -and $_.CommandLine -match 'browser_profile')
    )
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500

# 3) 古い登録・WSL2・不要ファイル削除
if ($runGpu) { Remove-ItemProperty -Path $runKey -Name "OnseiManServer" -ErrorAction SilentlyContinue; Write-Host "登録を外しました" }
if ($distro) {
    & wsl.exe --terminate onseiman-gpu *> $null
    & wsl.exe --unregister onseiman-gpu *> $null
    Write-Host "Linux（onseiman-gpu）を消しました"
}
foreach ($p in ($found + $clientPyFiles)) {
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $p) {
        Write-Host ("消せませんでした（使用中かもしれません）: " + $p.Substring($Root.Length + 1))
    } else {
        Write-Host ("消しました: " + $p.Substring($Root.Length + 1))
    }
}

# 4) 自動起動が登録されていれば Rust 版へ向け直し、Rust 版を起動
if ($hasRustBin -and (Test-Path -LiteralPath $rustExe) -and (-not $isGitRepo)) {
    $curRun = (Get-ItemProperty -Path $runKey -Name "OnseiMan" -ErrorAction SilentlyContinue).OnseiMan
    if ($curRun) {
        Set-ItemProperty -Path $runKey -Name "OnseiMan" -Value "`"$rustExe`" --no-browser" -ErrorAction SilentlyContinue
        Write-Host "自動起動を Rust 版（onseiman.exe）へ更新しました。"
    }
    Write-Host "Rust 版 OnseiMan を起動します..."
    Start-Process -FilePath $rustExe -WorkingDirectory $Root
}

Write-Host "おわりました。"
exit 0
