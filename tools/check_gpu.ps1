# GPU で聞き取る版を入れられるか確かめる（インストーラーから呼ばれる。画面は出さない）。
# 終了コード: 0 = 入れられる / 2 = NVIDIA の GPU が無い / 3 = NVIDIA のドライバーが古い
# 結果の説明は、%TEMP%\onseiman_gpu_check.txt に 1 行で書く（インストーラーが見せる）。
$ErrorActionPreference = "SilentlyContinue"
# Haas-ja が使う部品（CUDA 13）は、ドライバー 580 以上が要る
$MinDriver = 580
$out = Join-Path $env:TEMP "onseiman_gpu_check.txt"

$smi = Get-Command nvidia-smi
if (-not $smi) {
    Set-Content -Path $out -Value "NVIDIA の GPU が見つかりませんでした。" -Encoding UTF8
    exit 2
}
$line = & nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>$null | Select-Object -First 1
if (-not $line) {
    Set-Content -Path $out -Value "NVIDIA の GPU が見つかりませんでした。" -Encoding UTF8
    exit 2
}
$parts = $line -split ",\s*"
$name = $parts[0]
$driver = $parts[1]
$major = 0
[void][int]::TryParse(($driver -split "\.")[0], [ref]$major)
if ($major -lt $MinDriver) {
    Set-Content -Path $out -Value "$name のドライバーが古いです（いま $driver、$MinDriver 以上が要ります）。" -Encoding UTF8
    exit 3
}
Set-Content -Path $out -Value "$name（ドライバー $driver、記憶 $($parts[2])）" -Encoding UTF8
exit 0
