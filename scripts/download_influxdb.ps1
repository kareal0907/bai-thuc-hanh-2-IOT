# Tai InfluxDB OSS 2.7.11 ban Windows (~48 MB) tu trang chinh thuc va giai nen vao tools\influxdb.
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"
$zip = Join-Path $tools "influxdb2-2.7.11-windows.zip"
New-Item -ItemType Directory -Force $tools | Out-Null
Invoke-WebRequest -Uri "https://dl.influxdata.com/influxdb/releases/influxdb2-2.7.11-windows.zip" -OutFile $zip -UseBasicParsing
Expand-Archive -Force $zip (Join-Path $tools "influxdb")
Remove-Item $zip
& (Join-Path $tools "influxdb\influxd.exe") version
