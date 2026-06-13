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

Stop the service:

```powershell
.\scripts\stop_sts_pipeline.ps1
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

Stop the service:

```bash
./scripts/stop_sts_pipeline.sh
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

## Tool Injection

The official Reachy Mini Conversation App owns robot-side tool execution. It
loads enabled tools from the selected profile, sends their Realtime function
schemas in `session.update`, listens for
`response.function_call_arguments.done`, executes the tool locally, then sends
the result back as `conversation.item.create` with `type=function_call_output`
followed by `response.create`.

`hermes-sts-server` follows that boundary:

- Tools supplied by the app are registered per WebSocket session.
- Tool schemas are forwarded to the configured LLM provider as OpenAI-compatible
  chat tools.
- Local STS tools stay tiny (`noop`, `current_time`) and are executed in the
  server.
- App/client tools are not executed by STS. They are emitted back to the app as
  `response.function_call_arguments.done` so the app can run Reachy actions,
  camera, head-tracking, memory, custom profile tools, or remote Space tools.
- When the app returns `function_call_output`, the server asks the LLM for a
  final short spoken answer using the real tool result.

Minimal Realtime tool shape:

```json
{
  "type": "session.update",
  "session": {
    "instructions": "Use Chinese. Use tools for robot actions.",
    "tools": [
      {
        "type": "function",
        "name": "dance",
        "description": "Queue a Reachy Mini dance.",
        "parameters": {
          "type": "object",
          "properties": {
            "dance": { "type": "string", "enum": ["happy"] }
          },
          "required": ["dance"],
          "additionalProperties": false
        }
      }
    ],
    "tool_choice": "auto"
  }
}
```

Prompt policy: pass the Conversation App profile instructions to the LLM, but
only as additive session instructions. STS keeps its own base system prompt for
voice brevity, language matching, tool-use rules, and "do not speak JSON/tool
arguments/expression tags" behavior. This keeps app-selected personalities and
tool mappings effective without letting a profile accidentally break the audio
contract.

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

Fast local checks that do not need running models or a live STS server:

Windows:

```powershell
.\scripts\run_tests.ps1
```

Fedora/Linux:

```bash
./scripts/run_tests.sh
```

These run a compile check plus the standard-library unit test suite. The current
core tests cover energy VAD, audio append validation, tool schema normalization,
client tool forwarding, tool-result follow-up, TTS segmentation, and bracketed
cue stripping.

Runtime smoke checks after the server is running:

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

`ws_turn_smoke.py` uses generated audio and is best suited to the `energy` VAD
or `dev` provider path. Model VADs such as Silero may correctly ignore pure tone
audio because it is not speech.

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

## Roadmap

Near-term work should keep the project reliable, local-first, and small:

1. Lightweight web search tool for direct LLM mode.
   - Preferred first provider: Tavily.
   - Expose it as a fast STS-local tool only when explicitly configured.
   - Return compact search snippets and source URLs to the LLM; do not stream
     full pages through the speech path.
   - Keep Hermes Agent mode unchanged, because Hermes may already own its own
     retrieval/tooling layer.

2. Better voice quality without heavy PyTorch runtime.
   - Evaluate OmniVoice or an equivalent non-PyTorch/GPU-accelerated path.
   - Preserve the current `sherpa_kokoro` provider as the stable default until
     the new provider has comparable startup reliability and latency.
   - Measure first-audio latency, segment synthesis time, voice naturalness, and
     Windows/Fedora parity before making it recommended.

3. Local memory for direct LLM mode.
   - Add a small memory provider abstraction, used only when
     `STS_LLM_PROVIDER=openai_compatible` unless explicitly enabled elsewhere.
   - Start with a local file-backed store for durable user facts and session
     summaries.
   - Consider Obsidian-compatible Markdown storage as an optional backend for
     human-readable notes.
   - Prefer local libraries or simple local persistence over hosted memory
     services. Keep the default privacy-preserving and easy to inspect.
