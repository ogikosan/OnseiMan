# 前の版の、もう使わないデータとファイルを消す（cleanup_old.bat から呼ばれる）。
# 消すものと大きさを並べて、確かめてから消す。設定・辞書・履歴（config.json・dict.json・logs の履歴と録音）・
# 預かった辞書（server_data）・管理画面（client\web）・いまの聞き取りの部品（tools\speech・models\haas-ja）には触らない。
# Rust 版（onseiman.exe / onseiman-server.exe）が入っている場合は、不要になった旧 Python 環境（.venv・tools\python・tools\bin 等）も片付け対象に含める。
# -List: 並べるだけで、消さない
param([switch]$List)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$env:WSL_UTF8 = "1"

# 1.2.27 でやめたもの
$paths = @(
    # これまでの聞き取り（Fun-ASR）
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30",
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2",
    "models\sherpa-onnx-funasr-nano-int8-2025-12-30.tar.bz2.part",
    "download_model.py",
    "shared\asr_engine.py",
    "server\bench_providers.py",
    # NVIDIA の GPU を使う版（WSL2）と、このパソコンを認識役にする仕組み
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

# Rust 版（2.0.0〜）が入っている場合は、旧 Python 環境も不要になる
$hasRustBin = (Test-Path -LiteralPath (Join-Path $Root "onseiman.exe")) -or
              (Test-Path -LiteralPath (Join-Path $Root "onseiman-server.exe"))
$isGitRepo = Test-Path -LiteralPath (Join-Path $Root ".git")
$pythonPaths = @(
    ".venv",
    ".venv_server",
    "tools\python",
    "tools\bin",
    "__pycache__",
    "client\__pycache__",
    "shared\__pycache__",
    "server\__pycache__"
)
if ($hasRustBin) {
    $paths += $pythonPaths
    # 開発リポジトリ（.git あり）でなければ、Rust 版で不要になった旧フォルダ・Mac 用ファイルも整理対象に含める
    if (-not $isGitRepo) {
        $paths += @(
            "shared",
            "server",
            "tests",
            "docs",
            "packaging",
            "backup",
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
    $bytes = if ($item.PSIsContainer) { (Get-ChildItem -LiteralPath $p -Recurse -Force -File | Measure-Object Length -Sum).Sum } else { $item.Length }
    if ($bytes -ge 1GB) { return "{0:N1}GB" -f ($bytes / 1GB) }
    if ($bytes -ge 1MB) { return "{0:N0}MB" -f ($bytes / 1MB) }
    return "{0:N0}KB" -f [math]::Max(1, $bytes / 1KB)
}

$found = @($paths | ForEach-Object { Join-Path $Root $_ } | Where-Object { Test-Path -LiteralPath $_ })
$distro = $false
$names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() }
if ($names -contains "onseiman-gpu") { $distro = $true }
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$run = (Get-ItemProperty -Path $runKey -Name "OnseiManServer" -ErrorAction SilentlyContinue).OnseiManServer
$runGpu = $run -and $run -like "*start_server_gpu_hidden.vbs*"

if (-not $found -and -not $distro -and -not $runGpu) {
    Write-Host "消すものはありません。"
    if (-not $hasRustBin -and (Test-Path -LiteralPath (Join-Path $Root ".venv"))) {
        Write-Host "※ Rust 版（onseiman.exe）へ切り替えたあとに再実行すると、旧 Python 環境（.venv・tools\python 等）も片付けられます。"
    }
    exit 0
}
Write-Host "前の版の、もう使わないものです:"
if ($hasRustBin) {
    Write-Host "（Rust 版が入っているため、不要になった旧 Python 環境も対象に含めています）"
}
foreach ($p in $found) { Write-Host ("  " + $p.Substring($Root.Length + 1) + "（" + (Get-SizeText $p) + "）") }
if ($distro) { Write-Host "  GPU を使う版の Linux（onseiman-gpu）" }
if ($runGpu) { Write-Host "  ログインしたときに GPU を使う版を動かす登録" }
Write-Host "設定・辞書・履歴と、いまの聞き取りの部品（models\haas-ja・tools\speech）には触りません。"
if ($List) { exit 0 }

$ans = Read-Host "消してよいですか？ (y / n)"
if ($ans -notin @("y", "Y", "yes")) { Write-Host "やめました。何も消していません。"; exit 0 }

# 動いていたら止める（GPU を使う版の中継・旧 Python 版）
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.Name -like "python*" -and
    ($_.CommandLine -match 'relay\.py' -or ($hasRustBin -and $_.CommandLine -match 'client\\main\.py|server\\app\.py')) -and
    $_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ($runGpu) { Remove-ItemProperty -Path $runKey -Name "OnseiManServer"; Write-Host "登録を外しました" }
if ($distro) {
    & wsl.exe --terminate onseiman-gpu *> $null
    & wsl.exe --unregister onseiman-gpu *> $null
    Write-Host "Linux（onseiman-gpu）を消しました"
}
foreach ($p in $found) {
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $p) { Write-Host ("消せませんでした（使っているかもしれません）: " + $p) } else { Write-Host ("消しました: " + $p.Substring($Root.Length + 1)) }
}
Write-Host "おわりました。"
exit 0
