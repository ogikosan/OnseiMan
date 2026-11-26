# 管理者の権限が要る準備（インストーラーの途中で、管理者の確認を 1 回だけ出して実行する）。
# -Firewall: 声を文字にする役（ポート 8765）を、家のネットワークと Tailscale（プライベート）からだけ受け付ける。
#            以前に確認の窓を見逃して Windows が「止める」に登録していたら、それも直す。
# -Wsl:      GPU で聞き取る版に要る WSL2 が無ければ入れる（入れたら、再起動が要る＝終了コード 3010）。
param([Parameter(Mandatory = $true)][string]$Root, [switch]$Firewall, [switch]$Wsl)

$ErrorActionPreference = "Continue"
$log = Join-Path $Root "logs\install_admin.log"
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $log) | Out-Null
function Write-Log([string]$m) { Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding UTF8 }
$code = 0

if ($Firewall) {
    # OnseiMan 専用の Python（tools\python の中）。venv の python.exe はこれを呼ぶので、これに許可を出す
    $exes = Get-ChildItem -Path (Join-Path $Root "tools\python") -Recurse -Include python.exe, pythonw.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch "\\Lib\\" }
    foreach ($exe in $exes) {
        $path = $exe.FullName
        # その Python についている決まり（自動でできた「止める」も）を消してから、許可を 1 つだけ作る
        Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program -ieq $path } |
            Get-NetFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName "OnseiMan 声を文字にする役（家のネットワークと Tailscale だけ）" -Group "OnseiMan" `
            -Direction Inbound -Program $path -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private | Out-Null
        Write-Log "ファイアウォール: 許可しました $path"
    }
    if (-not $exes) { Write-Log "ファイアウォール: 専用の Python が見つかりません" }
}

if ($Wsl) {
    $ok = $false
    try { & wsl.exe --version *> $null; $ok = ($LASTEXITCODE -eq 0) } catch { }
    if ($ok) {
        Write-Log "WSL2: 入っています"
    } else {
        Write-Log "WSL2: 入れます"
        & wsl.exe --install --no-distribution
        if ($LASTEXITCODE -eq 0) { $code = 3010; Write-Log "WSL2: 入れました（再起動が要ります）" }
        else { $code = 1; Write-Log "WSL2: 入れられませんでした ($LASTEXITCODE)" }
    }
}
exit $code
