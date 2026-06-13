from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env", override=False)


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
    hermes_agent_max_wait_seconds: float = _float_env("HERMES_AGENT_MAX_WAIT_SECONDS", 240.0)
    hermes_first_filler_delay_seconds: float = _float_env("HERMES_FIRST_FILLER_DELAY_SECONDS", 1.2)
    hermes_filler_interval_seconds: float = _float_env("HERMES_FILLER_INTERVAL_SECONDS", 8.0)
    hermes_max_fillers: int = _int_env("HERMES_MAX_FILLERS", 2)
    hermes_allow_fallback: bool = _bool_env("HERMES_ALLOW_FALLBACK", True)
    hermes_fallback_text: str = os.getenv(
        "HERMES_FALLBACK_TEXT",
        "我这边还在等 Hermes 的结果，语音链路本身是正常的。你可以再说一遍，或者稍等我继续处理。",
    )
    hermes_fallback_texts: str = os.getenv("HERMES_FALLBACK_TEXTS", "")
    hermes_history_max_messages: int = _int_env("HERMES_HISTORY_MAX_MESSAGES", 40)
    hermes_history_max_chars: int = _int_env("HERMES_HISTORY_MAX_CHARS", 12000)
    llm_fallback_enabled: bool = _bool_env("LLM_FALLBACK_ENABLED", True)
    llm_fallback_base_url: str = os.getenv("LLM_FALLBACK_BASE_URL", "")
    llm_fallback_model: str = os.getenv("LLM_FALLBACK_MODEL", "")
    llm_fallback_api_key: str = os.getenv("LLM_FALLBACK_API_KEY", "")
    llm_fallback_timeout_seconds: float = _float_env("LLM_FALLBACK_TIMEOUT_SECONDS", 180.0)
    llm_fallback_max_tokens: int = _int_env("LLM_FALLBACK_MAX_TOKENS", 160)

    stt_provider: str = os.getenv("STS_STT_PROVIDER", "dev")
    dev_transcript: str = os.getenv("HERMES_STS_DEV_TRANSCRIPT", "hello")
    funasr_model_dir: str = os.getenv("FUNASR_MODEL_DIR", "")
    funasr_quantize: bool = _bool_env("FUNASR_QUANTIZE", False)
    lemonade_base_url: str = os.getenv("LEMONADE_BASE_URL", "http://127.0.0.1:13305/api/v1")
    lemonade_api_key: str = os.getenv("LEMONADE_API_KEY", "nopass")
    lemonade_stt_model: str = os.getenv("LEMONADE_STT_MODEL", "Whisper-Large-v3-Turbo")
    lemonade_stt_language: str = os.getenv("LEMONADE_STT_LANGUAGE", "zh")
    lemonade_stt_timeout_seconds: float = _float_env("LEMONADE_STT_TIMEOUT_SECONDS", 60.0)
    sherpa_sensevoice_model: str = os.getenv("SHERPA_SENSEVOICE_MODEL", "")
    sherpa_sensevoice_tokens: str = os.getenv("SHERPA_SENSEVOICE_TOKENS", "")
    sherpa_sensevoice_language: str = os.getenv("SHERPA_SENSEVOICE_LANGUAGE", "zh")
    sherpa_sensevoice_use_itn: bool = _bool_env("SHERPA_SENSEVOICE_USE_ITN", True)

    tts_provider: str = os.getenv("STS_TTS_PROVIDER", "sapi")
    sapi_voice: str = os.getenv("SAPI_VOICE", "")
    sherpa_tts_model: str = os.getenv("SHERPA_TTS_MODEL", "")
    sherpa_tts_tokens: str = os.getenv("SHERPA_TTS_TOKENS", "")
    sherpa_tts_data_dir: str = os.getenv("SHERPA_TTS_DATA_DIR", "")
    sherpa_kokoro_model: str = os.getenv("SHERPA_KOKORO_MODEL", "")
    sherpa_kokoro_voices: str = os.getenv("SHERPA_KOKORO_VOICES", "")
    sherpa_kokoro_tokens: str = os.getenv("SHERPA_KOKORO_TOKENS", "")
    sherpa_kokoro_lexicon: str = os.getenv("SHERPA_KOKORO_LEXICON", "")
    sherpa_kokoro_data_dir: str = os.getenv("SHERPA_KOKORO_DATA_DIR", "")
    sherpa_kokoro_voice: int = _int_env("SHERPA_KOKORO_VOICE", 0)
    sherpa_kokoro_lang: str = os.getenv("SHERPA_KOKORO_LANG", "")

    vad_energy_threshold: float = _float_env("VAD_ENERGY_THRESHOLD", 0.012)
    vad_start_ms: int = _int_env("VAD_START_MS", 160)
    vad_end_ms: int = _int_env("VAD_END_MS", 700)
    vad_min_utterance_ms: int = _int_env("VAD_MIN_UTTERANCE_MS", 300)
    vad_max_utterance_ms: int = _int_env("VAD_MAX_UTTERANCE_MS", 12000)

    models_dir: Path = Path(os.getenv("MODELS_DIR", str(ROOT / "models")))
    log_dir: Path = Path(os.getenv("LOG_DIR", str(ROOT / "logs")))


settings = Settings()
