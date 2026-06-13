# Hermes STS Server for Reachy Mini

Independent local STS bridge for `reachy_mini_conversation_app` and Hermes Agent.

The Reachy app stays in the Reachy Mini Control managed environment. This server
runs in its own Python 3.12 virtual environment and exposes an OpenAI
Realtime-compatible WebSocket:

```text
ws://127.0.0.1:8765/v1/realtime
```

## Runtime Flow

1. Start Hermes Agent and make sure its OpenAI-compatible API is reachable:

   ```text
   http://127.0.0.1:8642/v1
   ```

2. Start this STS service, for example with the desktop BAT or:

   ```powershell
   .\scripts\start_sts_pipeline.ps1
   ```

3. Start Reachy Mini Control and launch `reachy_mini_conversation_app`.

4. Configure the conversation app to connect to the local realtime endpoint:

   ```text
   BACKEND_PROVIDER=huggingface
   HF_REALTIME_CONNECTION_MODE=local
   HF_REALTIME_WS_URL=ws://127.0.0.1:8765/v1/realtime
   ```

The desktop BAT can be started before or after Hermes. It will warn when Hermes
is not reachable, but the LLM part will only work after Hermes is healthy.

## Recommended Local Providers

Current recommended Chinese-first setup:

- STT: `sherpa_sensevoice`
- TTS: `sherpa_kokoro`
- LLM: Hermes Agent at `http://127.0.0.1:8642/v1`

This avoids heavy PyTorch dependencies in the STS venv and keeps STT/TTS local.

## Setup

```powershell
.\scripts\setup_venv.ps1
Copy-Item .env.example .env
notepad .env
.\scripts\download_models.ps1
.\scripts\start_sts_pipeline.ps1
```

Set `HERMES_API_KEY` in `.env` when Hermes requires an API key.

## Health Checks

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
.\.venv-sts\Scripts\python.exe .\scripts\ws_smoke.py
.\.venv-sts\Scripts\python.exe .\scripts\ws_turn_smoke.py
```

Logs are written under `logs/`.

## Repository Hygiene

Do not commit:

- `.env`
- `.venv-sts/`
- `logs/`
- `models/`
- `*.egg-info/`
- `__pycache__/`

Large ONNX model files should be downloaded with `scripts\download_models.ps1`.
