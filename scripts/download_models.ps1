$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ModelsDir = Join-Path $Root "models"
New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

function Download-FileIfMissing($Url, $OutputPath) {
  if (Test-Path $OutputPath) {
    Write-Host "Exists: $OutputPath"
    return
  }
  Write-Host "Downloading: $Url"
  curl.exe -L --retry 3 --connect-timeout 30 -o $OutputPath $Url
  if ($LASTEXITCODE -ne 0) {
    throw "Download failed: $Url"
  }
}

$SenseDir = Join-Path $ModelsDir "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
New-Item -ItemType Directory -Force -Path $SenseDir | Out-Null
$SenseBase = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
Download-FileIfMissing "$SenseBase/model.int8.onnx" (Join-Path $SenseDir "model.int8.onnx")
Download-FileIfMissing "$SenseBase/tokens.txt" (Join-Path $SenseDir "tokens.txt")

$KokoroDir = Join-Path $ModelsDir "kokoro-multi-lang-v1_0"
$KokoroArchive = Join-Path $ModelsDir "kokoro-multi-lang-v1_0.tar.bz2"
if (!(Test-Path (Join-Path $KokoroDir "model.onnx"))) {
  Download-FileIfMissing `
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/kokoro-multi-lang-v1_0.tar.bz2" `
    $KokoroArchive
  Write-Host "Extracting Kokoro model..."
  tar -xf $KokoroArchive -C $ModelsDir
} else {
  Write-Host "Exists: $KokoroDir"
}

Write-Host "Models are ready under: $ModelsDir"
