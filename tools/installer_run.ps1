# インストーラーが、ファイルを置いたあとに呼ぶ準備（進み具合はこの窓に出る）。
# -App:      OnseiMan のアプリを使えるようにする
# -Server:   cpu / gpu / none。このパソコンを、声を文字にする役にする
# -Resume:   WSL2 を入れて再起動したあとの続き（GPU で聞き取る版）
# -NoPause:  うまくいかなくても Enter を待たない（OnseiMan の設定画面から、窓を出さずに動かすとき）
# 終了コード: 0 = 済んだ / 3010 = 再起動が要る（再起動のあとに自動で続きをする） / それ以外 = うまくいかなかった
param([switch]$App, [ValidateSet("cpu", "gpu", "none")][string]$Server = "none", [switch]$Resume, [switch]$NoPause)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = $PSScriptRoot
$logDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Transcript -Path (Join-Path $logDir "install.log") -Append | Out-Null
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runOnceKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"

function Invoke-Step([string]$File, [string[]]$ArgList) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Tools $File) @ArgList
    return $LASTEXITCODE
}

function Stop-Install([string]$Message) {
    Write-Host ""
    Write-Host $Message
    Write-Host "くわしい記録: $logDir\install.log"
    Stop-Transcript | Out-Null
    if (-not $NoPause) { Read-Host "Enter を押すと閉じます" }
    exit 1
}

Write-Host "============================================================"
Write-Host " OnseiMan の準備をしています。この窓は、終わると自動で閉じます。"
Write-Host "============================================================"

if ($App -and -not $Resume) {
    Write-Host ""
    Write-Host "■ アプリ"
    if ((Invoke-Step "install.ps1" @("-Target", "client")) -ne 0) { Stop-Install "アプリの準備がうまくいきませんでした。インターネットにつながっているか確かめて、もう一度インストーラーを実行してください。" }
}

if ($Server -ne "none" -and -not $Resume) {
    # 切り替えるとき（CPU 版 ⇔ GPU 版）は、前の役を先に止める（同じポートを使うため。GPU 版の Linux は残す）
    Invoke-Step "stop_host_server.ps1" @() | Out-Null
    Write-Host ""
    if ($Server -eq "cpu") {
        Write-Host "■ 声を文字にする役（CPU で聞き取る版）"
        if ((Invoke-Step "install.ps1" @("-Target", "server")) -ne 0) { Stop-Install "声を文字にする役の準備がうまくいきませんでした。" }
    } else {
        Write-Host "■ 声を文字にする役（GPU で聞き取る版）"
        if ((Invoke-Step "install.ps1" @("-Target", "relay")) -ne 0) { Stop-Install "中継の準備がうまくいきませんでした。" }
    }

    # 管理者の確認を 1 回だけ出して、ファイアウォール（と WSL2）を準備する
    Write-Host ""
    Write-Host "管理者の確認が出たら「はい」を押してください（ファイアウォールの許可$(if ($Server -eq 'gpu') { 'と WSL2' })）..."
    $admArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$Tools\admin_setup.ps1`" -Root `"$Root`" -Firewall"
    if ($Server -eq "gpu") { $admArgs += " -Wsl" }
    $admCode = -1
    try {
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $admArgs -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        $admCode = $p.ExitCode
    } catch {
        Write-Host "管理者の確認が「いいえ」でした。ほかのパソコンからつなぐときに、Windows の確認が出たら「許可」を押してください。"
    }
    if ($Server -eq "gpu") {
        if ($admCode -eq 3010) {
            # 再起動のあとに、続き（GPU で聞き取る版の準備）を自動で始める
            $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$Tools\installer_run.ps1`" -Server gpu -Resume"
            Set-ItemProperty -Path $runOnceKey -Name "OnseiManSetup" -Value $cmd
            Write-Host ""
            Write-Host "WSL2 を入れました。パソコンを再起動すると、続きを自動で始めます。"
            Stop-Transcript | Out-Null
            exit 3010
        }
        if ($admCode -eq 1 -or $admCode -eq -1) {
            $wslOk = $false
            try { & wsl.exe --version *> $null; $wslOk = ($LASTEXITCODE -eq 0) } catch { }
            if (-not $wslOk) { Stop-Install "WSL2 を入れられませんでした。管理者の PowerShell で「wsl --install --no-distribution」を実行して再起動し、もう一度インストーラーを実行してください。" }
        }
    }
}

if ($Server -eq "gpu") {
    Write-Host ""
    Write-Host "■ GPU で聞き取る版（10〜20 分かかります）"
    if ((Invoke-Step "setup_gpu_server.ps1" @()) -ne 0) { Stop-Install "GPU で聞き取る版の準備がうまくいきませんでした。" }
}

if ($Server -eq "cpu") {
    # ログインしたときに、窓を出さずに動くようにして、いま起動する
    $vbs = Join-Path $Root "server\start_server_hidden.vbs"
    Set-ItemProperty -Path $runKey -Name "OnseiManServer" -Value ('wscript.exe "' + $vbs + '"')
    Start-Process -FilePath "wscript.exe" -ArgumentList ('"' + $vbs + '"')
    Write-Host "声を文字にする役を起動しました。ログインするたびに、窓を出さずに自動で動きます。"
}

if ($Resume -and (Test-Path (Join-Path $Root ".venv\onseiman-ready.txt"))) {
    Start-Process -FilePath (Join-Path $Root "start_client.bat") -WorkingDirectory $Root -WindowStyle Hidden
}

Write-Host ""
Write-Host "準備が済みました。"
Stop-Transcript | Out-Null
if (-not $NoPause) { Start-Sleep -Seconds 3 }
exit 0
