$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv-sts\Scripts\python.exe"
$Uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (!$Uv) { $Uv = Join-Path (Join-Path $env:LOCALAPPDATA "Reachy Mini Control") "uv.exe" }
$Mirror = $env:PYPI_INDEX_URL
if (!$Mirror) { $Mirror = "https://mirrors.aliyun.com/pypi/simple" }
& $Uv pip install --python $Python --no-cache --index-url $Mirror -e "$Root[sherpa]"
