# Chay InfluxDB 2.x (ban Windows, khong can Docker). Giu cua so nay mo trong luc thuc hanh.
$root = Split-Path -Parent $PSScriptRoot
$influxd = Join-Path $root "tools\influxdb\influxd.exe"
if (-not (Test-Path $influxd)) {
    Write-Host "Chua co influxd.exe - chay scripts\download_influxdb.ps1 truoc." -ForegroundColor Yellow
    exit 1
}
$data = Join-Path $root "data\influxdb"
New-Item -ItemType Directory -Force $data | Out-Null
Write-Host "InfluxDB: http://127.0.0.1:8086  (du lieu: $data)"
& $influxd --bolt-path "$data\influxd.bolt" --engine-path "$data\engine" `
    --http-bind-address 127.0.0.1:8086 --reporting-disabled --log-level warn
