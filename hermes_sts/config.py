from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool_default(default: bool = False) -> bool:
    return default


def _int_default(default: int) -> int:
    return default


def _float_default(default: float) -> float:
    return default


def _path_default(default: str = "") -> str:
    raw = default.strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path)


def _path_list_default(default: str = "") -> str:
    raw = default.strip()
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
    host: str = "127.0.0.1"
    port: int = _int_default(8765)
    log_level: str = "INFO"
    sample_rate: int = _int_default(16000)

    hermes_base_url: str = "http://127.0.0.1:8642/v1"
    hermes_model: str = "hermes-agent"
    hermes_api_key: str = ""
    hermes_max_tokens: int = _int_default(220)
    hermes_timeout_seconds: float = _float_default(45.0)
    hermes_connect_timeout_seconds: float = _float_default(3.0)
    hermes_read_timeout_seconds: float = _float_default(hermes_timeout_seconds)
    hermes_agent_max_wait_seconds: float = _float_default(60.0)
    hermes_first_filler_delay_seconds: float = _float_default(3.0)
    hermes_filler_interval_seconds: float = _float_default(12.0)
    hermes_max_fillers: int = _int_default(0)
    hermes_allow_fallback: bool = _bool_default(True)
    hermes_fallback_text: str = "我这边还在等 Hermes 的结果，语音链路本身是正常的。你可以再说一遍，或者稍等我继续处理。"
    hermes_fallback_texts: str = ""
    hermes_history_max_messages: int = _int_default(2000)
    hermes_history_max_chars: int = _int_default(262144)
    hermes_history_anchor_messages: int = _int_default(80)
    hermes_history_idle_reset_seconds: float = _float_default(21600.0)
    # 21600s = 6h (was 14400/4h). 0=never auto-arch on idle.
    sts_conversations_enabled: bool = True
    sts_conversations_db_path: str = "data/hermes_sts.sqlite3"
    sts_conversations_reload_max_messages: int = 0
    hermes_voice_no_think: bool = _bool_default(True)
    llm_provider: str = "hermes_agent"
    active_llm_profile_id: str = ""
    llm_max_concurrent_requests: int = _int_default(1)
    llm_streaming_enabled: bool = _bool_default(True)
    llm_base_url: str = "http://127.0.0.1:8642/v1"
    llm_model: str = "hermes-agent"
    llm_api_key: str = ""
    llm_max_tokens: int = _int_default(220)
    llm_timeout_seconds: float = _float_default(45.0)
    llm_cache_prompt: bool = _bool_default(True)
    llm_cache_slot: int = _int_default(-1)
    llm_fallback_enabled: bool = _bool_default(True)
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_timeout_seconds: float = _float_default(180.0)
    llm_fallback_max_tokens: int = _int_default(160)
    sts_persona_source: str = "settings"
    sts_persona_preset: str = "operator"
    sts_persona_custom: str = ""

    stt_provider: str = "dev"
    dev_transcript: str = "hello"
    funasr_model_dir: str = _path_default()
    funasr_quantize: bool = _bool_default(False)
    lemonade_base_url: str = "http://127.0.0.1:13305/api/v1"
    lemonade_api_key: str = "nopass"
    lemonade_stt_model: str = "Whisper-Large-v3-Turbo"
    lemonade_stt_language: str = "zh"
    lemonade_stt_timeout_seconds: float = _float_default(60.0)
    sherpa_sensevoice_model: str = _path_default()
    sherpa_sensevoice_tokens: str = _path_default()
    sherpa_sensevoice_language: str = "zh"
    sherpa_sensevoice_use_itn: bool = _bool_default(True)

    tts_provider: str = "qwen3tts"
    sapi_voice: str = ""
    sherpa_tts_model: str = _path_default()
    sherpa_tts_tokens: str = _path_default()
    sherpa_tts_data_dir: str = _path_default()
    sherpa_kokoro_model: str = _path_default()
    sherpa_kokoro_voices: str = _path_default()
    sherpa_kokoro_tokens: str = _path_default()
    sherpa_kokoro_lexicon: str = _path_list_default()
    sherpa_kokoro_data_dir: str = _path_default()
    sherpa_kokoro_voice: int = _int_default(0)
    sherpa_kokoro_lang: str = ""
    tts_voice_source: str = "settings"
    qwentts_cpp_bin: str = _path_default("../hermes-tts-lab/src/qwentts.cpp/build/qwen-tts")
    qwentts_cpp_model: str = _path_default("../hermes-tts-lab/models/qwen-talker-1.7b-base-Q8_0.gguf")
    qwentts_cpp_codec: str = _path_default("../hermes-tts-lab/models/qwen-tokenizer-12hz-Q8_0.gguf")
    qwentts_cpp_base_model: str = _path_default("../hermes-tts-lab/models/qwen-talker-1.7b-base-Q8_0.gguf")
    qwentts_cpp_customvoice_model: str = _path_default("../hermes-tts-lab/models/qwen-talker-1.7b-customvoice-Q8_0.gguf")
    qwentts_cpp_voicedesign_model: str = _path_default("../hermes-tts-lab/models/qwen-talker-1.7b-voicedesign-Q8_0.gguf")
    qwentts_cpp_voice_mode: str = "default"
    qwentts_cpp_voice_preset: str = ""
    qwentts_cpp_voice_design: str = ""
    qwentts_cpp_clone_voice_id: str = ""
    qwentts_cpp_backend: str = "Vulkan0"
    qwentts_cpp_lang: str = "Chinese"
    qwentts_cpp_speaker: str = ""
    qwentts_cpp_instruct: str = ""
    qwentts_cpp_ref_wav: str = _path_default()
    qwentts_cpp_ref_text: str = _path_default()
    qwentts_cpp_ref_spk: str = _path_default()
    qwentts_cpp_ref_rvq: str = _path_default()
    qwentts_cpp_format: str = "wav16"
    qwentts_cpp_extra_args: str = "--codec-chunk-dur 0.5 --codec-left-dur 0.1"
    qwentts_cpp_seed: int = _int_default(42)
    qwentts_cpp_max_new_frames: int = _int_default(512)
    qwentts_cpp_timeout_seconds: float = _float_default(120.0)
    omnivoice_bin: str = _path_default("../hermes-omnivoice-lab/src/omnivoice.cpp/build/omnivoice-tts")
    omnivoice_codec_bin: str = _path_default("../hermes-omnivoice-lab/src/omnivoice.cpp/build/omnivoice-codec")
    omnivoice_model: str = _path_default("../hermes-omnivoice-lab/models/omnivoice-base-Q8_0.gguf")
    omnivoice_codec: str = _path_default("../hermes-omnivoice-lab/models/omnivoice-tokenizer-F32.gguf")
    omnivoice_voice_mode: str = "auto"
    omnivoice_voice_design: str = ""
    omnivoice_clone_voice_id: str = ""
    omnivoice_backend: str = "Vulkan0"
    omnivoice_lang: str = "Chinese"
    omnivoice_ref_wav: str = _path_default()
    omnivoice_ref_text: str = _path_default()
    omnivoice_ref_rvq: str = _path_default()
    omnivoice_format: str = "wav16"
    omnivoice_extra_args: str = ""
    omnivoice_seed: int = _int_default(42)
    omnivoice_duration_seconds: float = _float_default(0.0)
    omnivoice_chunk_duration_seconds: float = _float_default(15.0)
    omnivoice_chunk_threshold_seconds: float = _float_default(30.0)
    omnivoice_timeout_seconds: float = _float_default(120.0)

    vad_provider: str = "energy"
    vad_energy_threshold: float = _float_default(0.004)
    vad_start_ms: int = _int_default(120)
    vad_end_ms: int = _int_default(600)
    vad_min_utterance_ms: int = _int_default(300)
    vad_max_utterance_ms: int = _int_default(12000)
    sherpa_silero_vad_model: str = _path_default()
    vad_min_silence_seconds: float = _float_default(0.45)
    vad_buffer_seconds: float = _float_default(30.0)
    vad_threshold: float = _float_default(0.35)
    max_audio_chunk_bytes: int = _int_default(32000)
    suppress_input_while_speaking: bool = _bool_default(True)
    response_audio_chunk_ms: int = _int_default(80)
    response_audio_chunk_send_delay_ms: int = _int_default(0)
    tts_segment_min_chars: int = _int_default(8)
    tts_segment_max_chars: int = _int_default(48)
    tts_strip_bracketed_cues: bool = _bool_default(True)
    tts_strip_emoji: bool = _bool_default(True)
    tts_max_audio_seconds: float = _float_default(18.0)
    latency_logging: bool = _bool_default(True)
    dashboard_wave_style: str = "scanner"

    memory_enabled: bool = _bool_default(False)
    memory_provider: str = "sqlite"  # sqlite | openviking | noop
    memory_remember_in_hermes: bool = _bool_default(True)
    memory_injection_budget: int = _int_default(500)
    memory_recall_limit: int = _int_default(5)
    memory_recall_min_score: float = _float_default(0.0)
    memory_commit_interval_turns: int = _int_default(10)
    memory_commit_idle_seconds: float = _float_default(300.0)
    memory_extract_enabled: bool = _bool_default(True)
    memory_extract_max_per_turn: int = _int_default(2)
    openviking_base_url: str = "http://127.0.0.1:1933"
    openviking_api_key: str = ""
    openviking_account: str = "default"
    openviking_user: str = "reachy"
    openviking_target_uri: str = "viking://user/memories/"
    openviking_timeout_seconds: float = _float_default(6.0)
    openviking_commit_timeout_seconds: float = _float_default(30.0)
    sqlite_memory_path: str = _path_default("data/memory.sqlite3")
    web_search_enabled: bool = _bool_default(False)
    web_search_providers: str = "brave,tavily,searxng,duckduckgo"
    tavily_api_key: str = ""
    tavily_search_depth: str = "basic"
    tavily_max_results: int = _int_default(3)
    tavily_timeout_seconds: float = _float_default(2.0)
    tavily_base_url: str = "https://api.tavily.com"
    brave_api_key: str = ""
    brave_base_url: str = "https://api.search.brave.com/res/v1"
    brave_timeout_seconds: float = _float_default(2.5)
    duckduckgo_timeout_seconds: float = _float_default(2.5)
    searxng_base_url: str = ""
    searxng_timeout_seconds: float = _float_default(3.0)
    terminal_tool_enabled: bool = _bool_default(False)
    terminal_tool_allowed_commands: str = "curl,python3,node,date,pwd,ls,rg,jq"
    terminal_tool_cwd: str = _path_default(str(ROOT))
    terminal_tool_timeout_seconds: float = _float_default(6.0)
    terminal_tool_max_output_chars: int = _int_default(4000)
    client_tool_followup_timeout_seconds: float = _float_default(15.0)
    # 客户端工具转发后等待 function_call_output + response.create 的最大秒数；
    # 超时后静默清理 pending_tool_context/pending_tool_results，避免会话卡死

    models_dir: Path = Path(_path_default(str(ROOT / "models")))
    log_dir: Path = Path(_path_default(str(ROOT / "logs")))
    data_dir: Path = Path(_path_default(str(ROOT / "data")))
    config_db: Path = Path(_path_default(str(ROOT / "data" / "hermes_sts.sqlite3")))


def load_settings() -> Settings:
    from hermes_sts.config_store import ConfigStore

    return ConfigStore.default().load_settings()


settings = load_settings()
