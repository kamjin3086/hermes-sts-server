$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv-sts\Scripts\python.exe"
$EnvFile = Join-Path $Root ".env"

function Get-DotenvValue($Name, $Default = "") {
  if (!(Test-Path $EnvFile)) {
    return $Default
  }
  $line = Get-Content $EnvFile | Where-Object { $_ -match "^$Name=" } | Select-Object -First 1
  if (!$line) {
    return $Default
  }
  return (($line -replace "^$Name=", "").Trim().Trim('"'))
}

function Write-Step($Message) {
  Write-Host "==> $Message"
}

function Get-PortOwners($Port) {
  @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" -or $_.State -eq 2 } |
    Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ProcessCommandLine($ProcessId) {
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
  if ($proc) {
    return [string]$proc.CommandLine
  }
  return ""
}

function Stop-StsProcessTreeForRoot {
  $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
  if ($all.Count -eq 0) {
    return $false
  }

  $ids = New-Object 'System.Collections.Generic.HashSet[int]'
  foreach ($proc in $all) {
    $cmd = [string]$proc.CommandLine
    $exe = [string]$proc.ExecutablePath
    if (($cmd -like "*hermes_sts*") -and (($cmd -like "*$Root*") -or ($exe -like "$Root*"))) {
      [void]$ids.Add([int]$proc.ProcessId)
    }
  }

  $changed = $true
  while ($changed) {
    $changed = $false
    foreach ($proc in $all) {
      if ($ids.Contains([int]$proc.ParentProcessId) -and -not $ids.Contains([int]$proc.ProcessId)) {
        [void]$ids.Add([int]$proc.ProcessId)
        $changed = $true
      }
    }
  }

  foreach ($pidToStop in @($ids)) {
    if ($pidToStop -eq $PID) {
      continue
    }
    Write-Step "Stopping residual STS process for this workspace (PID $pidToStop)"
    Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
  }
  return ($ids.Count -gt 0)
}

$Port = [int](Get-DotenvValue "HERMES_STS_PORT" "8765")
$owners = Get-PortOwners $Port

if ($owners.Count -eq 0) {
  $stoppedResidual = Stop-StsProcessTreeForRoot
  if ($stoppedResidual) {
    Write-Host "STS residual process tree stopped."
  } else {
    Write-Host "STS is not listening on port $Port, and no residual STS process was found."
  }
  exit 0
}

$stopped = $false
foreach ($ownerPid in $owners) {
  $cmd = Get-ProcessCommandLine $ownerPid
  if ($cmd -like "*hermes_sts*") {
    Write-Step "Stopping STS process on port $Port (PID $ownerPid)"
    Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    $stopped = $true
  } else {
    throw "Port $Port is occupied by a non-STS process (PID $ownerPid): $cmd"
  }
}
$stopped = (Stop-StsProcessTreeForRoot) -or $stopped

if ($stopped) {
  Write-Host "STS stopped on port $Port."
} else {
  Write-Host "No STS process stopped."
}
