# OnseiMan を GitHub の最新版に入れ替える（git は不要）。update.bat と、アプリの「今すぐ更新」から呼ばれる。
# - 入れ替えるのはプログラムだけ。設定（config.json）・辞書（dict.json）・履歴（logs）・認識データ（models）・.venv には触らない。
# - 入れ替えの前に、いまのプログラムを backup\ に残す。
# - -WaitPid <番号>: その OnseiMan が終わるのを待ってから始める（アプリの「今すぐ更新」）。
# - -Restart: 終わったら OnseiMan を起動し直す（うまくいかなかったときも、前の版のまま起動し直す）。
param([int]$WaitPid = 0, [switch]$Restart)

$ErrorActionPreference = "Stop"
$Repo = "ogikosan/OnseiMan"
$Branch = "main"
$Root = Split-Path -Parent $PSScriptRoot
$Keep = @("config.json", "dict.json", "logs", "models", ".venv", ".venv_server", "backup", "build_dist", "server_data", "browser_profile")

function Finish([int]$code) {
    if ($Restart) {
        if ($code -ne 0) {
            Write-Host ""
            Read-Host "Enter を押すと、前の版のまま OnseiMan を起動し直します"
        } else {
            Write-Host "OnseiMan を起動し直します..."
        }
        Start-Process -FilePath (Join-Path $Root "start_client.bat") -WorkingDirectory $Root -WindowStyle Hidden
        if ($code -eq 0) { Start-Sleep -Seconds 3 }
    }
    exit $code
}

function Get-LocalVersion {
    $p = Join-Path $Root "client\version.py"
    if (Test-Path $p) {
        $m = Select-String -Path $p -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value }
    }
    return "0"
}

# このフォルダから動いている OnseiMan だけ（ほかの場所に置いた OnseiMan は関係ない）
function Get-RunningOnseiMan {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
        Where-Object {
            $_.CommandLine -match 'client[\\/]main\.py' -and (
                ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) -or
                ($_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0))
        }
}

# git で管理している開発用のフォルダは上書きしない（作業中の変更を壊さないため）
if (Test-Path (Join-Path $Root ".git")) {
    Write-Host "このフォルダは git で管理している開発用のフォルダです。上書きはしません（git pull で更新してください）。"
    Finish 1
}

if ($WaitPid -gt 0) {
    Write-Host "OnseiMan が終わるのを待っています..."
    try { Wait-Process -Id $WaitPid -Timeout 60 -ErrorAction Stop } catch { }
    # 起動用の小さな処理が残っていることがあるので、少し待つ
    for ($i = 0; $i -lt 30 -and (Get-RunningOnseiMan); $i++) { Start-Sleep -Milliseconds 500 }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$local = Get-LocalVersion
Write-Host "いまの版: $local"
try {
    $raw = Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/$Repo/$Branch/client/version.py"
    $remote = ([regex]'__version__\s*=\s*"([^"]+)"').Match($raw.Content).Groups[1].Value
} catch {
    Write-Host "最新版を確かめられませんでした。インターネットにつながっているか確かめてください。"
    Finish 1
}
Write-Host "最新の版: $remote"
if ([version]$remote -le [version]$local) {
    Write-Host "最新版です。入れ替えは不要です。"
    Finish 0
}

# 動いている OnseiMan（client\main.py）があれば、先に止めてもらう
if (Get-RunningOnseiMan) {
    Write-Host ""
    Write-Host "OnseiMan が動いています。タスクバーのアイコンを右クリック →「終了」してから、もう一度 update.bat を実行してください。"
    Finish 1
}

$tmp = Join-Path $env:TEMP ("OnseiManUpdate_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$ok = $false
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
    # 専用の Python（tools\python）と道具（tools\bin）は大きく、中に近道（ジャンクション）もあるので、残す分に入れない
    foreach ($item in Get-ChildItem -Path $Root -Force | Where-Object { $Keep -notcontains $_.Name -and $_.Name -ne "tools" }) {
        Copy-Item -Path $item.FullName -Destination $bk -Recurse -Force
    }
    $bkTools = Join-Path $bk "tools"
    New-Item -ItemType Directory -Path $bkTools -Force | Out-Null
    foreach ($item in Get-ChildItem -Path (Join-Path $Root "tools") -Force | Where-Object { @("bin", "python") -notcontains $_.Name }) {
        Copy-Item -Path $item.FullName -Destination $bkTools -Recurse -Force
    }

    Write-Host "入れ替えています..."
    foreach ($item in Get-ChildItem -Path $src.FullName -Force | Where-Object { $Keep -notcontains $_.Name }) {
        Copy-Item -Path $item.FullName -Destination $Root -Recurse -Force
    }

    # 新しい版で必要な部品が増えていれば入れる
    if ($Restart) { & (Join-Path $Root "tools\install.ps1") -Target client -Starting | Out-Null }
    else { & (Join-Path $Root "tools\install.ps1") -Target client | Out-Null }
    if ($LASTEXITCODE -ne 0) { Write-Host "部品の確認に失敗しました。次に起動するときに、もう一度確かめます。" }
    Write-Host ""
    if ($Restart) {
        Write-Host "$remote に入れ替えました。"
    } else {
        Write-Host "$remote に入れ替えました。start_client.bat で起動してください。"
    }
    Write-Host "（前の版は $bk に残してあります）"
    $ok = $true
} catch {
    Write-Host ""
    Write-Host "入れ替えられませんでした: $($_.Exception.Message)"
} finally {
    Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
if ($ok) { Finish 0 } else { Finish 1 }
