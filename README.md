# Hermes STS Server for Reachy Mini

Independent local STS bridge for `reachy_mini_conversation_app` and Hermes Agent.

The Reachy app stays in the Reachy Mini Control managed environment. This server
runs in its own Python 3.12 virtual environment and exposes an OpenAI
Realtime-like WebSocket:

```text
ws://127.0.0.1:8765/v1/realtime
```

## Runtime Flow

```text
Reachy Mini
  -> WebSocket / OpenAI Realtime-like
hermes-sts-server
  -> VAD
STT
  -> LLM / Hermes Agent / OpenAI-compatible model
  -> TTS
  -> chunked PCM16 audio response
Reachy Mini
```

The service expects mono PCM16 little-endian audio at `HERMES_STS_SAMPLE_RATE`
(default `16000`).

## Recommended Providers

Recommended local, Chinese-first setup:

- VAD: `sherpa_silero`
- STT: `sherpa_sensevoice`
- TTS: `sherpa_kokoro`
- LLM: `hermes_agent` at `http://127.0.0.1:8642/v1`

This keeps the STS service small while still running VAD/STT/TTS locally on
Windows and Fedora/Linux.

Use the same `.env` provider settings on Windows and Fedora/Linux for matching
behavior. `sapi` is kept only as a Windows fallback; the recommended path on
both platforms is `sherpa_kokoro`.

## Windows Setup

```powershell
.\scripts\setup_venv.ps1
Copy-Item .env.example .env
notepad .env
.\scripts\download_models.ps1
.\scripts\start_sts_pipeline.ps1
```

The desktop BAT remains available:

```powershell
.\scripts\Start Hermes STS Pipeline.bat
```

## Fedora/Linux Setup

Install Python 3.12 and `uv`, then run:

```bash
./scripts/setup_venv.sh
cp .env.example .env
${EDITOR:-vi} .env
./scripts/download_models.sh
./scripts/start_sts_pipeline.sh
```

If `uv` is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Reachy Mini Conversation App

Configure the conversation app to connect to the local realtime endpoint:

```text
BACKEND_PROVIDER=huggingface
HF_REALTIME_CONNECTION_MODE=local
HF_REALTIME_WS_URL=ws://127.0.0.1:8765/v1/realtime
```

## Provider Configuration

Main provider switches:

```text
STS_VAD_PROVIDER=sherpa_silero
STS_STT_PROVIDER=sherpa_sensevoice
STS_TTS_PROVIDER=sherpa_kokoro
STS_LLM_PROVIDER=hermes_agent
```

For a no-model smoke test, set:

```text
STS_VAD_PROVIDER=energy
STS_STT_PROVIDER=dev
STS_TTS_PROVIDER=tone
HERMES_ALLOW_FALLBACK=true
```

To call a direct OpenAI-compatible model instead of Hermes Agent:

```text
STS_LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=your-model
LLM_API_KEY=
```

## Latency and Duplex Settings

The server logs one summary line per completed turn:

```text
Turn latency turn_x status=completed utterance_ms=... stt_ms=... llm_ms=... first_tts_ms=... first_audio_ms=... total_ms=...
```

Useful tuning switches:

```text
STS_LATENCY_LOGGING=true
STS_SUPPRESS_INPUT_WHILE_SPEAKING=true
STS_RESPONSE_AUDIO_CHUNK_MS=80
STS_TTS_SEGMENT_MIN_CHARS=24
STS_TTS_SEGMENT_MAX_CHARS=90
STS_TTS_STRIP_BRACKETED_CUES=true
VAD_MIN_SILENCE_SECONDS=0.5
```

`STS_SUPPRESS_INPUT_WHILE_SPEAKING=true` prevents the robot from hearing its own
TTS output as a new user turn. Set it to `false` only when you need open-mic
barge-in during playback and your speaker/mic isolation is good.

`STS_TTS_STRIP_BRACKETED_CUES=true` prevents short expression cues such as
`[呲牙]` or `（笑）` from being spoken by TTS. Ordinary parenthetical content like
`（杭州）` is preserved.

`.env.example` intentionally keeps only the common knobs. Advanced fallback
settings such as `LLM_FALLBACK_*`, `LEMONADE_*`, `SAPI_VOICE`, and generic
`SHERPA_TTS_*` are still supported by `hermes_sts.config.Settings`, but are not
needed for the recommended Windows/Fedora setup.

## Health Checks

Windows:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
.\.venv-sts\Scripts\python.exe .\scripts\ws_smoke.py
.\.venv-sts\Scripts\python.exe .\scripts\ws_turn_smoke.py
.\.venv-sts\Scripts\python.exe .\scripts\ws_cancel_smoke.py
```

Fedora/Linux:

```bash
curl http://127.0.0.1:8765/health
./.venv-sts/bin/python scripts/ws_smoke.py
./.venv-sts/bin/python scripts/ws_turn_smoke.py
./.venv-sts/bin/python scripts/ws_cancel_smoke.py
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

Large ONNX model files should be downloaded with `scripts/download_models.ps1`
or `scripts/download_models.sh`.
