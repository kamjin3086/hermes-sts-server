$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$VenvPython = Join-Path $Root ".venv-sts\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} elseif ($env:PYTHON) {
    $Python = $env:PYTHON
} else {
    $Python = "python"
}

Write-Host "==> Python"
& $Python --version

Write-Host "==> Compile check"
& $Python -m compileall -q hermes_sts tests

Write-Host "==> Unit tests"
& $Python -m unittest discover -s tests -p "test_*.py" -v
