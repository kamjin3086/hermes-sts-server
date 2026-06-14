from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from hermes_sts.config import ROOT, Settings


@dataclass(frozen=True)
class SettingSpec:
    env: str
    attr: str
    label: str
    category: str
    kind: str = "text"
    help: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    live: bool = True
    secret: bool = False


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec("HERMES_STS_HOST", "host", "STS 监听地址", "连接", help="通常保持 127.0.0.1。", live=False),
    SettingSpec("HERMES_STS_PORT", "port", "STS 端口", "连接", kind="int", minimum=1, maximum=65535, live=False),
    SettingSpec("HERMES_STS_LOG_LEVEL", "log_level", "日志等级", "连接", choices=("DEBUG", "INFO", "WARNING", "ERROR")),
    SettingSpec("HERMES_STS_SAMPLE_RATE", "sample_rate", "采样率", "连接", kind="int", choices=("16000",), live=False),
    SettingSpec("STS_LLM_PROVIDER", "llm_provider", "LLM Provider", "Hermes/LLM", choices=("hermes_agent", "openai_compatible"), live=False),
    SettingSpec("HERMES_BASE_URL", "hermes_base_url", "Hermes Base URL", "Hermes/LLM"),
    SettingSpec("HERMES_MODEL", "hermes_model", "Hermes 模型", "Hermes/LLM"),
    SettingSpec("HERMES_API_KEY", "hermes_api_key", "Hermes API Key", "Hermes/LLM", kind="password", secret=True),
    SettingSpec("HERMES_MAX_TOKENS", "hermes_max_tokens", "Hermes 最大输出 Token", "Hermes/LLM", kind="int", minimum=16, maximum=2000),
    SettingSpec("HERMES_CONNECT_TIMEOUT_SECONDS", "hermes_connect_timeout_seconds", "连接超时秒数", "Hermes/LLM", kind="float", minimum=0.5, maximum=60),
    SettingSpec("HERMES_READ_TIMEOUT_SECONDS", "hermes_read_timeout_seconds", "读取超时秒数", "Hermes/LLM", kind="float", minimum=5, maximum=600),
    SettingSpec("HERMES_AGENT_MAX_WAIT_SECONDS", "hermes_agent_max_wait_seconds", "最长等待 Hermes 秒数", "Hermes/LLM", kind="float", minimum=5, maximum=600),
    SettingSpec("HERMES_FIRST_FILLER_DELAY_SECONDS", "hermes_first_filler_delay_seconds", "首次等待语延迟", "等待/兜底", kind="float", minimum=0, maximum=30),
    SettingSpec("HERMES_FILLER_INTERVAL_SECONDS", "hermes_filler_interval_seconds", "等待语间隔", "等待/兜底", kind="float", minimum=1, maximum=120),
    SettingSpec("HERMES_MAX_FILLERS", "hermes_max_fillers", "最多等待语次数", "等待/兜底", kind="int", minimum=0, maximum=10),
    SettingSpec("HERMES_ALLOW_FALLBACK", "hermes_allow_fallback", "允许本地兜底回答", "等待/兜底", kind="bool"),
    SettingSpec("HERMES_FALLBACK_TEXTS", "hermes_fallback_texts", "兜底回答候选", "等待/兜底", kind="textarea", help="多条用 | 分隔。建议保持中文。"),
    SettingSpec("HERMES_HISTORY_MAX_MESSAGES", "hermes_history_max_messages", "本地历史最大消息数", "上下文", kind="int", minimum=0, maximum=200),
    SettingSpec("HERMES_HISTORY_MAX_CHARS", "hermes_history_max_chars", "本地历史最大字符数", "上下文", kind="int", minimum=0, maximum=100000),
    SettingSpec("STS_VAD_PROVIDER", "vad_provider", "VAD Provider", "VAD 截句", choices=("sherpa_silero", "energy"), live=False),
    SettingSpec("SHERPA_SILERO_VAD_MODEL", "sherpa_silero_vad_model", "Silero VAD 模型", "VAD 截句", live=False),
    SettingSpec("VAD_THRESHOLD", "vad_threshold", "Silero 阈值", "VAD 截句", kind="float", minimum=0.05, maximum=0.95),
    SettingSpec("VAD_MIN_SILENCE_SECONDS", "vad_min_silence_seconds", "最小静音秒数", "VAD 截句", kind="float", minimum=0.1, maximum=3),
    SettingSpec("VAD_ENERGY_THRESHOLD", "vad_energy_threshold", "能量兜底阈值", "VAD 截句", kind="float", minimum=0.001, maximum=0.2),
    SettingSpec("VAD_END_MS", "vad_end_ms", "静音结束毫秒", "VAD 截句", kind="int", minimum=100, maximum=3000),
    SettingSpec("VAD_MIN_UTTERANCE_MS", "vad_min_utterance_ms", "最短语音毫秒", "VAD 截句", kind="int", minimum=100, maximum=3000),
    SettingSpec("VAD_MAX_UTTERANCE_MS", "vad_max_utterance_ms", "最长语音毫秒", "VAD 截句", kind="int", minimum=1000, maximum=60000),
    SettingSpec("STS_STT_PROVIDER", "stt_provider", "STT Provider", "STT", choices=("sherpa_sensevoice", "funasr_onnx", "lemonade_whisper", "dev"), live=False),
    SettingSpec("SHERPA_SENSEVOICE_MODEL", "sherpa_sensevoice_model", "SenseVoice 模型", "STT", live=False),
    SettingSpec("SHERPA_SENSEVOICE_TOKENS", "sherpa_sensevoice_tokens", "SenseVoice tokens", "STT", live=False),
    SettingSpec("SHERPA_SENSEVOICE_LANGUAGE", "sherpa_sensevoice_language", "SenseVoice 语言", "STT", choices=("zh", "en", "auto")),
    SettingSpec("SHERPA_SENSEVOICE_USE_ITN", "sherpa_sensevoice_use_itn", "数字/文本规整 ITN", "STT", kind="bool"),
    SettingSpec("HERMES_STS_DEV_TRANSCRIPT", "dev_transcript", "开发转写文本", "STT"),
    SettingSpec("STS_TTS_PROVIDER", "tts_provider", "TTS Provider", "TTS 声音", choices=("sherpa_kokoro", "sapi", "tone", "sherpa_onnx"), live=False),
    SettingSpec("SHERPA_KOKORO_MODEL", "sherpa_kokoro_model", "Kokoro 模型", "TTS 声音", live=False),
    SettingSpec("SHERPA_KOKORO_VOICES", "sherpa_kokoro_voices", "Kokoro voices.bin", "TTS 声音", live=False),
    SettingSpec("SHERPA_KOKORO_TOKENS", "sherpa_kokoro_tokens", "Kokoro tokens", "TTS 声音", live=False),
    SettingSpec("SHERPA_KOKORO_LEXICON", "sherpa_kokoro_lexicon", "Kokoro 词典", "TTS 声音", live=False),
    SettingSpec("SHERPA_KOKORO_DATA_DIR", "sherpa_kokoro_data_dir", "Kokoro data dir", "TTS 声音", live=False),
    SettingSpec("SHERPA_KOKORO_VOICE", "sherpa_kokoro_voice", "Kokoro 声线 ID", "TTS 声音", kind="int", minimum=0, maximum=52, help="中文推荐 45-52，当前 47 为 zf_xiaoxiao。"),
    SettingSpec("SHERPA_KOKORO_LANG", "sherpa_kokoro_lang", "Kokoro 语言", "TTS 声音", help="通常留空自动。"),
    SettingSpec("SAPI_VOICE", "sapi_voice", "Windows SAPI 声音", "TTS 声音"),
    SettingSpec("STS_RESPONSE_AUDIO_CHUNK_MS", "response_audio_chunk_ms", "音频分片毫秒", "TTS 声音", kind="int", minimum=20, maximum=500),
    SettingSpec("STS_TTS_SEGMENT_MIN_CHARS", "tts_segment_min_chars", "TTS 最小分句字符", "TTS 声音", kind="int", minimum=4, maximum=120),
    SettingSpec("STS_TTS_SEGMENT_MAX_CHARS", "tts_segment_max_chars", "TTS 最大分句字符", "TTS 声音", kind="int", minimum=8, maximum=300),
    SettingSpec("STS_TTS_STRIP_BRACKETED_CUES", "tts_strip_bracketed_cues", "清理括号表情标签", "TTS 声音", kind="bool"),
    SettingSpec("STS_SUPPRESS_INPUT_WHILE_SPEAKING", "suppress_input_while_speaking", "说话时抑制输入", "交互", kind="bool"),
    SettingSpec("STS_LATENCY_LOGGING", "latency_logging", "记录延迟日志", "交互", kind="bool"),
)

SPECS_BY_ENV = {spec.env: spec for spec in SETTING_SPECS}

DISPLAY_SETTING_ENVS = {
    "HERMES_MAX_TOKENS",
    "HERMES_AGENT_MAX_WAIT_SECONDS",
    "HERMES_FIRST_FILLER_DELAY_SECONDS",
    "HERMES_FILLER_INTERVAL_SECONDS",
    "HERMES_MAX_FILLERS",
    "HERMES_ALLOW_FALLBACK",
    "HERMES_FALLBACK_TEXTS",
    "VAD_MIN_SILENCE_SECONDS",
    "VAD_ENERGY_THRESHOLD",
    "VAD_END_MS",
    "SHERPA_KOKORO_VOICE",
    "STS_TTS_SEGMENT_MAX_CHARS",
    "STS_SUPPRESS_INPUT_WHILE_SPEAKING",
}

UI_OVERRIDES: dict[str, dict[str, str]] = {
    "HERMES_MAX_TOKENS": {
        "category": "回答长度",
        "label": "最大输出 Token",
        "help": "想让回答更短就调低，例如 80-120；想更完整就调高。",
    },
    "HERMES_AGENT_MAX_WAIT_SECONDS": {
        "category": "等待体验",
        "label": "最长等待秒数",
        "help": "Hermes 超过这个时间仍无结果时，改用本地兜底。",
    },
    "HERMES_FIRST_FILLER_DELAY_SECONDS": {"category": "等待体验", "label": "首次等待语延迟"},
    "HERMES_FILLER_INTERVAL_SECONDS": {"category": "等待体验", "label": "等待语间隔"},
    "HERMES_MAX_FILLERS": {"category": "等待体验", "label": "最多等待语次数"},
    "HERMES_ALLOW_FALLBACK": {"category": "等待体验", "label": "允许本地兜底"},
    "HERMES_FALLBACK_TEXTS": {
        "category": "等待体验",
        "label": "兜底回答候选",
        "help": "多条用 | 分隔，建议保持中文、短句、自然。",
    },
    "VAD_MIN_SILENCE_SECONDS": {
        "category": "听写截句",
        "label": "停顿多久算说完",
        "help": "感觉反应慢就调低；经常抢话或截断就调高。",
    },
    "VAD_ENERGY_THRESHOLD": {
        "category": "听写截句",
        "label": "环境噪声阈值",
        "help": "环境吵、误触发多就调高；听不见轻声说话就调低。",
    },
    "VAD_END_MS": {
        "category": "听写截句",
        "label": "静音结束毫秒",
        "help": "能量兜底的结束等待时间，通常 500-900 比较自然。",
    },
    "SHERPA_KOKORO_VOICE": {
        "category": "声音",
        "label": "声音 ID",
        "help": "中文推荐 45-52，当前推荐 47 zf_xiaoxiao。",
    },
    "STS_TTS_SEGMENT_MAX_CHARS": {
        "category": "声音",
        "label": "每段最多字符",
        "help": "调低会更快开口但更碎；调高会更完整但首声更慢。",
    },
    "STS_SUPPRESS_INPUT_WHILE_SPEAKING": {
        "category": "交互",
        "label": "说话时忽略麦克风",
        "help": "开启可避免机器人把自己的声音听进去；想支持打断可关闭。",
    },
}


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def create_admin_router(settings: Settings, rebuild_components) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_admin_html())

    @router.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return {
            "health": _health(settings),
            "categories": _settings_payload(settings),
            "kokoro_voices": _kokoro_voice_options(),
            "env_path": str(ROOT / ".env"),
        }

    @router.patch("/api/settings")
    async def patch_settings(payload: SettingsPatch) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        restart_required: list[str] = []
        rebuild_required = False

        env_values = _read_env(ROOT / ".env")
        for env_name, raw_value in payload.values.items():
            spec = SPECS_BY_ENV.get(env_name)
            if not spec:
                continue
            try:
                value = _coerce_value(spec, raw_value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            old_value = getattr(settings, spec.attr)
            if old_value == value:
                continue
            object.__setattr__(settings, spec.attr, value)
            env_values[env_name] = _format_env_value(spec, value)
            changed[env_name] = value
            if not spec.live:
                restart_required.append(env_name)
            if env_name in {
                "STS_STT_PROVIDER",
                "STS_TTS_PROVIDER",
                "STS_LLM_PROVIDER",
                "SHERPA_KOKORO_MODEL",
                "SHERPA_KOKORO_VOICES",
                "SHERPA_KOKORO_TOKENS",
                "SHERPA_KOKORO_LEXICON",
                "SHERPA_KOKORO_DATA_DIR",
                "SHERPA_SENSEVOICE_MODEL",
                "SHERPA_SENSEVOICE_TOKENS",
            }:
                rebuild_required = True

        if changed:
            _write_env(ROOT / ".env", env_values)
            _sync_os_environ(changed)
            if rebuild_required:
                rebuild_components()

        return {
            "changed": changed,
            "restart_required": restart_required,
            "health": _health(settings),
        }

    @router.get("/api/logs")
    async def logs(lines: int = 120) -> dict[str, str]:
        safe_lines = max(20, min(lines, 500))
        return {
            "stdout": _tail(settings.log_dir / "sts-server.out.log", safe_lines),
            "stderr": _tail(settings.log_dir / "sts-server.err.log", safe_lines),
        }

    @router.get("/api/diagnostics/hermes")
    async def hermes_diagnostics() -> dict[str, Any]:
        headers = {}
        if settings.hermes_api_key:
            headers["Authorization"] = f"Bearer {settings.hermes_api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{settings.hermes_base_url.rstrip('/')}/models", headers=headers)
                return {"ok": resp.is_success, "status_code": resp.status_code, "body": resp.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return router


def _settings_payload(settings: Settings) -> list[dict[str, Any]]:
    categories: dict[str, list[dict[str, Any]]] = {}
    for spec in SETTING_SPECS:
        if spec.env not in DISPLAY_SETTING_ENVS:
            continue
        override = UI_OVERRIDES.get(spec.env, {})
        value = getattr(settings, spec.attr)
        category = override.get("category", spec.category)
        categories.setdefault(category, []).append(
            {
                "env": spec.env,
                "label": override.get("label", spec.label),
                "kind": spec.kind,
                "value": value,
                "help": override.get("help", spec.help),
                "choices": list(spec.choices),
                "min": spec.minimum,
                "max": spec.maximum,
                "live": spec.live,
                "secret": spec.secret,
            }
        )
    order = ["回答长度", "等待体验", "听写截句", "声音", "交互"]
    return [{"name": name, "settings": categories[name]} for name in order if name in categories]


def _health(settings: Settings) -> dict[str, Any]:
    return {
        "status": "ok",
        "sample_rate": settings.sample_rate,
        "vad_provider": settings.vad_provider,
        "stt_provider": settings.stt_provider,
        "tts_provider": settings.tts_provider,
        "llm_provider": settings.llm_provider,
        "hermes_base_url": settings.hermes_base_url,
        "hermes_model": settings.hermes_model,
        "voice": settings.sherpa_kokoro_voice,
    }


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for spec in SETTING_SPECS:
        if spec.env in values and spec.env not in seen:
            output.append(f"{spec.env}={values[spec.env]}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _sync_os_environ(changed: dict[str, Any]) -> None:
    for env_name, value in changed.items():
        spec = SPECS_BY_ENV[env_name]
        os.environ[env_name] = _format_env_value(spec, value)


def _coerce_value(spec: SettingSpec, value: Any) -> Any:
    if spec.kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if spec.kind == "int":
        coerced = int(value)
    elif spec.kind == "float":
        coerced = float(value)
    else:
        coerced = str(value).strip()

    if isinstance(coerced, (int, float)):
        if spec.minimum is not None and coerced < spec.minimum:
            raise ValueError(f"{spec.env} must be >= {spec.minimum}")
        if spec.maximum is not None and coerced > spec.maximum:
            raise ValueError(f"{spec.env} must be <= {spec.maximum}")
    return coerced


def _format_env_value(spec: SettingSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "true" if value else "false"
    return str(value)


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _kokoro_voice_options() -> list[dict[str, Any]]:
    return [
        {"id": 45, "name": "zf_xiaobei", "lang": "zh", "note": "中文女声"},
        {"id": 46, "name": "zf_xiaoni", "lang": "zh", "note": "中文女声"},
        {"id": 47, "name": "zf_xiaoxiao", "lang": "zh", "note": "当前推荐，较自然"},
        {"id": 48, "name": "zf_xiaoyi", "lang": "zh", "note": "中文女声"},
        {"id": 49, "name": "zm_yunjian", "lang": "zh", "note": "中文男声"},
        {"id": 50, "name": "zm_yunxi", "lang": "zh", "note": "中文男声"},
        {"id": 51, "name": "zm_yunxia", "lang": "zh", "note": "中文男声"},
        {"id": 52, "name": "zm_yunyang", "lang": "zh", "note": "中文男声"},
    ]


def _admin_html() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes STS 控制台</title>
  <style>{_admin_css()}</style>
</head>
<body>
  <header>
    <div>
      <h1>Hermes STS 控制台</h1>
      <p id="subtitle">Reachy Mini Lite 本地语音链路参数面板</p>
    </div>
    <div class="status">
      <span id="statusDot"></span>
      <span id="healthText">加载中</span>
    </div>
  </header>
  <main>
    <aside class="nav">
      <button class="tab active" data-tab="settings">设置</button>
      <button class="tab" data-tab="voices">声线</button>
      <button class="tab" data-tab="diagnostics">诊断</button>
      <button class="tab" data-tab="logs">日志</button>
    </aside>
    <section id="content"></section>
  </main>
  <div id="toast"></div>
  <script>{_admin_js()}</script>
</body>
</html>"""


def _admin_css() -> str:
    return """
:root { color-scheme: light; --bg:#f6f7f9; --panel:#ffffff; --text:#1d2430; --muted:#667085; --line:#d8dee8; --soft:#eef3f8; --accent:#2563eb; --ok:#13976b; --warn:#b7791f; --bad:#c2410c; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--text); background: var(--bg); }
header { min-height: 76px; display:flex; align-items:center; justify-content:space-between; padding: 16px 28px; border-bottom:1px solid var(--line); background: var(--panel); }
h1 { margin:0; font-size:21px; letter-spacing:0; }
p { margin:4px 0 0; color:var(--muted); }
.status { display:flex; gap:8px; align-items:center; font-weight:600; }
#statusDot { width:10px; height:10px; border-radius:50%; background:var(--warn); }
main { display:grid; grid-template-columns: 168px minmax(0, 1fr); min-height: calc(100vh - 76px); }
.nav { padding:18px 12px; border-right:1px solid var(--line); background:#eef2f7; }
.tab { width:100%; height:38px; border:0; background:transparent; color:var(--text); text-align:left; padding:0 12px; border-radius:6px; cursor:pointer; font-weight:600; }
.tab.active, .tab:hover { background:#dfe7f2; }
#content { padding:22px 28px 36px; max-width:1180px; }
.toolbar { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; margin-bottom:16px; }
.toolbar h2 { margin:0; font-size:20px; }
.toolbar p { max-width:720px; }
.actions { display:flex; gap:10px; flex-wrap:wrap; }
button.primary, button.secondary { height:36px; border-radius:6px; padding:0 14px; font-weight:700; cursor:pointer; }
button.primary { border:1px solid var(--accent); background:var(--accent); color:white; }
button.secondary { border:1px solid var(--line); background:white; color:var(--text); }
.overview { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; margin-bottom:16px; }
.metric { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; min-height:72px; }
.metricLabel { color:var(--muted); font-size:12px; font-weight:700; margin-bottom:6px; }
.metricValue { font-size:15px; font-weight:800; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.metricHint { color:var(--muted); font-size:12px; margin-top:3px; }
.settingsLayout { display:grid; grid-template-columns: minmax(0, 1fr) 260px; gap:16px; align-items:start; }
.grid { display:flex; flex-direction:column; gap:10px; }
.sidePanel { position:sticky; top:18px; background:var(--soft); border:1px solid var(--line); border-radius:8px; padding:14px; }
.sidePanel h3 { margin:0 0 8px; font-size:14px; }
.sidePanel p { font-size:12px; margin:0 0 10px; }
.sidePanel ul { margin:0; padding-left:18px; color:var(--muted); font-size:12px; }
.group { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:0; overflow:hidden; }
.group summary { list-style:none; cursor:pointer; padding:14px 16px; font-size:15px; font-weight:800; display:grid; grid-template-columns: 1fr auto auto; gap:12px; align-items:center; }
.group summary::-webkit-details-marker { display:none; }
.group summary::after { content:"展开"; color:var(--muted); font-size:12px; font-weight:700; }
.group[open] summary { border-bottom:1px solid #edf0f5; }
.group[open] summary::after { content:"收起"; }
.groupCount { color:var(--muted); font-size:12px; font-weight:700; }
.groupBody { padding:6px 16px 14px; }
.field { display:grid; grid-template-columns: minmax(180px, 260px) minmax(220px, 1fr); gap:14px; align-items:center; padding:12px 0; border-top:1px solid #edf0f5; }
.field:first-of-type { border-top:0; }
.fieldMeta { min-width:0; }
label { display:block; font-weight:750; padding:0; }
.control { min-width:0; }
input, select, textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:8px 9px; font:inherit; background:white; color:var(--text); }
input[type=checkbox] { width:20px; height:20px; margin:0; accent-color:var(--accent); }
textarea { min-height:96px; resize:vertical; }
.help { margin-top:4px; color:var(--muted); font-size:12px; }
.envName { margin-top:4px; color:#98a2b3; font-size:11px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
.boolControl { display:flex; justify-content:flex-start; align-items:center; min-height:38px; }
.pill { display:inline-block; margin-left:6px; padding:2px 6px; border-radius:999px; font-size:11px; background:#eef2ff; color:#3447a0; }
.pill.restart { background:#fff7ed; color:#9a3412; }
.cards { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }
.voice { background:white; border:1px solid var(--line); border-radius:8px; padding:14px; }
.voice strong { display:block; margin-bottom:4px; }
.voice.current { outline:2px solid var(--accent); }
pre { margin:0; padding:14px; background:#101828; color:#f2f4f7; border-radius:8px; overflow:auto; max-height:520px; white-space:pre-wrap; }
.diag { background:white; border:1px solid var(--line); border-radius:8px; padding:16px; margin-bottom:12px; }
#toast { position:fixed; right:20px; bottom:20px; min-width:220px; max-width:420px; padding:12px 14px; border-radius:8px; background:#1d2939; color:white; opacity:0; transform:translateY(8px); transition:160ms; pointer-events:none; }
#toast.show { opacity:1; transform:translateY(0); }
@media (max-width: 980px) { .overview { grid-template-columns: repeat(2, minmax(0, 1fr)); } .settingsLayout { grid-template-columns:1fr; } .sidePanel { position:static; } }
@media (max-width: 760px) { header { height:auto; padding:14px 16px; align-items:flex-start; gap:10px; flex-direction:column; } main { grid-template-columns:1fr; } .nav { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:8px; overflow:auto; border-right:0; border-bottom:1px solid var(--line); padding:12px; } .tab { min-width:0; text-align:center; } #content { padding:16px; } .overview { grid-template-columns:repeat(2, minmax(0, 1fr)); } .toolbar { flex-direction:column; } .field { grid-template-columns:1fr; gap:8px; } }
@media (max-width: 520px) { .overview { grid-template-columns:1fr; } .nav { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
"""


def _admin_js() -> str:
    return r"""
const state = { data: null, tab: "settings", dirty: new Map() };
const content = document.getElementById("content");
const toast = document.getElementById("toast");

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    btn.classList.add("active");
    state.tab = btn.dataset.tab;
    render();
  });
});

function showToast(text) {
  toast.textContent = text;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2600);
}

async function load() {
  const res = await fetch("/api/settings");
  state.data = await res.json();
  updateHealth(state.data.health);
  render();
}

function updateHealth(health) {
  document.getElementById("statusDot").style.background = health.status === "ok" ? "var(--ok)" : "var(--bad)";
  document.getElementById("healthText").textContent =
    `${health.vad_provider} / ${health.stt_provider} / ${health.tts_provider} / ${health.hermes_model}`;
}

function render() {
  if (!state.data) return;
  if (state.tab === "settings") renderSettings();
  if (state.tab === "voices") renderVoices();
  if (state.tab === "diagnostics") renderDiagnostics();
  if (state.tab === "logs") renderLogs();
}

function renderSettings() {
  const health = state.data.health;
  content.innerHTML = `
    <div class="toolbar">
      <div>
        <h2>常用设置</h2>
        <p>只保留会影响对话体验的少数参数。按体验目标展开一组调整，保存后会写入 .env。</p>
      </div>
      <div class="actions">
        <button class="secondary" id="reload">重新读取</button>
        <button class="primary" id="save">保存并应用</button>
      </div>
    </div>
    <div class="overview">
      ${renderMetric("语音链路", `${health.vad_provider} / ${health.stt_provider}`, health.tts_provider)}
      ${renderMetric("Hermes", health.hermes_model, health.hermes_base_url)}
      ${renderMetric("当前声线", `ID ${health.voice}`, voiceName(health.voice))}
      ${renderMetric("设置项", `${state.data.categories.reduce((sum, group) => sum + group.settings.length, 0)} 项`, "低频项保留在 .env")}
    </div>
    <div class="settingsLayout">
      <div class="grid">
        ${state.data.categories.map(renderGroup).join("")}
      </div>
      <aside class="sidePanel">
        <h3>调参建议</h3>
        <p>先按实际问题找分组，避免一次改太多。</p>
        <ul>
          <li>反应慢：看“听写截句”和“等待体验”。</li>
          <li>声音不自然：看“声音”。</li>
          <li>经常自我打断：看“交互”。</li>
          <li>回答太长：看“回答长度”。</li>
        </ul>
      </aside>
    </div>`;
  document.getElementById("reload").onclick = () => { state.dirty.clear(); load(); };
  document.getElementById("save").onclick = saveSettings;
  bindInputs();
}

function renderMetric(label, value, hint) {
  return `<div class="metric">
    <div class="metricLabel">${escapeHtml(label)}</div>
    <div class="metricValue" title="${escapeAttr(value)}">${escapeHtml(value)}</div>
    <div class="metricHint" title="${escapeAttr(hint)}">${escapeHtml(hint || "")}</div>
  </div>`;
}

function renderGroup(group) {
  return `<details class="group">
    <summary><span>${escapeHtml(group.name)}</span><span class="groupCount">${group.settings.length} 项</span></summary>
    <div class="groupBody">
    ${group.settings.map(renderField).join("")}
    </div>
  </details>`;
}

function renderField(item) {
  const value = state.dirty.has(item.env) ? state.dirty.get(item.env) : item.value;
  const badge = item.live ? '<span class="pill">即时</span>' : '<span class="pill restart">需重启/重建</span>';
  let input = "";
  if (item.kind === "bool") {
    input = `<div class="boolControl"><input data-env="${item.env}" data-kind="${item.kind}" type="checkbox" ${value ? "checked" : ""}></div>`;
  } else if (item.choices && item.choices.length) {
    input = `<select data-env="${item.env}" data-kind="${item.kind}">${item.choices.map(choice => `<option value="${escapeAttr(choice)}" ${String(value) === String(choice) ? "selected" : ""}>${escapeHtml(choice)}</option>`).join("")}</select>`;
  } else if (item.kind === "textarea") {
    input = `<textarea data-env="${item.env}" data-kind="${item.kind}">${escapeHtml(value ?? "")}</textarea>`;
  } else {
    const type = item.kind === "password" ? "password" : item.kind === "int" || item.kind === "float" ? "number" : "text";
    const step = item.kind === "float" ? "0.1" : "1";
    input = `<input data-env="${item.env}" data-kind="${item.kind}" type="${type}" step="${step}" value="${escapeAttr(value ?? "")}">`;
  }
  return `<div class="field">
    <div class="fieldMeta">
      <label>${escapeHtml(item.label)} ${badge}</label>
      ${item.help ? `<div class="help">${escapeHtml(item.help)}</div>` : ""}
      <div class="envName">${escapeHtml(item.env)}</div>
    </div>
    <div class="control">
      ${input}
    </div>
  </div>`;
}

function voiceName(id) {
  const item = state.data.kokoro_voices.find(v => v.id === id);
  return item ? item.name : "未命名声线";
}

function bindInputs() {
  content.querySelectorAll("[data-env]").forEach(input => {
    input.addEventListener("input", () => {
      const kind = input.dataset.kind;
      let value = kind === "bool" ? input.checked : input.value;
      if (kind === "int") value = Number.parseInt(value || "0", 10);
      if (kind === "float") value = Number.parseFloat(value || "0");
      state.dirty.set(input.dataset.env, value);
    });
  });
}

async function saveSettings() {
  if (!state.dirty.size) { showToast("没有需要保存的改动"); return; }
  const values = Object.fromEntries(state.dirty.entries());
  const res = await fetch("/api/settings", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ values })
  });
  if (!res.ok) { showToast("保存失败"); return; }
  const result = await res.json();
  state.dirty.clear();
  await load();
  const restart = result.restart_required?.length ? `；这些项建议重启服务：${result.restart_required.join(", ")}` : "";
  showToast(`已保存 ${Object.keys(result.changed).length} 项${restart}`);
}

function renderVoices() {
  const current = state.data.health.voice;
  content.innerHTML = `
    <div class="toolbar"><h2>Kokoro 中文声线</h2><div class="actions"><button class="secondary" id="reload">刷新</button></div></div>
    <div class="cards">${state.data.kokoro_voices.map(v => `
      <div class="voice ${v.id === current ? "current" : ""}">
        <strong>${v.id} · ${escapeHtml(v.name)}</strong>
        <div>${escapeHtml(v.note)}</div>
        <button class="secondary" data-voice="${v.id}">设为当前声线</button>
      </div>`).join("")}</div>`;
  document.getElementById("reload").onclick = load;
  content.querySelectorAll("[data-voice]").forEach(btn => {
    btn.onclick = async () => {
      state.dirty.set("SHERPA_KOKORO_VOICE", Number.parseInt(btn.dataset.voice, 10));
      await saveSettings();
    };
  });
}

async function renderDiagnostics() {
  content.innerHTML = `<div class="toolbar"><h2>诊断</h2><div class="actions"><button class="secondary" id="checkHermes">检查 Hermes</button></div></div><div id="diag"></div>`;
  const diag = document.getElementById("diag");
  diag.innerHTML = `<div class="diag"><strong>当前链路</strong><p>${escapeHtml(JSON.stringify(state.data.health))}</p><p>.env：${escapeHtml(state.data.env_path)}</p></div>`;
  document.getElementById("checkHermes").onclick = async () => {
    diag.innerHTML = `<div class="diag">正在检查 Hermes /models ...</div>`;
    const res = await fetch("/api/diagnostics/hermes");
    const data = await res.json();
    diag.innerHTML = `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
  };
}

async function renderLogs() {
  content.innerHTML = `<div class="toolbar"><h2>日志</h2><div class="actions"><button class="secondary" id="refreshLogs">刷新日志</button></div></div><pre id="logBox">加载中</pre>`;
  document.getElementById("refreshLogs").onclick = loadLogs;
  await loadLogs();
}

async function loadLogs() {
  const res = await fetch("/api/logs?lines=160");
  const data = await res.json();
  document.getElementById("logBox").textContent = `STDERR\n${data.stderr || "(empty)"}\n\nSTDOUT\n${data.stdout || "(empty)"}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[ch]));
}
function escapeAttr(value) { return escapeHtml(value); }

load().catch(err => {
  content.innerHTML = `<pre>${escapeHtml(err.stack || err.message || err)}</pre>`;
});
"""
