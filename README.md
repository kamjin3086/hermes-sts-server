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

## Admin Console And Configuration

Open the local console after the server starts:

```text
http://127.0.0.1:8765/
```

Configuration is UI-first. Runtime settings live in
`data/hermes_sts.sqlite3`, and the console is the place to edit them. This
project no longer reads `.env` files.

Changes that affect native/model-backed components, such as STT/TTS providers
or Qwen model paths, are applied by restarting the user service instead of
hot-rebuilding those components in-process. The restart costs a few seconds but
keeps memory stable on unified-memory systems.

The console includes:

- Status dashboard: current role, TTS engine, voice, Qwen model availability,
  recent preview latency, real turn latency, RTF, uptime, and switchable voice
  waveform styles.
- Role and voice studio: persona prompt and TTS voice are edited together.
  Selecting a persona preset immediately shows the full prompt in an editable
  text box; saving persists it for refreshes and restarts.
- Voice workshop: call the configured Hermes/LLM backend to generate a complete
  persona + Qwen3TTS voice proposal, preview it, apply it, or save it as a
  reusable role.
- Seed A/B deck: generate five Qwen Base voice seeds, preview candidates,
  collect the good ones with tags, filter saved voices, and delete with
  confirmation.
- Source switches: choose whether persona and voice are controlled by the
  console or by Reachy WebSocket profile/session data.
- First setup: fill Hermes/LLM, STT, TTS, and Qwen model paths from the browser.
- Advanced settings: model paths, backend, debug args, VAD, Kokoro fallback, and
  other low-frequency controls.

The Vite React frontend lives in `admin_ui/`. Build the static console with:

```bash
cd admin_ui
npm install
npm run build
```

FastAPI serves the built console from `admin_ui/dist`. During UI development,
run `npm run dev` and use the Vite proxy to the backend.

## Recommended Providers

Recommended local, Chinese-first setup:

- VAD: `sherpa_silero`
- STT: `sherpa_sensevoice`
- TTS: `qwen3tts`
- LLM: `hermes_agent` at `http://127.0.0.1:8642/v1`

This keeps the STS service local-first while using the current Fedora + AMD
Vulkan machine for the higher-quality Qwen3TTS voice path.

`sherpa_kokoro` remains available as a stable low-latency fallback. Windows
support is kept for legacy use but is no longer the primary target.

### Fedora AMD Qwen3TTS

The default `qwen3tts` provider runs `qwentts.cpp` as an isolated C++/GGML
subprocess with `GGML_BACKEND=Vulkan0`, then converts the generated 24 kHz WAV
to the STS server's 16 kHz PCM16 stream.

Install/build the lab runtime and download the Q4 GGUF model:

```bash
# Optional: let the script install Fedora Vulkan build packages with sudo.
QWENTTS_INSTALL_SYSTEM_DEPS=1 ./scripts/qwen/setup_qwentts_lab.sh

# Re-run later without sudo once dependencies are present.
./scripts/qwen/setup_qwentts_lab.sh
```

Benchmark Qwen3TTS against the checked-in prompt set:

```bash
./scripts/qwen/bench_qwentts_lab.sh
```

Smoke-test the configured provider from `hermes-sts-server`:

```bash
./scripts/qwen/smoke_qwen3tts_provider.sh
```

Qwen3TTS voice modes in the console are mapped directly to qwentts.cpp modes:

- Default: Base model default voice. Base does not accept `--speaker` or
  `--instruct`.
- Preset: CustomVoice model with `--speaker`. Supported speakers are `serena`,
  `vivian`, `uncle_fu`, `ryan`, `aiden`, `ono_anna`, `sohee`, `eric`, and
  `dylan`.
- Design: VoiceDesign model with `--instruct` driven by a text voice
  description.
- Clone: upload a reference WAV and transcript; the server stores it in
  `data/voices/<voice_id>/` and runs `qwen-codec --talker` to create reusable
  `.spk`/`.rvq` files.

qwentts.cpp defaults to `--seed -1`, which intentionally randomizes sampling.
Hermes STS fixes the seed to `42` by default, so the same text and voice mode
stay stable across turns. Set the advanced "fixed voice seed" value to `-1`
only when you intentionally want stochastic variation.

## Control Console Roadmap

Implemented:

- AI voice workshop for persona + voice prompt + seed proposals.
- One-click save/apply of AI-generated complete roles.
- Tagged saved voice seeds with filtering and delete confirmation.
- Five-slot A/B seed audition deck.
- Dashboard waveform style switching, uptime, turn latency, and Qwen model
  readiness.

Still worth doing:

- Richer voice profile metadata such as rating, notes, last-used time, and
  favorite sorting.
- Server-side pre-render/cache for A/B candidates so five voices can be prepared
  ahead of playback.
- More runtime metrics from the Reachy client side, such as user interruption
  intent, tool latency, and robot action success/failure.
- Import/export bundles for roles, prompts, saved seed voices, and clone
  profiles.

## Windows Scripts

Windows is no longer the primary target. Legacy PowerShell and BAT helpers were
moved to `scripts/windows/` and are kept only as compatibility references.

## Fedora/Linux Setup

Install Python 3.12 and `uv`, then run:

```bash
./scripts/bootstrap_fedora_amd.sh --system
./scripts/service/start_sts_pipeline.sh
```

Then open `http://127.0.0.1:8765/` and finish configuration in the console.
On a machine where Fedora packages are already installed, omit `--system`.

Deployment boundary:

- Script stage: Fedora packages, Python venv, Kokoro/SenseVoice model download,
  qwentts.cpp build, Qwen Base/codec download, and admin UI build.
- Console stage: Hermes/LLM URL and API key, TTS engine, Qwen voice mode,
  persona prompts, saved voices, model paths, VAD, and low-frequency runtime
  tuning.

Stop the service:

```bash
./scripts/service/stop_sts_pipeline.sh
```

Install as a user-level systemd service:

```bash
./scripts/service/install_user_service.sh
systemctl --user status hermes-sts-server.service
journalctl --user -u hermes-sts-server.service -f
```

Manage it later:

```bash
systemctl --user restart hermes-sts-server.service
systemctl --user stop hermes-sts-server.service
```

Uninstall the user service:

```bash
./scripts/service/uninstall_user_service.sh
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

Prompt/persona policy: STS keeps its own base system prompt for voice brevity,
language matching, tool-use rules, and "do not speak JSON/tool
arguments/expression tags" behavior. The persona layer is explicitly selected:

```text
persona source = console     # use the server/admin UI persona, ignore WS instructions
persona source = Reachy      # use Reachy session/response instructions, fallback to settings
persona preset = operator, night_copilot, news_anchor, field_operator, baritone_male,
                 soft_companion, taiwan_sweet, quiet_cat, custom
```

Use `settings` when the admin UI should own the assistant personality. Use `ws`
when the Reachy Mini Conversation App profile should own it. TTS voice remains
locked to the admin UI setting so waiting audio and final answers stay consistent.

## Provider Configuration

Use the admin console for provider configuration. The details below describe
what the console writes into SQLite.

Main provider switches:

Recommended provider values are `sherpa_silero`, `sherpa_sensevoice`,
`qwen3tts`, and `hermes_agent`.

Qwen3TTS provider settings:

The Qwen fields in the console map to the qwentts.cpp binary, Base model,
CustomVoice model, VoiceDesign model, codec model, backend, language, voice
mode, selected speaker, voice description, clone voice id, fixed seed, and
optional extra args.

Voice source and cloning:

Use "界面控制" when the server should always use the selected console voice.
Use "跟随 WS" only when the Reachy profile should override it.

`Reachy Mini` may pass `voice` through `session.update` or `response.create`.
The server accepts either a simple speaker string or a structured object:

```json
{"voice": "vivian"}
```

```json
{
  "voice": {
    "speaker": "",
    "instruct": "",
    "ref_wav": "/path/to/reference.wav",
    "ref_text": "/path/to/reference.txt",
    "ref_spk": "/path/to/reference.spk",
    "ref_rvq": "/path/to/reference.rvq"
  }
}
```

Pre-encode a clone reference for lower per-utterance overhead:

```bash
./scripts/qwen/encode_qwen3tts_clone.sh /path/to/reference.wav
```

Waiting fillers, fallback text, and normal assistant responses all use the same
effective voice selected by `STS_TTS_VOICE_SOURCE`.

To fall back to Kokoro:

```text
STS_TTS_PROVIDER=sherpa_kokoro
```

For a no-model smoke test, switch VAD/STT/TTS to `energy`, `dev`, and `tone` in
the console.

To call a direct OpenAI-compatible model instead of Hermes Agent, switch the LLM
provider in the console and fill the base URL, model, and API key.

## Latency and Duplex Settings

The server logs one summary line per completed turn:

```text
Turn latency turn_x status=completed utterance_ms=... stt_ms=... llm_ms=... first_tts_ms=... first_audio_ms=... total_ms=...
```

Useful tuning switches live in the console's advanced settings: latency logging,
input suppression while speaking, response chunk size, TTS segment sizes,
bracketed-cue stripping, and VAD silence timing.

`STS_SUPPRESS_INPUT_WHILE_SPEAKING=true` prevents the robot from hearing its own
TTS output as a new user turn. Set it to `false` only when you need open-mic
barge-in during playback and your speaker/mic isolation is good.

`STS_TTS_STRIP_BRACKETED_CUES=true` prevents short expression cues such as
`[呲牙]` or `（笑）` from being spoken by TTS. Ordinary parenthetical content like
`（杭州）` is preserved.

## Health Checks

Fast local checks that do not need running models or a live STS server:

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
./.venv-sts/bin/python scripts/smoke/ws_smoke.py
./.venv-sts/bin/python scripts/smoke/ws_turn_smoke.py
./.venv-sts/bin/python scripts/smoke/ws_cancel_smoke.py
```

`ws_turn_smoke.py` uses generated audio and is best suited to the `energy` VAD
or `dev` provider path. Model VADs such as Silero may correctly ignore pure tone
audio because it is not speech.

Logs are written under `logs/`.

## Repository Hygiene

Do not commit:

- `.venv-sts/`
- `logs/`
- `models/`
- `*.egg-info/`
- `__pycache__/`

Large ONNX model files should be downloaded with `scripts/dev/download_models.sh`.

## Roadmap

Near-term work should keep the project reliable, local-first, and small:

1. Lightweight web search tool for direct LLM mode.
   - Preferred first provider: Tavily.
   - Expose it as a fast STS-local tool only when explicitly configured.
   - Return compact search snippets and source URLs to the LLM; do not stream
     full pages through the speech path.
   - Keep Hermes Agent mode unchanged, because Hermes may already own its own
     retrieval/tooling layer.

2. Qwen3TTS latency improvements.
   - Move from per-segment CLI subprocesses to the bundled `tts-server` or a
     small persistent process when startup cost becomes the bottleneck.
   - Keep `sherpa_kokoro` as the quick fallback for low-resource or broken-GPU
     sessions.
   - Evaluate OmniVoice only if Qwen3TTS naturalness or voice-design controls
     are not sufficient.

3. Local memory for direct LLM mode.
   - Add a small memory provider abstraction, used only when
     `STS_LLM_PROVIDER=openai_compatible` unless explicitly enabled elsewhere.
   - Start with a local file-backed store for durable user facts and session
     summaries.
   - Consider Obsidian-compatible Markdown storage as an optional backend for
     human-readable notes.
   - Prefer local libraries or simple local persistence over hosted memory
     services. Keep the default privacy-preserving and easy to inspect.
