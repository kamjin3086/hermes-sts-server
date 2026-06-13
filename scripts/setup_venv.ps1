$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Venv = Join-Path $Root ".venv-sts"
$Mirror = $env:PYPI_INDEX_URL
if (!$Mirror) {
  $envFile = Join-Path $Root ".env"
  if (Test-Path $envFile) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^PYPI_INDEX_URL=' } | Select-Object -First 1
    if ($line) {
      $Mirror = ($line -replace '^PYPI_INDEX_URL=', '').Trim()
    }
  }
}
if (!$Mirror) {
  $Mirror = "https://mirrors.aliyun.com/pypi/simple"
}
$env:UV_DEFAULT_INDEX = $Mirror
Write-Host "Using PyPI mirror: $Mirror"

function Find-Uv {
  $cmd = Get-Command uv -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }
  $reachyUv = Join-Path (Join-Path $env:LOCALAPPDATA "Reachy Mini Control") "uv.exe"
  if (Test-Path $reachyUv) {
    return $reachyUv
  }
  throw "Could not find uv. Install uv or add it to PATH."
}

function Find-Python312 {
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    $path = & $py.Source -3.12 -c "import sys; print(sys.executable)"
    if ($LASTEXITCODE -eq 0 -and $path -and (Test-Path $path)) {
      return $path
    }
  }

  $candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path (Join-Path $env:LOCALAPPDATA "Reachy Mini Control") "cpython-3.12.13-windows-x86_64-none\python.exe")
  )
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) {
      $version = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
      if ($version -eq "3.12") {
        return $candidate
      }
    }
  }
  throw "Could not find Python 3.12. Install it or make py -3.12 work."
}

$Uv = Find-Uv
$Python = Find-Python312

if (!(Test-Path $Venv)) {
  Write-Host "Creating isolated STS venv with uv and Python: $Python"
  & $Uv venv --python $Python $Venv
}

$VenvPython = Join-Path $Venv "Scripts\python.exe"
& $Uv pip install --python $VenvPython `
  --no-cache `
  --index-url $Mirror `
  "fastapi>=0.115" `
  "uvicorn[standard]>=0.30" `
  "websockets>=12" `
  "httpx>=0.27" `
  "python-dotenv>=1.0" `
  "numpy>=1.26,<2" `
  "sherpa-onnx==1.13.2"
& $Uv pip install --python $VenvPython --no-cache --index-url $Mirror -e $Root --no-deps

Write-Host "STS venv ready: $Venv"
