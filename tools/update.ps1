# OnseiMan を GitHub の最新版に入れ替える（git は不要）。update.bat から呼ばれる。
# - 入れ替えるのはプログラムだけ。設定（config.json）・辞書（dict.json）・履歴（logs）・認識データ（models）・.venv には触らない。
# - 入れ替えの前に、いまのプログラムを backup\ に残す。
$ErrorActionPreference = "Stop"
$Repo = "ogikosan/OnseiMan"
$Branch = "main"
$Root = Split-Path -Parent $PSScriptRoot
$Keep = @("config.json", "dict.json", "logs", "models", ".venv", ".venv_server", "backup", "build_dist")

function Get-LocalVersion {
    $p = Join-Path $Root "client\version.py"
    if (Test-Path $p) {
        $m = Select-String -Path $p -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    return "0"
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$local = Get-LocalVersion
Write-Host "いまの版: $local"
try {
    $raw = Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/$Repo/$Branch/client/version.py"
    $remote = ([regex]'__version__\s*=\s*"([^"]+)"').Match($raw.Content).Groups[1].Value
} catch {
    Write-Host "最新版を確かめられませんでした。インターネットにつながっているか確かめてください。"
    exit 1
}
Write-Host "最新の版: $remote"
if ([version]$remote -le [version]$local) {
    Write-Host "最新版です。入れ替えは不要です。"
    exit 0
}

# 動いている OnseiMan（client\main.py）があれば、先に止めてもらう
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'client[\\/]main\.py' }
if ($running) {
    Write-Host ""
    Write-Host "OnseiMan が動いています。タスクバーのアイコンを右クリック →「終了」してから、もう一度 update.bat を実行してください。"
    exit 1
}

$tmp = Join-Path $env:TEMP ("OnseiManUpdate_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "ダウンロードしています..."
    $zip = Join-Path $tmp "src.zip"
    Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp
    $src = Get-ChildItem -Path $tmp -Directory | Where-Object { $_.Name -like "OnseiMan-*" } | Select-Object -First 1
    if (-not $src -or -not (Test-Path (Join-Path $src.FullName "client\main.py"))) { throw "ダウンロードした中身が正しくありません" }

    # いまのプログラムを残しておく（うまく動かなければ backup から戻せる）
    $bk = Join-Path $Root ("backup\" + $local + "_" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Path $bk -Force | Out-Null
    foreach ($item in Get-ChildItem -Path $Root -Force | Where-Object { $Keep -notcontains $_.Name }) {
        Copy-Item -Path $item.FullName -Destination $bk -Recurse -Force
    }
    # 専用の Python と道具は大きいので、残す分には入れない
    foreach ($big in @("tools\bin", "tools\python")) {
        $bp = Join-Path $bk $big
        if (Test-Path $bp) { Remove-Item -Path $bp -Recurse -Force }
    }

    Write-Host "入れ替えています..."
    foreach ($item in Get-ChildItem -Path $src.FullName -Force | Where-Object { $Keep -notcontains $_.Name }) {
        Copy-Item -Path $item.FullName -Destination $Root -Recurse -Force
    }

    # 新しい版で必要な部品が増えていれば入れる
    & (Join-Path $Root "tools\install.ps1") -Target client | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "部品の確認に失敗しました。install_client.bat を実行してください。" }
    Write-Host ""
    Write-Host "$remote に入れ替えました。start_client.bat で起動してください。"
    Write-Host "（前の版は $bk に残してあります）"
} finally {
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
