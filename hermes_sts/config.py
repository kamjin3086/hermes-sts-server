from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    return float(raw)


def _path_env(name: str, default: str = "") -> str:
    raw = os.getenv(name, default).strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path)


def _path_list_env(name: str, default: str = "") -> str:
    raw = os.getenv(name, default).strip()
    if not raw:
        return ""
    resolved = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ROOT / path
        resolved.append(str(path))
    return ",".join(resolved)


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("HERMES_STS_HOST", "127.0.0.1")
    port: int = _int_env("HERMES_STS_PORT", 8765)
    log_level: str = os.getenv("HERMES_STS_LOG_LEVEL", "INFO")
    sample_rate: int = _int_env("HERMES_STS_SAMPLE_RATE", 16000)

    hermes_base_url: str = os.getenv("HERMES_BASE_URL", "http://127.0.0.1:8642/v1")
    hermes_model: str = os.getenv("HERMES_MODEL", "hermes-agent")
    hermes_api_key: str = os.getenv("HERMES_API_KEY", "")
    hermes_max_tokens: int = _int_env("HERMES_MAX_TOKENS", 220)
    hermes_timeout_seconds: float = _float_env("HERMES_TIMEOUT_SECONDS", 45.0)
    hermes_connect_timeout_seconds: float = _float_env("HERMES_CONNECT_TIMEOUT_SECONDS", 3.0)
    hermes_read_timeout_seconds: float = _float_env("HERMES_READ_TIMEOUT_SECONDS", hermes_timeout_seconds)
    hermes_agent_max_wait_seconds: float = _float_env("HERMES_AGENT_MAX_WAIT_SECONDS", 60.0)
    hermes_first_filler_delay_seconds: float = _float_env("HERMES_FIRST_FILLER_DELAY_SECONDS", 3.0)
    hermes_filler_interval_seconds: float = _float_env("HERMES_FILLER_INTERVAL_SECONDS", 12.0)
    hermes_max_fillers: int = _int_env("HERMES_MAX_FILLERS", 1)
    hermes_allow_fallback: bool = _bool_env("HERMES_ALLOW_FALLBACK", True)
    hermes_fallback_text: str = os.getenv(
        "HERMES_FALLBACK_TEXT",
        "我这边还在等 Hermes 的结果，语音链路本身是正常的。你可以再说一遍，或者稍等我继续处理。",
    )
    hermes_fallback_texts: str = os.getenv("HERMES_FALLBACK_TEXTS", "")
    hermes_history_max_messages: int = _int_env("HERMES_HISTORY_MAX_MESSAGES", 300)
    hermes_history_max_chars: int = _int_env("HERMES_HISTORY_MAX_CHARS", 65536)
    hermes_history_idle_reset_seconds: float = _float_env("HERMES_HISTORY_IDLE_RESET_SECONDS", 21600.0)
    # 21600s = 6h (was 14400/4h). 0=never auto-arch on idle.
    sts_conversations_enabled: bool = os.environ.get("STS_CONVERSATIONS_ENABLED", "true").lower() in ("1", "true", "yes")
    sts_conversations_db_path: str = os.environ.get("STS_CONVERSATIONS_DB_PATH", "data/hermes_sts.sqlite3")
    sts_conversations_reload_max_messages: int = int(os.environ.get("STS_CONVERSATIONS_RELOAD_MAX_MESSAGES", "0"))
    hermes_voice_no_think: bool = _bool_env("HERMES_VOICE_NO_THINK", True)
    llm_provider: str = os.getenv("STS_LLM_PROVIDER", "hermes_agent")
    llm_max_concurrent_requests: int = _int_env("STS_LLM_MAX_CONCURRENT_REQUESTS", 1)
    llm_base_url: str = os.getenv("LLM_BASE_URL", os.getenv("HERMES_BASE_URL", "http://127.0.0.1:8642/v1"))
    llm_model: str = os.getenv("LLM_MODEL", os.getenv("HERMES_MODEL", "hermes-agent"))
    llm_api_key: str = os.getenv("LLM_API_KEY", os.getenv("HERMES_API_KEY", ""))
    llm_max_tokens: int = _int_env("LLM_MAX_TOKENS", _int_env("HERMES_MAX_TOKENS", 220))
    llm_timeout_seconds: float = _float_env("LLM_TIMEOUT_SECONDS", _float_env("HERMES_TIMEOUT_SECONDS", 45.0))
    llm_fallback_enabled: bool = _bool_env("LLM_FALLBACK_ENABLED", True)
    llm_fallback_base_url: str = os.getenv("LLM_FALLBACK_BASE_URL", "")
    llm_fallback_model: str = os.getenv("LLM_FALLBACK_MODEL", "")
    llm_fallback_api_key: str = os.getenv("LLM_FALLBACK_API_KEY", "")
    llm_fallback_timeout_seconds: float = _float_env("LLM_FALLBACK_TIMEOUT_SECONDS", 180.0)
    llm_fallback_max_tokens: int = _int_env("LLM_FALLBACK_MAX_TOKENS", 160)
    sts_persona_source: str = os.getenv("STS_PERSONA_SOURCE", "settings")
    sts_persona_preset: str = os.getenv("STS_PERSONA_PRESET", "operator")
    sts_persona_custom: str = os.getenv("STS_PERSONA_CUSTOM", "")

    stt_provider: str = os.getenv("STS_STT_PROVIDER", "dev")
    dev_transcript: str = os.getenv("HERMES_STS_DEV_TRANSCRIPT", "hello")
    funasr_model_dir: str = _path_env("FUNASR_MODEL_DIR")
    funasr_quantize: bool = _bool_env("FUNASR_QUANTIZE", False)
    lemonade_base_url: str = os.getenv("LEMONADE_BASE_URL", "http://127.0.0.1:13305/api/v1")
    lemonade_api_key: str = os.getenv("LEMONADE_API_KEY", "nopass")
    lemonade_stt_model: str = os.getenv("LEMONADE_STT_MODEL", "Whisper-Large-v3-Turbo")
    lemonade_stt_language: str = os.getenv("LEMONADE_STT_LANGUAGE", "zh")
    lemonade_stt_timeout_seconds: float = _float_env("LEMONADE_STT_TIMEOUT_SECONDS", 60.0)
    sherpa_sensevoice_model: str = _path_env("SHERPA_SENSEVOICE_MODEL")
    sherpa_sensevoice_tokens: str = _path_env("SHERPA_SENSEVOICE_TOKENS")
    sherpa_sensevoice_language: str = os.getenv("SHERPA_SENSEVOICE_LANGUAGE", "zh")
    sherpa_sensevoice_use_itn: bool = _bool_env("SHERPA_SENSEVOICE_USE_ITN", True)

    tts_provider: str = os.getenv("STS_TTS_PROVIDER", "qwen3tts")
    sapi_voice: str = os.getenv("SAPI_VOICE", "")
    sherpa_tts_model: str = _path_env("SHERPA_TTS_MODEL")
    sherpa_tts_tokens: str = _path_env("SHERPA_TTS_TOKENS")
    sherpa_tts_data_dir: str = _path_env("SHERPA_TTS_DATA_DIR")
    sherpa_kokoro_model: str = _path_env("SHERPA_KOKORO_MODEL")
    sherpa_kokoro_voices: str = _path_env("SHERPA_KOKORO_VOICES")
    sherpa_kokoro_tokens: str = _path_env("SHERPA_KOKORO_TOKENS")
    sherpa_kokoro_lexicon: str = _path_list_env("SHERPA_KOKORO_LEXICON")
    sherpa_kokoro_data_dir: str = _path_env("SHERPA_KOKORO_DATA_DIR")
    sherpa_kokoro_voice: int = _int_env("SHERPA_KOKORO_VOICE", 0)
    sherpa_kokoro_lang: str = os.getenv("SHERPA_KOKORO_LANG", "")
    tts_voice_source: str = os.getenv("STS_TTS_VOICE_SOURCE", "settings")
    qwentts_cpp_bin: str = _path_env(
        "QWENTTS_CPP_BIN",
        "../hermes-tts-lab/src/qwentts.cpp/build/qwen-tts",
    )
    qwentts_cpp_model: str = _path_env(
        "QWENTTS_CPP_MODEL",
        "../hermes-tts-lab/models/qwen-talker-1.7b-base-Q4_K_M.gguf",
    )
    qwentts_cpp_codec: str = _path_env(
        "QWENTTS_CPP_CODEC",
        "../hermes-tts-lab/models/qwen-tokenizer-12hz-Q4_K_M.gguf",
    )
    qwentts_cpp_base_model: str = _path_env(
        "QWENTTS_CPP_BASE_MODEL",
        "../hermes-tts-lab/models/qwen-talker-1.7b-base-Q4_K_M.gguf",
    )
    qwentts_cpp_customvoice_model: str = _path_env(
        "QWENTTS_CPP_CUSTOMVOICE_MODEL",
        "../hermes-tts-lab/models/qwen-talker-1.7b-customvoice-Q4_K_M.gguf",
    )
    qwentts_cpp_voicedesign_model: str = _path_env(
        "QWENTTS_CPP_VOICEDESIGN_MODEL",
        "../hermes-tts-lab/models/qwen-talker-1.7b-voicedesign-Q4_K_M.gguf",
    )
    qwentts_cpp_voice_mode: str = os.getenv("QWENTTS_CPP_VOICE_MODE", "default")
    qwentts_cpp_voice_preset: str = os.getenv("QWENTTS_CPP_VOICE_PRESET", "")
    qwentts_cpp_voice_design: str = os.getenv("QWENTTS_CPP_VOICE_DESIGN", "")
    qwentts_cpp_clone_voice_id: str = os.getenv("QWENTTS_CPP_CLONE_VOICE_ID", "")
    qwentts_cpp_backend: str = os.getenv("QWENTTS_CPP_BACKEND", "Vulkan0")
    qwentts_cpp_lang: str = os.getenv("QWENTTS_CPP_LANG", "Chinese")
    qwentts_cpp_speaker: str = os.getenv("QWENTTS_CPP_SPEAKER", "")
    qwentts_cpp_instruct: str = os.getenv("QWENTTS_CPP_INSTRUCT", "")
    qwentts_cpp_ref_wav: str = _path_env("QWENTTS_CPP_REF_WAV")
    qwentts_cpp_ref_text: str = _path_env("QWENTTS_CPP_REF_TEXT")
    qwentts_cpp_ref_spk: str = _path_env("QWENTTS_CPP_REF_SPK")
    qwentts_cpp_ref_rvq: str = _path_env("QWENTTS_CPP_REF_RVQ")
    qwentts_cpp_format: str = os.getenv("QWENTTS_CPP_FORMAT", "wav16")
    qwentts_cpp_extra_args: str = os.getenv("QWENTTS_CPP_EXTRA_ARGS", "")
    qwentts_cpp_seed: int = _int_env("QWENTTS_CPP_SEED", 42)
    qwentts_cpp_timeout_seconds: float = _float_env("QWENTTS_CPP_TIMEOUT_SECONDS", 120.0)

    vad_provider: str = os.getenv("STS_VAD_PROVIDER", "energy")
    vad_energy_threshold: float = _float_env("VAD_ENERGY_THRESHOLD", 0.004)
    vad_start_ms: int = _int_env("VAD_START_MS", 120)
    vad_end_ms: int = _int_env("VAD_END_MS", 600)
    vad_min_utterance_ms: int = _int_env("VAD_MIN_UTTERANCE_MS", 300)
    vad_max_utterance_ms: int = _int_env("VAD_MAX_UTTERANCE_MS", 12000)
    sherpa_silero_vad_model: str = _path_env("SHERPA_SILERO_VAD_MODEL")
    vad_min_silence_seconds: float = _float_env("VAD_MIN_SILENCE_SECONDS", 0.45)
    vad_buffer_seconds: float = _float_env("VAD_BUFFER_SECONDS", 30.0)
    vad_threshold: float = _float_env("VAD_THRESHOLD", 0.35)
    max_audio_chunk_bytes: int = _int_env("HERMES_STS_MAX_AUDIO_CHUNK_BYTES", 32000)
    suppress_input_while_speaking: bool = _bool_env("STS_SUPPRESS_INPUT_WHILE_SPEAKING", True)
    post_speak_cooldown_ms: int = _int_env("STS_POST_SPEAK_COOLDOWN_MS", 500)
    response_audio_chunk_ms: int = _int_env("STS_RESPONSE_AUDIO_CHUNK_MS", 80)
    tts_segment_min_chars: int = _int_env("STS_TTS_SEGMENT_MIN_CHARS", 24)
    tts_segment_max_chars: int = _int_env("STS_TTS_SEGMENT_MAX_CHARS", 90)
    tts_strip_bracketed_cues: bool = _bool_env("STS_TTS_STRIP_BRACKETED_CUES", True)
    latency_logging: bool = _bool_env("STS_LATENCY_LOGGING", True)
    dashboard_wave_style: str = os.getenv("DASHBOARD_WAVE_STYLE", "scanner")

    memory_enabled: bool = _bool_env("STS_MEMORY_ENABLED", False)
    memory_provider: str = os.getenv("STS_MEMORY_PROVIDER", "sqlite")  # sqlite | openviking | noop
    memory_remember_in_hermes: bool = _bool_env("STS_MEMORY_REMEMBER_IN_HERMES", True)
    memory_injection_budget: int = _int_env("STS_MEMORY_INJECTION_BUDGET", 500)
    memory_recall_limit: int = _int_env("STS_MEMORY_RECALL_LIMIT", 5)
    memory_recall_min_score: float = _float_env("STS_MEMORY_RECALL_MIN_SCORE", 0.0)
    memory_commit_interval_turns: int = _int_env("STS_MEMORY_COMMIT_INTERVAL_TURNS", 10)
    memory_commit_idle_seconds: float = _float_env("STS_MEMORY_COMMIT_IDLE_SECONDS", 300.0)
    memory_extract_enabled: bool = _bool_env("STS_MEMORY_EXTRACT_ENABLED", True)
    memory_extract_max_per_turn: int = _int_env("STS_MEMORY_EXTRACT_MAX_PER_TURN", 2)
    openviking_base_url: str = os.getenv("OPENVIKING_BASE_URL", "http://127.0.0.1:1933")
    openviking_api_key: str = os.getenv("OPENVIKING_API_KEY", "")
    openviking_account: str = os.getenv("OPENVIKING_ACCOUNT", "default")
    openviking_user: str = os.getenv("OPENVIKING_USER", "reachy")
    openviking_target_uri: str = os.getenv("OPENVIKING_TARGET_URI", "viking://user/memories/")
    openviking_timeout_seconds: float = _float_env("OPENVIKING_TIMEOUT_SECONDS", 6.0)
    openviking_commit_timeout_seconds: float = _float_env("OPENVIKING_COMMIT_TIMEOUT_SECONDS", 30.0)
    sqlite_memory_path: str = _path_env("STS_SQLITE_MEMORY_PATH", "data/memory.sqlite3")
    web_search_enabled: bool = _bool_env("STS_WEB_SEARCH_ENABLED", False)
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")
    tavily_search_depth: str = os.getenv("TAVILY_SEARCH_DEPTH", "ultra-fast")
    tavily_max_results: int = _int_env("TAVILY_MAX_RESULTS", 3)
    tavily_timeout_seconds: float = _float_env("TAVILY_TIMEOUT_SECONDS", 2.0)
    tavily_base_url: str = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com")

    models_dir: Path = Path(_path_env("MODELS_DIR", str(ROOT / "models")))
    log_dir: Path = Path(_path_env("LOG_DIR", str(ROOT / "logs")))
    data_dir: Path = Path(_path_env("DATA_DIR", str(ROOT / "data")))
    config_db: Path = Path(_path_env("HERMES_STS_CONFIG_DB", str(ROOT / "data" / "hermes_sts.sqlite3")))


def load_settings() -> Settings:
    from hermes_sts.config_store import ConfigStore

    return ConfigStore.default().load_settings()


settings = load_settings()
