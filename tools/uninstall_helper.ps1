# アンインストールのときに呼ばれる（画面は出さない）。
# 止めて、自動で動く登録を外し、GPU で聞き取る版の Linux を消す。ファイアウォールの許可は、管理者の確認を出して消す。
# 設定・辞書・履歴（config.json・dict.json・logs・server_data）は残す。
param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = "SilentlyContinue"
$env:WSL_UTF8 = "1"
$Root = $Root.TrimEnd("\")

# このフォルダの OnseiMan（アプリ・声を文字にする役・中継）を止める
Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.Name -like "python*" -and $_.CommandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 自動で動く登録（このフォルダを指しているものだけ）
foreach ($key in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Run", "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce")) {
    foreach ($name in @("OnseiMan", "OnseiManServer", "OnseiManSetup")) {
        $v = (Get-ItemProperty -Path $key -Name $name).$name
        if ($v -and $v.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0) { Remove-ItemProperty -Path $key -Name $name }
    }
}

# GPU で聞き取る版の Linux（このフォルダの中に作ったものだけ）
if (Test-Path (Join-Path $Root "wsl\onseiman-gpu")) {
    $names = (& wsl.exe -l -q 2>$null | Out-String) -split "`r?`n" | ForEach-Object { $_.Trim() }
    if ($names -contains "onseiman-gpu") {
        & wsl.exe --terminate onseiman-gpu *> $null
        & wsl.exe --unregister onseiman-gpu *> $null
    }
}

# ファイアウォールの許可（OnseiMan が作ったもの）。管理者の確認が「いいえ」なら残る（フォルダが消えるので、使われない）
$hasRule = Get-NetFirewallRule -Group "OnseiMan" -ErrorAction SilentlyContinue
if ($hasRule) {
    try {
        Start-Process -FilePath "powershell.exe" -Verb RunAs -WindowStyle Hidden -Wait `
            -ArgumentList '-NoProfile -Command "Remove-NetFirewallRule -Group OnseiMan"'
    } catch { }
}
exit 0
