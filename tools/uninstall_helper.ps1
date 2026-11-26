# アンインストールのときに呼ばれる（画面は出さない）。このフォルダの OnseiMan を止めて、自動で動く登録を外す。
# 設定・辞書・履歴（config.json・dict.json・logs・server_data）は残す。
param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = "SilentlyContinue"
$Root = $Root.TrimEnd("\")
$RootPrefix = $Root + "\"

# このフォルダの OnseiMan（アプリ・認識役・聞き取りの道具）を止める
Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.Name -like "python*" -and $_.CommandLine.IndexOf($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0)
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# 自動で動く登録（このフォルダを指しているものだけ）
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
foreach ($name in @("OnseiMan", "OnseiManServer")) {
    $v = (Get-ItemProperty -Path $key -Name $name).$name
    if ($v -and $v.IndexOf($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0) { Remove-ItemProperty -Path $key -Name $name }
}
exit 0
