$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv-sts\Scripts\python.exe"
if (!(Test-Path $Python)) {
  throw "Missing venv. Run scripts\setup_venv.ps1 first."
}
Set-Location $Root
& $Python -m hermes_sts
