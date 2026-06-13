$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv-sts\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$StdoutLog = Join-Path $LogDir "sts-server.out.log"
$StderrLog = Join-Path $LogDir "sts-server.err.log"
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

$HostName = Get-DotenvValue "HERMES_STS_HOST" "127.0.0.1"
$ConnectHost = $HostName
if ($ConnectHost -eq "0.0.0.0" -or $ConnectHost -eq "::") {
  $ConnectHost = "127.0.0.1"
}
$Port = [int](Get-DotenvValue "HERMES_STS_PORT" "8765")
$HealthUrl = "http://${ConnectHost}:${Port}/health"
$HermesBaseUrl = (Get-DotenvValue "HERMES_BASE_URL" "http://127.0.0.1:8642/v1").TrimEnd("/")
$HermesApiKey = Get-DotenvValue "HERMES_API_KEY" ""
$HermesModelsUrl = "$HermesBaseUrl/models"
$LemonadeBaseUrl = (Get-DotenvValue "LEMONADE_BASE_URL" "http://127.0.0.1:13305/api/v1").TrimEnd("/")
$LemonadeApiKey = Get-DotenvValue "LEMONADE_API_KEY" "nopass"
$LemonadeModelsUrl = "$LemonadeBaseUrl/models"

function Write-Step($Message) {
  Write-Host "==> $Message"
}

function Test-HttpOk($Url, $Headers = @{}, $TimeoutSec = 5) {
  try {
    Invoke-RestMethod -Uri $Url -Headers $Headers -TimeoutSec $TimeoutSec | Out-Null
    return $true
  } catch {
    return $false
  }
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

function Stop-StsOnPortIfOwned {
  $owners = Get-PortOwners $Port
  foreach ($ownerPid in $owners) {
    $cmd = Get-ProcessCommandLine $ownerPid
    if ($cmd -like "*hermes_sts*") {
      Write-Step "Stopping stale STS process on port $Port (PID $ownerPid)"
      Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    } else {
      throw "Port $Port is occupied by a non-STS process (PID $ownerPid): $cmd"
    }
  }
}

if (!(Test-Path $Python)) {
  throw "Missing STS venv Python: $Python. Run scripts\setup_venv.ps1 first."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Step "Checking Hermes API"
$HermesHeaders = @{}
if ($HermesApiKey) {
  $HermesHeaders.Authorization = "Bearer $HermesApiKey"
}
$HermesOk = Test-HttpOk $HermesModelsUrl $HermesHeaders 8
if ($HermesOk) {
  Write-Host "Hermes API: OK"
} else {
  Write-Warning "Hermes API is not reachable at $HermesModelsUrl. STS can still start, but LLM responses may use fallback."
}

Write-Step "Checking Lemonade local model API"
$LemonadeHeaders = @{}
if ($LemonadeApiKey) {
  $LemonadeHeaders.Authorization = "Bearer $LemonadeApiKey"
}
$LemonadeOk = Test-HttpOk $LemonadeModelsUrl $LemonadeHeaders 8
if ($LemonadeOk) {
  Write-Host "Lemonade API: OK"
} else {
  Write-Warning "Lemonade API is not reachable at $LemonadeModelsUrl. Hermes/local fallback may fail."
}

Write-Step "Restarting STS service"
Stop-StsOnPortIfOwned

Write-Step "Starting STS server on ${HostName}:${Port}"
Start-Process `
  -FilePath $Python `
  -ArgumentList "-m", "hermes_sts" `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $StdoutLog `
  -RedirectStandardError $StderrLog

$Started = $false
for ($i = 0; $i -lt 30; $i++) {
  Start-Sleep -Seconds 1
  if (Test-HttpOk $HealthUrl @{} 3) {
    $Started = $true
    break
  }
}
if (!$Started) {
  Write-Host ""
  Write-Host "STDERR tail:"
  if (Test-Path $StderrLog) {
    Get-Content $StderrLog -Tail 40
  }
  throw "STS did not become healthy on $HealthUrl"
}
Write-Host "STS started: $HealthUrl"

Write-Step "Current STS health"
Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 5 | ConvertTo-Json -Depth 8

Write-Step "Reachy daemon/app status"
if (Test-HttpOk "http://127.0.0.1:8000/api/daemon/status" @{} 5) {
  try {
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/apps/current-app-status" -TimeoutSec 5 |
      ConvertTo-Json -Depth 8
  } catch {
    Write-Warning "Reachy daemon is reachable, but current app status endpoint failed: $($_.Exception.Message)"
  }
} else {
  Write-Warning "Reachy daemon is not reachable at http://127.0.0.1:8000. Start Reachy Mini Control if the robot app needs to connect."
}

Write-Step "Established connections to STS"
$connections = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
  Where-Object { $_.State -eq "Established" -or $_.State -eq 5 } |
  Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess)
if ($connections.Count -gt 0) {
  $connections | ConvertTo-Json -Depth 5
} else {
  Write-Host "No client is currently connected to STS. This is normal until Reachy conversation app connects."
}

Write-Host ""
Write-Host "STS pipeline is ready."
Write-Host "WebSocket endpoint: ws://${ConnectHost}:${Port}/v1/realtime"
Write-Host "Logs:"
Write-Host "  $StdoutLog"
Write-Host "  $StderrLog"
