# OnseiMan に必要なものを用意する（install_client.bat / server\install_server_windows.bat から呼ばれる）。
# - Python が入っていなくてもよい。OnseiMan 専用の Python を、このフォルダの中（tools\python）に用意する。
#   パソコン全体には何も入れない。管理者の権限もいらない。
# - 用意には uv（https://github.com/astral-sh/uv）を使う。決まった版を、確認用の値（SHA-256）を確かめてから使う。
# -Starting: start_client.bat / start_server.bat から、起動の前に呼ばれたとき（終わったら、そのまま起動する）
# -Target relay: GPU で聞き取る版の中継（server\relay.py）用の Python だけを用意する（部品は入れない）
param([ValidateSet("client", "server", "relay")][string]$Target = "client", [switch]$Starting)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$UvVersion = "0.13.9"
$UvSha256 = "edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb"
$UvUrl = "https://github.com/astral-sh/uv/releases/download/$UvVersion/uv-x86_64-pc-windows-msvc.zip"
$PythonVersion = "3.11"

if ($Target -eq "server") {
    $VenvName = ".venv_server"
    $Req = "server\requirements.txt"
} elseif ($Target -eq "relay") {
    $VenvName = ".venv_relay"
    $Req = ""
} else {
    $VenvName = ".venv"
    $Req = "client\requirements.txt"
}
$Venv = Join-Path $Root $VenvName
$Py = Join-Path $Venv "Scripts\python.exe"
$UvExe = Join-Path $Root "tools\bin\uv.exe"
# 準備が最後まで済んだ印。start_client.bat は、これが無ければ先にこの準備を実行する
$Ready = Join-Path $Venv "onseiman-ready.txt"
if (Test-Path $Ready) { Remove-Item -Path $Ready -Force }

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
# 専用の Python は、このフォルダの中に置く
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Root "tools\python"
$env:UV_NO_PROGRESS = "1"
# 取ってきた部品の控えを、ユーザーのフォルダ（%LOCALAPPDATA%\uv など）に残さない
$env:UV_NO_CACHE = "1"
$env:PIP_NO_CACHE_DIR = "1"

function Get-Uv {
    if (Test-Path $UvExe) { return $true }
    Write-Host "準備の道具をダウンロードしています..."
    $tmp = Join-Path $env:TEMP ("OnseiManSetup_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp "uv.zip"
        Invoke-WebRequest -UseBasicParsing $UvUrl -OutFile $zip
        $hash = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLower()
        if ($hash -ne $UvSha256) { throw "ダウンロードしたものが正しいものと一致しませんでした" }
        Expand-Archive -Path $zip -DestinationPath $tmp
        $found = Get-ChildItem -Path $tmp -Recurse -Filter "uv.exe" | Select-Object -First 1
        if (-not $found) { throw "uv.exe が見つかりません" }
        New-Item -ItemType Directory -Path (Split-Path -Parent $UvExe) -Force | Out-Null
        Copy-Item -Path $found.FullName -Destination $UvExe -Force
        return $true
    } catch {
        Write-Host ("道具をダウンロードできませんでした: " + $_.Exception.Message)
        return $false
    } finally {
        Remove-Item -Path $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Find-SystemPython {
    # 専用の Python を用意できなかったときの予備: パソコンに入っている Python 3.11 以上を探す
    foreach ($cand in @(@("py", "-3.11"), @("python"))) {
        try {
            $exe = $cand[0]
            $rest = @()
            if ($cand.Count -gt 1) { $rest = $cand[1..($cand.Count - 1)] }
            $v = & $exe @rest -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.11") { return , $cand }
        } catch { }
    }
    return $null
}

Write-Host "[OnseiMan] 必要なものを用意します（数分かかります）"
$haveUv = Get-Uv

if (-not (Test-Path $Py)) {
    if ($haveUv) {
        Write-Host "OnseiMan 専用の Python を用意しています..."
        & $UvExe venv --managed-python --python $PythonVersion $Venv
        if ($LASTEXITCODE -ne 0) { throw "Python を用意できませんでした" }
    } else {
        $sys = Find-SystemPython
        if (-not $sys) {
            Write-Host ""
            Write-Host "Python を用意できませんでした。インターネットにつながっているか確かめて、もう一度実行してください。"
            exit 1
        }
        $exe = $sys[0]
        $rest = @()
        if ($sys.Count -gt 1) { $rest = $sys[1..($sys.Count - 1)] }
        & $exe @rest -m venv $Venv
        if ($LASTEXITCODE -ne 0) { throw "Python の環境を作れませんでした" }
    }
}

if ($Target -eq "relay") {
    # 中継は Python の標準の部品だけで動く
    & $Py -c "import asyncio"
    if ($LASTEXITCODE -ne 0) { Write-Host "用意した Python が動きませんでした。"; exit 1 }
    Set-Content -Path $Ready -Value "ok" -Encoding ASCII
    exit 0
}

Write-Host "部品を入れています..."
$reqPath = Join-Path $Root $Req
if ($haveUv) {
    & $UvExe pip install --python $Py -r $reqPath
    if ($LASTEXITCODE -eq 0 -and $Target -eq "server") { & $UvExe pip install --python $Py sherpa-onnx }
} else {
    & $Py -m pip install -r $reqPath
    if ($LASTEXITCODE -eq 0 -and $Target -eq "server") { & $Py -m pip install sherpa-onnx }
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "部品を入れられませんでした。インターネットにつながっているか確かめて、もう一度実行してください。"
    exit 1
}

# 入ったものが動くか確かめる
if ($Target -eq "server") {
    & $Py -c "import fastapi, uvicorn, numpy, sherpa_onnx"
} else {
    & $Py -c "import tkinter, numpy, sounddevice, requests, pystray, PIL, sherpa_onnx, uiautomation, pycaw; tkinter.Tcl()"
}
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "用意したものが正しく動きませんでした。この画面の内容を、作った人に見せてください。"
    exit 1
}
Set-Content -Path $Ready -Value "ok" -Encoding ASCII

Write-Host ""
Write-Host "============================================================"
if ($Target -eq "server") {
    Write-Host "認識用のデータ（約840MB）を用意しています..."
    & $Py (Join-Path $Root "download_model.py")
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Path $Ready -Force -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Host ""
    if ($Starting) { Write-Host "準備できました。サーバーを起動します。" } else { Write-Host "準備できました。server_windows.bat で起動します。" }
    Write-Host "Windows のファイアウォールの確認が出たら「許可」を選んでください。"
} else {
    if ($Starting) { Write-Host "準備できました。OnseiMan を起動します。" } else { Write-Host "準備できました。start_client.bat をダブルクリックで起動します。" }
    Write-Host " - 初めて起動したとき、声を文字にするためのデータ（約840MB）を自動でダウンロードします。"
    Write-Host " - 右Alt を押して話し、もう一度押すと、カーソルの場所に文字が入ります。"
}
Write-Host "============================================================"
exit 0
