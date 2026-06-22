$ErrorActionPreference = "Stop"

$LocalApp = Join-Path $env:LOCALAPPDATA "Reachy Mini Control"
$Candidates = @(
  (Join-Path $LocalApp "reachy_mini_conversation_app\.env"),
  (Join-Path $LocalApp ".temp\reachy_mini_conversation_app\.env")
)

$Target = $Candidates[0]
$Dir = Split-Path -Parent $Target
New-Item -ItemType Directory -Force -Path $Dir | Out-Null

@"
BACKEND_PROVIDER=huggingface
HF_REALTIME_CONNECTION_MODE=local
HF_REALTIME_WS_URL=ws://127.0.0.1:8765/v1/realtime
"@ | Set-Content -LiteralPath $Target -Encoding UTF8

Write-Host "Wrote Reachy conversation app env candidate: $Target"
Write-Host "If your Reachy app uses a different instance path, copy these three lines into that app's .env."
