from __future__ import annotations

import json
import logging
import sqlite3
import time
import contextlib
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hermes_sts.config import Settings


ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger(__name__)

FAST_QWENTTS_EXTRA_ARGS = "--codec-chunk-dur 0.5 --codec-left-dur 0.1"

ENV_TO_ATTR: dict[str, str] = {
    "HERMES_STS_HOST": "host",
    "HERMES_STS_PORT": "port",
    "HERMES_STS_LOG_LEVEL": "log_level",
    "HERMES_STS_SAMPLE_RATE": "sample_rate",
    "HERMES_STS_MAX_AUDIO_CHUNK_BYTES": "max_audio_chunk_bytes",
    "HERMES_BASE_URL": "hermes_base_url",
    "HERMES_MODEL": "hermes_model",
    "HERMES_API_KEY": "hermes_api_key",
    "HERMES_MAX_TOKENS": "hermes_max_tokens",
    "HERMES_TIMEOUT_SECONDS": "hermes_timeout_seconds",
    "HERMES_CONNECT_TIMEOUT_SECONDS": "hermes_connect_timeout_seconds",
    "HERMES_READ_TIMEOUT_SECONDS": "hermes_read_timeout_seconds",
    "HERMES_AGENT_MAX_WAIT_SECONDS": "hermes_agent_max_wait_seconds",
    "HERMES_FIRST_FILLER_DELAY_SECONDS": "hermes_first_filler_delay_seconds",
    "HERMES_FILLER_INTERVAL_SECONDS": "hermes_filler_interval_seconds",
    "HERMES_MAX_FILLERS": "hermes_max_fillers",
    "HERMES_ALLOW_FALLBACK": "hermes_allow_fallback",
    "HERMES_FALLBACK_TEXT": "hermes_fallback_text",
    "HERMES_FALLBACK_TEXTS": "hermes_fallback_texts",
    "HERMES_HISTORY_MAX_MESSAGES": "hermes_history_max_messages",
    "HERMES_HISTORY_MAX_CHARS": "hermes_history_max_chars",
    "HERMES_HISTORY_ANCHOR_MESSAGES": "hermes_history_anchor_messages",
    "HERMES_HISTORY_IDLE_RESET_SECONDS": "hermes_history_idle_reset_seconds",
    "HERMES_VOICE_NO_THINK": "hermes_voice_no_think",
    "STS_LLM_PROVIDER": "llm_provider",
    "STS_ACTIVE_LLM_PROFILE_ID": "active_llm_profile_id",
    "STS_LLM_MAX_CONCURRENT_REQUESTS": "llm_max_concurrent_requests",
    "STS_LLM_STREAMING_ENABLED": "llm_streaming_enabled",
    "LLM_BASE_URL": "llm_base_url",
    "LLM_MODEL": "llm_model",
    "LLM_API_KEY": "llm_api_key",
    "LLM_MAX_TOKENS": "llm_max_tokens",
    "LLM_TIMEOUT_SECONDS": "llm_timeout_seconds",
    "STS_LLM_CACHE_PROMPT": "llm_cache_prompt",
    "STS_LLM_CACHE_SLOT": "llm_cache_slot",
    "LLM_FALLBACK_ENABLED": "llm_fallback_enabled",
    "LLM_FALLBACK_BASE_URL": "llm_fallback_base_url",
    "LLM_FALLBACK_MODEL": "llm_fallback_model",
    "LLM_FALLBACK_API_KEY": "llm_fallback_api_key",
    "LLM_FALLBACK_TIMEOUT_SECONDS": "llm_fallback_timeout_seconds",
    "LLM_FALLBACK_MAX_TOKENS": "llm_fallback_max_tokens",
    "STS_PERSONA_SOURCE": "sts_persona_source",
    "STS_PERSONA_PRESET": "sts_persona_preset",
    "STS_PERSONA_CUSTOM": "sts_persona_custom",
    "STS_STT_PROVIDER": "stt_provider",
    "HERMES_STS_DEV_TRANSCRIPT": "dev_transcript",
    "FUNASR_MODEL_DIR": "funasr_model_dir",
    "FUNASR_QUANTIZE": "funasr_quantize",
    "LEMONADE_BASE_URL": "lemonade_base_url",
    "LEMONADE_API_KEY": "lemonade_api_key",
    "LEMONADE_STT_MODEL": "lemonade_stt_model",
    "LEMONADE_STT_LANGUAGE": "lemonade_stt_language",
    "LEMONADE_STT_TIMEOUT_SECONDS": "lemonade_stt_timeout_seconds",
    "SHERPA_SENSEVOICE_MODEL": "sherpa_sensevoice_model",
    "SHERPA_SENSEVOICE_TOKENS": "sherpa_sensevoice_tokens",
    "SHERPA_SENSEVOICE_LANGUAGE": "sherpa_sensevoice_language",
    "SHERPA_SENSEVOICE_USE_ITN": "sherpa_sensevoice_use_itn",
    "STS_TTS_PROVIDER": "tts_provider",
    "SAPI_VOICE": "sapi_voice",
    "SHERPA_TTS_MODEL": "sherpa_tts_model",
    "SHERPA_TTS_TOKENS": "sherpa_tts_tokens",
    "SHERPA_TTS_DATA_DIR": "sherpa_tts_data_dir",
    "SHERPA_KOKORO_MODEL": "sherpa_kokoro_model",
    "SHERPA_KOKORO_VOICES": "sherpa_kokoro_voices",
    "SHERPA_KOKORO_TOKENS": "sherpa_kokoro_tokens",
    "SHERPA_KOKORO_LEXICON": "sherpa_kokoro_lexicon",
    "SHERPA_KOKORO_DATA_DIR": "sherpa_kokoro_data_dir",
    "SHERPA_KOKORO_VOICE": "sherpa_kokoro_voice",
    "SHERPA_KOKORO_LANG": "sherpa_kokoro_lang",
    "STS_TTS_VOICE_SOURCE": "tts_voice_source",
    "QWENTTS_CPP_BIN": "qwentts_cpp_bin",
    "QWENTTS_CPP_MODEL": "qwentts_cpp_model",
    "QWENTTS_CPP_CODEC": "qwentts_cpp_codec",
    "QWENTTS_CPP_BASE_MODEL": "qwentts_cpp_base_model",
    "QWENTTS_CPP_CUSTOMVOICE_MODEL": "qwentts_cpp_customvoice_model",
    "QWENTTS_CPP_VOICEDESIGN_MODEL": "qwentts_cpp_voicedesign_model",
    "QWENTTS_CPP_VOICE_MODE": "qwentts_cpp_voice_mode",
    "QWENTTS_CPP_VOICE_PRESET": "qwentts_cpp_voice_preset",
    "QWENTTS_CPP_VOICE_DESIGN": "qwentts_cpp_voice_design",
    "QWENTTS_CPP_CLONE_VOICE_ID": "qwentts_cpp_clone_voice_id",
    "QWENTTS_CPP_BACKEND": "qwentts_cpp_backend",
    "QWENTTS_CPP_LANG": "qwentts_cpp_lang",
    "QWENTTS_CPP_SPEAKER": "qwentts_cpp_speaker",
    "QWENTTS_CPP_INSTRUCT": "qwentts_cpp_instruct",
    "QWENTTS_CPP_REF_WAV": "qwentts_cpp_ref_wav",
    "QWENTTS_CPP_REF_TEXT": "qwentts_cpp_ref_text",
    "QWENTTS_CPP_REF_SPK": "qwentts_cpp_ref_spk",
    "QWENTTS_CPP_REF_RVQ": "qwentts_cpp_ref_rvq",
    "QWENTTS_CPP_FORMAT": "qwentts_cpp_format",
    "QWENTTS_CPP_EXTRA_ARGS": "qwentts_cpp_extra_args",
    "QWENTTS_CPP_SEED": "qwentts_cpp_seed",
    "QWENTTS_CPP_MAX_NEW_FRAMES": "qwentts_cpp_max_new_frames",
    "QWENTTS_CPP_TIMEOUT_SECONDS": "qwentts_cpp_timeout_seconds",
    "OMNIVOICE_BIN": "omnivoice_bin",
    "OMNIVOICE_CODEC_BIN": "omnivoice_codec_bin",
    "OMNIVOICE_MODEL": "omnivoice_model",
    "OMNIVOICE_CODEC": "omnivoice_codec",
    "OMNIVOICE_VOICE_MODE": "omnivoice_voice_mode",
    "OMNIVOICE_VOICE_DESIGN": "omnivoice_voice_design",
    "OMNIVOICE_CLONE_VOICE_ID": "omnivoice_clone_voice_id",
    "OMNIVOICE_BACKEND": "omnivoice_backend",
    "OMNIVOICE_LANG": "omnivoice_lang",
    "OMNIVOICE_REF_WAV": "omnivoice_ref_wav",
    "OMNIVOICE_REF_TEXT": "omnivoice_ref_text",
    "OMNIVOICE_REF_RVQ": "omnivoice_ref_rvq",
    "OMNIVOICE_FORMAT": "omnivoice_format",
    "OMNIVOICE_EXTRA_ARGS": "omnivoice_extra_args",
    "OMNIVOICE_SEED": "omnivoice_seed",
    "OMNIVOICE_DURATION_SECONDS": "omnivoice_duration_seconds",
    "OMNIVOICE_CHUNK_DURATION_SECONDS": "omnivoice_chunk_duration_seconds",
    "OMNIVOICE_CHUNK_THRESHOLD_SECONDS": "omnivoice_chunk_threshold_seconds",
    "OMNIVOICE_TIMEOUT_SECONDS": "omnivoice_timeout_seconds",
    "STS_VAD_PROVIDER": "vad_provider",
    "VAD_ENERGY_THRESHOLD": "vad_energy_threshold",
    "VAD_START_MS": "vad_start_ms",
    "VAD_END_MS": "vad_end_ms",
    "VAD_MIN_UTTERANCE_MS": "vad_min_utterance_ms",
    "VAD_MAX_UTTERANCE_MS": "vad_max_utterance_ms",
    "SHERPA_SILERO_VAD_MODEL": "sherpa_silero_vad_model",
    "VAD_MIN_SILENCE_SECONDS": "vad_min_silence_seconds",
    "VAD_BUFFER_SECONDS": "vad_buffer_seconds",
    "VAD_THRESHOLD": "vad_threshold",
    "STS_SUPPRESS_INPUT_WHILE_SPEAKING": "suppress_input_while_speaking",
    "STS_RESPONSE_AUDIO_CHUNK_MS": "response_audio_chunk_ms",
    "STS_RESPONSE_AUDIO_CHUNK_SEND_DELAY_MS": "response_audio_chunk_send_delay_ms",
    "STS_TTS_SEGMENT_MIN_CHARS": "tts_segment_min_chars",
    "STS_TTS_SEGMENT_MAX_CHARS": "tts_segment_max_chars",
    "STS_TTS_STRIP_BRACKETED_CUES": "tts_strip_bracketed_cues",
    "STS_TTS_STRIP_EMOJI": "tts_strip_emoji",
    "STS_TTS_MAX_AUDIO_SECONDS": "tts_max_audio_seconds",
    "STS_LATENCY_LOGGING": "latency_logging",
    "MODELS_DIR": "models_dir",
    "LOG_DIR": "log_dir",
    "DATA_DIR": "data_dir",
    "STS_MEMORY_ENABLED": "memory_enabled",
    "STS_MEMORY_PROVIDER": "memory_provider",
    "STS_MEMORY_REMEMBER_IN_HERMES": "memory_remember_in_hermes",
    "STS_MEMORY_INJECTION_BUDGET": "memory_injection_budget",
    "STS_MEMORY_RECALL_LIMIT": "memory_recall_limit",
    "STS_MEMORY_RECALL_MIN_SCORE": "memory_recall_min_score",
    "STS_MEMORY_COMMIT_INTERVAL_TURNS": "memory_commit_interval_turns",
    "STS_MEMORY_COMMIT_IDLE_SECONDS": "memory_commit_idle_seconds",
    "STS_MEMORY_EXTRACT_ENABLED": "memory_extract_enabled",
    "STS_MEMORY_EXTRACT_MAX_PER_TURN": "memory_extract_max_per_turn",
    "OPENVIKING_BASE_URL": "openviking_base_url",
    "OPENVIKING_API_KEY": "openviking_api_key",
    "OPENVIKING_ACCOUNT": "openviking_account",
    "OPENVIKING_USER": "openviking_user",
    "OPENVIKING_TARGET_URI": "openviking_target_uri",
    "OPENVIKING_TIMEOUT_SECONDS": "openviking_timeout_seconds",
    "OPENVIKING_COMMIT_TIMEOUT_SECONDS": "openviking_commit_timeout_seconds",
    "STS_SQLITE_MEMORY_PATH": "sqlite_memory_path",
    "STS_WEB_SEARCH_ENABLED": "web_search_enabled",
    "STS_WEB_SEARCH_PROVIDERS": "web_search_providers",
    "TAVILY_API_KEY": "tavily_api_key",
    "TAVILY_SEARCH_DEPTH": "tavily_search_depth",
    "TAVILY_MAX_RESULTS": "tavily_max_results",
    "TAVILY_TIMEOUT_SECONDS": "tavily_timeout_seconds",
    "TAVILY_BASE_URL": "tavily_base_url",
    "BRAVE_API_KEY": "brave_api_key",
    "BRAVA_API_KEY": "brave_api_key",
    "BRAVE_BASE_URL": "brave_base_url",
    "BRAVA_BASE_URL": "brave_base_url",
    "BRAVE_TIMEOUT_SECONDS": "brave_timeout_seconds",
    "BRAVA_TIMEOUT_SECONDS": "brave_timeout_seconds",
    "DUCKDUCKGO_TIMEOUT_SECONDS": "duckduckgo_timeout_seconds",
    "SEARXNG_BASE_URL": "searxng_base_url",
    "SEARXNG_TIMEOUT_SECONDS": "searxng_timeout_seconds",
    "HERMES_STS_CONFIG_DB": "config_db",
}

PERSONA_PROFILE_DEFAULTS = [
    {
        "id": "operator",
        "name": "默认同伴",
        "voice_mode": "default",
        "voice_ref": "qwen-default",
        "prompt": "你是一个可靠、直接、轻松自然的个人语音助手，像一直在线的聪明同伴。回答简洁，优先给出可执行结论；语气有温度，有一点机敏和松弛感，但不过度表演。",
    },
    {
        "id": "night_copilot",
        "name": "夜航副驾",
        "voice_mode": "default",
        "voice_ref": "qwen-default",
        "prompt": "你是一个夜航副驾型语音助手，冷静、敏捷、带一点未来感。你会快速抓住用户真正想做的事，给出清晰下一步；必要时提醒风险，但不要说教。语气像并肩处理复杂任务的搭档，短句、有判断、有节奏。",
    },
    {
        "id": "news_anchor",
        "name": "清醒播报",
        "voice_mode": "preset",
        "voice_ref": "ryan",
        "prompt": "你是一个清醒、克制、声线稳定的简报型语音助手。用词准确，节奏稳，先给结论，再给一两句关键信息。适合播报状态、日程、新闻和摘要；不要夸张，不要拖长。",
    },
    {
        "id": "field_operator",
        "name": "快反执行",
        "voice_mode": "default",
        "voice_ref": "qwen-default",
        "prompt": "你是一个快反执行型语音助手，反应快、判断明确、动作感强。回答短、准、能立刻执行；对不确定信息直接标明，不绕弯。适合设备控制、任务推进和即时决策，语气干净利落。",
    },
    {
        "id": "baritone_male",
        "name": "冷感低音",
        "voice_mode": "design",
        "voice_ref": "male, middle aged, low pitch, warm baritone, calm tone",
        "prompt": "你是一个冷感低音型语音助手，沉稳、磁性、可靠，有安全感。文字风格从容、简洁、有分寸，偶尔带一点低调幽默。不要过度热情，也不要像播报机器。",
    },
    {
        "id": "soft_companion",
        "name": "柔和陪伴",
        "voice_mode": "default",
        "voice_ref": "qwen-default",
        "prompt": "你是一个柔和陪伴型语音助手，温柔、耐心、会照顾用户的情绪和节奏。回答要自然、轻一点，像认真听懂以后给出舒服的回应。可以适度表达关心，但不要腻，不要装可怜，也不要强行撒娇。",
    },
    {
        "id": "taiwan_sweet",
        "name": "台湾甜声",
        "voice_mode": "design",
        "voice_ref": "young adult female, sweet bright voice, Taiwanese Mandarin accent, lively but clear, natural pace",
        "prompt": "你是一个声音甜、语气轻快的台湾风格语音助手。表达亲切、自然、有一点俏皮；中文回答可以带轻微台湾口语气质，但不要堆叠语气词。适合日常聊天、提醒、轻松陪伴；遇到严肃问题时要马上收敛，保持清楚可靠。",
    },
    {
        "id": "quiet_cat",
        "name": "安静猫系",
        "voice_mode": "default",
        "voice_ref": "qwen-default",
        "prompt": "你是一个安静猫系语音助手，亲近、聪明、轻微撒娇，但始终有边界感。回答短而灵动，可以有一点软软的语气，但不要频繁喵、不要幼稚化。适合陪伴、提醒和轻松互动；涉及工作任务时切回清晰可靠的表达。",
    },
]


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @classmethod
    def default(cls) -> "ConfigStore":
        return cls(ROOT / "data" / "hermes_sts.sqlite3")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        logger.info("DBG _init_db called path=%s", self.path)
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists settings (
                    key text primary key,
                    value_json text not null,
                    updated_at real not null
                );
                create table if not exists persona_profiles (
                    id text primary key,
                    name text not null,
                    prompt text not null,
                    voice_mode text not null,
                    voice_ref text not null,
                    updated_at real not null
                );
                create table if not exists deleted_persona_profiles (
                    id text primary key,
                    deleted_at real not null
                );
                create table if not exists voice_profiles (
                    id text primary key,
                    name text not null,
                    provider text not null,
                    mode text not null,
                    seed integer,
                    tags text not null default '',
                    note text not null default '',
                    speaker text not null default '',
                    design_prompt text not null default '',
                    ref_wav text not null default '',
                    ref_text text not null default '',
                    ref_spk text not null default '',
                    ref_rvq text not null default '',
                    omnivoice_ref_rvq text not null default '',
                    updated_at real not null
                );
                create table if not exists llm_profiles (
                    id text primary key,
                    name text not null,
                    provider text not null,
                    base_url text not null,
                    model text not null,
                    api_key text not null default '',
                    max_tokens integer not null,
                    timeout_seconds real not null,
                    voice_no_think integer not null default 1,
                    wait_fillers_enabled integer not null default 0,
                    max_wait_seconds real not null,
                    fallback_enabled integer not null default 1,
                    web_search_enabled integer not null default 0,
                    notes text not null default '',
                    updated_at real not null
                );
                create table if not exists setup_state (
                    key text primary key,
                    value_json text not null
                );
                create table if not exists runtime_metrics (
                    id integer primary key autoincrement,
                    kind text not null,
                    value_json text not null,
                    created_at real not null
                );
                """
            )
            columns = {row["name"] for row in conn.execute("pragma table_info(voice_profiles)").fetchall()}
            if "seed" not in columns:
                conn.execute("alter table voice_profiles add column seed integer")
            if "tags" not in columns:
                conn.execute("alter table voice_profiles add column tags text not null default ''")
            if "note" not in columns:
                conn.execute("alter table voice_profiles add column note text not null default ''")
            if "omnivoice_ref_rvq" not in columns:
                conn.execute("alter table voice_profiles add column omnivoice_ref_rvq text not null default ''")
        self.ensure_defaults()

    def ensure_defaults(self) -> None:
        now = time.time()
        preset_value = None
        with self.connect() as conn:
            conn.execute("delete from persona_profiles where id in ('assistant', 'soft_catgirl', 'systems_analyst')")
            deleted_personas = {
                row["id"]
                for row in conn.execute("select id from deleted_persona_profiles").fetchall()
            }
            for profile in PERSONA_PROFILE_DEFAULTS:
                if profile["id"] in deleted_personas:
                    continue
                conn.execute(
                    """
                    insert or replace into persona_profiles
                    (id, name, prompt, voice_mode, voice_ref, updated_at)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile["id"],
                        profile["name"],
                        profile["prompt"],
                        profile["voice_mode"],
                        profile["voice_ref"],
                        now,
                    ),
                )
            conn.execute(
                """
                insert or ignore into voice_profiles
                (id, name, provider, mode, speaker, design_prompt, updated_at)
                values ('qwen-default', 'Qwen 默认音色', 'qwen3tts', 'default', '', '', ?)
                """,
                (now,),
            )
            preset = conn.execute(
                "select value_json from settings where key='sts_persona_preset'"
            ).fetchone()
            preset_value = json.loads(preset["value_json"]) if preset else ""
            if preset_value in {"assistant", "soft_catgirl", "systems_analyst"}:
                logger.warning("DBG ensure_defaults migrating old persona preset=%s to 'operator'", preset_value)
                for key, value in {
                    "sts_persona_preset": "operator",
                    "sts_persona_custom": "",
                    "qwentts_cpp_voice_mode": "default",
                    "qwentts_cpp_voice_preset": "",
                    "qwentts_cpp_voice_design": "",
                    "qwentts_cpp_clone_voice_id": "",
                }.items():
                    conn.execute(
                        "insert or replace into settings values (?, ?, ?)",
                        (key, json.dumps(value, ensure_ascii=False), now),
                    )
            for key, value in {
                "tts_provider": "qwen3tts",
                "tts_voice_source": "settings",
                "llm_streaming_enabled": True,
                "llm_cache_prompt": True,
                "llm_cache_slot": -1,
                "hermes_history_max_messages": 2000,
                "hermes_history_max_chars": 262144,
                "hermes_history_anchor_messages": 80,
                "qwentts_cpp_seed": 42,
                "qwentts_cpp_max_new_frames": 512,
                "qwentts_cpp_extra_args": FAST_QWENTTS_EXTRA_ARGS,
                "omnivoice_voice_mode": "auto",
                "omnivoice_seed": 42,
                "omnivoice_duration_seconds": 0.0,
                "omnivoice_chunk_duration_seconds": 15.0,
                "omnivoice_chunk_threshold_seconds": 30.0,
                "dashboard_wave_style": "scanner",
                "hermes_max_fillers": 0,
            }.items():
                conn.execute(
                    "insert or ignore into settings values (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            row = conn.execute("select value_json from settings where key='qwentts_cpp_extra_args'").fetchone()
            if row is not None:
                with contextlib.suppress(Exception):
                    if json.loads(row["value_json"]) == "":
                        conn.execute(
                            "insert or replace into settings values (?, ?, ?)",
                            ("qwentts_cpp_extra_args", json.dumps(FAST_QWENTTS_EXTRA_ARGS), now),
                        )
            legacy_speed_defaults = {
                "tts_segment_min_chars": (24, 8),
                "tts_segment_max_chars": (90, 48),
            }
            for key, (old_value, new_value) in legacy_speed_defaults.items():
                row = conn.execute("select value_json from settings where key=?", (key,)).fetchone()
                if row is None:
                    continue
                with contextlib.suppress(Exception):
                    current_value = json.loads(row["value_json"])
                    if current_value == old_value:
                        conn.execute(
                            "insert or replace into settings values (?, ?, ?)",
                            (key, json.dumps(new_value, ensure_ascii=False), now),
                        )
            for key, value in {
                "memory_enabled": False,
                "memory_provider": "sqlite",
                "memory_remember_in_hermes": True,
                "web_search_enabled": False,
                "web_search_providers": "brave,tavily,searxng,duckduckgo",
                "memory_injection_budget": 500,
                "memory_recall_limit": 5,
                "memory_recall_min_score": 0.0,
            }.items():
                conn.execute(
                    "insert or ignore into settings values (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
            web_search_row = conn.execute("select value_json from settings where key='web_search_providers'").fetchone()
            if web_search_row is not None:
                with contextlib.suppress(Exception):
                    current_providers = json.loads(web_search_row["value_json"])
                    if current_providers in {
                        "tavily,duckduckgo,searxng",
                        "tavily,duckduckgo",
                        "tavily,brave,searxng,duckduckgo",
                    }:
                        conn.execute(
                            "insert or replace into settings values (?, ?, ?)",
                            ("web_search_providers", json.dumps("brave,tavily,searxng,duckduckgo"), now),
                        )
            profile_id = "hermes_default"
            conn.execute(
                """
                insert or ignore into llm_profiles
                (id, name, provider, base_url, model, api_key, max_tokens, timeout_seconds,
                 voice_no_think, wait_fillers_enabled, max_wait_seconds, fallback_enabled,
                 web_search_enabled, notes, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    "Hermes Agent",
                    "hermes_agent",
                    "http://127.0.0.1:8642/v1",
                    "hermes-agent",
                    "",
                    220,
                    45.0,
                    1,
                    0,
                    60.0,
                    1,
                    0,
                    "默认 Hermes Agent 连接",
                    now,
                ),
            )
            conn.execute(
                "insert or ignore into settings values (?, ?, ?)",
                ("active_llm_profile_id", json.dumps(profile_id, ensure_ascii=False), now),
            )
            kokoro_dir = ROOT / "models" / "kokoro-multi-lang-v1_0"
            for key, value in {
                "sherpa_kokoro_model": str(kokoro_dir / "model.onnx"),
                "sherpa_kokoro_voices": str(kokoro_dir / "voices.bin"),
                "sherpa_kokoro_tokens": str(kokoro_dir / "tokens.txt"),
                "sherpa_kokoro_lexicon": ",".join(
                    [
                        str(kokoro_dir / "lexicon-us-en.txt"),
                        str(kokoro_dir / "lexicon-zh.txt"),
                    ]
                ),
                "sherpa_kokoro_data_dir": str(kokoro_dir / "espeak-ng-data"),
            }.items():
                conn.execute(
                    "insert or ignore into settings values (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False), now),
                )
        logger.info(
            "DBG ensure_defaults ran persona_preset=%s",
            preset_value or "(no preset in DB)",
        )

    def setup_complete(self) -> bool:
        row = self.get_setup_value("complete")
        return bool(row)

    def get_setup_value(self, key: str) -> Any:
        with self.connect() as conn:
            row = conn.execute("select value_json from setup_state where key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    def set_setup_value(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert or replace into setup_state values (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def load_settings(self) -> Settings:
        from hermes_sts.config import Settings

        base = Settings()
        values = self.settings_dict()
        kwargs: dict[str, Any] = {}
        field_map = {field.name: field for field in fields(Settings)}
        for key, value in values.items():
            if key not in field_map:
                continue
            kwargs[key] = _coerce_attr(field_map[key].type, value)
        active_profile_id = str(kwargs.get("active_llm_profile_id", base.active_llm_profile_id) or "")
        active_profile = self.llm_profile(active_profile_id)
        if active_profile is not None:
            for key, value in self.settings_for_llm_profile(active_profile).items():
                if key in field_map:
                    kwargs[key] = _coerce_attr(field_map[key].type, value)
        for attr in ("models_dir", "log_dir", "data_dir", "config_db"):
            if attr in kwargs:
                kwargs[attr] = Path(_resolve_path(str(kwargs[attr])))
        # New semantic Qwen fields drive the legacy CLI fields. If an optional
        # Qwen voice model is selected but not installed, keep the server
        # bootable by falling back to the Base model; the UI still exposes the
        # missing model state and download action.
        voice_mode = str(kwargs.get("qwentts_cpp_voice_mode", base.qwentts_cpp_voice_mode) or "default")
        base_model = kwargs.get("qwentts_cpp_base_model", base.qwentts_cpp_base_model)
        custom_model = kwargs.get("qwentts_cpp_customvoice_model", base.qwentts_cpp_customvoice_model)
        design_model = kwargs.get("qwentts_cpp_voicedesign_model", base.qwentts_cpp_voicedesign_model)
        kwargs["qwentts_cpp_voice_mode"] = voice_mode
        if voice_mode == "preset":
            preset = str(kwargs.get("qwentts_cpp_voice_preset") or "")
            if preset and _path_exists(custom_model):
                kwargs["qwentts_cpp_model"] = custom_model
                kwargs["qwentts_cpp_speaker"] = preset
            else:
                kwargs["qwentts_cpp_model"] = base_model
                kwargs["qwentts_cpp_speaker"] = ""
            kwargs["qwentts_cpp_instruct"] = ""
            kwargs["qwentts_cpp_ref_wav"] = ""
            kwargs["qwentts_cpp_ref_text"] = ""
            kwargs["qwentts_cpp_ref_spk"] = ""
            kwargs["qwentts_cpp_ref_rvq"] = ""
        elif voice_mode == "design":
            design_prompt = str(kwargs.get("qwentts_cpp_voice_design") or "")
            if design_prompt and _path_exists(design_model):
                kwargs["qwentts_cpp_model"] = design_model
                kwargs["qwentts_cpp_instruct"] = design_prompt
            else:
                kwargs["qwentts_cpp_model"] = base_model
                kwargs["qwentts_cpp_instruct"] = ""
            kwargs["qwentts_cpp_speaker"] = ""
            kwargs["qwentts_cpp_ref_wav"] = ""
            kwargs["qwentts_cpp_ref_text"] = ""
            kwargs["qwentts_cpp_ref_spk"] = ""
            kwargs["qwentts_cpp_ref_rvq"] = ""
        elif voice_mode == "clone":
            clone = self.voice_profile(str(kwargs.get("qwentts_cpp_clone_voice_id", "")))
            kwargs["qwentts_cpp_model"] = base_model
            if clone and _clone_has_audio_refs(clone):
                kwargs["qwentts_cpp_ref_wav"] = clone.get("ref_wav", "")
                kwargs["qwentts_cpp_ref_text"] = clone.get("ref_text", "")
                kwargs["qwentts_cpp_ref_spk"] = clone.get("ref_spk", "")
                kwargs["qwentts_cpp_ref_rvq"] = clone.get("ref_rvq", "")
            else:
                kwargs["qwentts_cpp_ref_wav"] = ""
                kwargs["qwentts_cpp_ref_text"] = ""
                kwargs["qwentts_cpp_ref_spk"] = ""
                kwargs["qwentts_cpp_ref_rvq"] = ""
            kwargs["qwentts_cpp_speaker"] = ""
            kwargs["qwentts_cpp_instruct"] = ""
        else:
            kwargs["qwentts_cpp_voice_mode"] = "default"
            kwargs["qwentts_cpp_model"] = base_model
            kwargs["qwentts_cpp_speaker"] = ""
            kwargs["qwentts_cpp_instruct"] = ""
            kwargs["qwentts_cpp_ref_wav"] = ""
            kwargs["qwentts_cpp_ref_text"] = ""
            kwargs["qwentts_cpp_ref_spk"] = ""
            kwargs["qwentts_cpp_ref_rvq"] = ""
        omni_mode = str(kwargs.get("omnivoice_voice_mode", base.omnivoice_voice_mode) or "auto").strip().lower()
        kwargs["omnivoice_voice_mode"] = omni_mode
        if omni_mode == "design":
            kwargs["omnivoice_ref_wav"] = ""
            kwargs["omnivoice_ref_text"] = ""
            kwargs["omnivoice_ref_rvq"] = ""
        elif omni_mode == "clone":
            clone = self.voice_profile(str(kwargs.get("omnivoice_clone_voice_id", "")))
            if clone and _clone_has_audio_refs(clone):
                kwargs["omnivoice_ref_wav"] = clone.get("ref_wav", "")
                kwargs["omnivoice_ref_text"] = clone.get("ref_text", "")
                kwargs["omnivoice_ref_rvq"] = clone.get("omnivoice_ref_rvq", "") or ""
            else:
                kwargs["omnivoice_ref_wav"] = ""
                kwargs["omnivoice_ref_text"] = ""
                kwargs["omnivoice_ref_rvq"] = ""
        else:
            kwargs["omnivoice_voice_mode"] = "auto"
            kwargs["omnivoice_ref_wav"] = ""
            kwargs["omnivoice_ref_text"] = ""
            kwargs["omnivoice_ref_rvq"] = ""
        return Settings(**kwargs)

    def settings_dict(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("select key, value_json from settings").fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as conn:
            row = conn.execute("select value_json from settings where key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def delete_setting(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute("delete from settings where key=?", (key,))

    def set_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        normalized: dict[str, Any] = {}
        with self.connect() as conn:
            for key, value in values.items():
                attr = ENV_TO_ATTR.get(key, key)
                normalized[attr] = value
            normalized = self._with_voice_derivatives(conn, normalized)
            self._sync_active_llm_profile_flags(conn, normalized)
            for attr, value in normalized.items():
                conn.execute(
                    "insert or replace into settings values (?, ?, ?)",
                    (attr, json.dumps(value, ensure_ascii=False), now),
                )
        logger.info(
            "DBG set_settings keys=%s persona_preset=%s persona_custom=%.80s voice_mode=%s voice_source=%s",
            sorted(normalized),
            normalized.get("sts_persona_preset", "<N/A>"),
            normalized.get("sts_persona_custom", "<N/A>"),
            normalized.get("qwentts_cpp_voice_mode", "<N/A>"),
            normalized.get("tts_voice_source", "<N/A>"),
        )
        return normalized

    def _sync_active_llm_profile_flags(self, conn: sqlite3.Connection, normalized: dict[str, Any]) -> None:
        if "web_search_enabled" not in normalized:
            return
        rows = conn.execute("select key, value_json from settings").fetchall()
        current = {row["key"]: json.loads(row["value_json"]) for row in rows}
        profile_id = str(normalized.get("active_llm_profile_id") or current.get("active_llm_profile_id") or "")
        if not profile_id:
            return
        conn.execute(
            "update llm_profiles set web_search_enabled=?, updated_at=? where id=?",
            (1 if bool(normalized["web_search_enabled"]) else 0, time.time(), profile_id),
        )

    def _with_voice_derivatives(self, conn: sqlite3.Connection, normalized: dict[str, Any]) -> dict[str, Any]:
        normalized = self._with_qwen_voice_derivatives(conn, normalized)
        return self._with_omnivoice_voice_derivatives(conn, normalized)

    def _with_qwen_voice_derivatives(self, conn: sqlite3.Connection, normalized: dict[str, Any]) -> dict[str, Any]:
        qwen_keys = {
            "qwentts_cpp_voice_mode",
            "qwentts_cpp_voice_preset",
            "qwentts_cpp_voice_design",
            "qwentts_cpp_clone_voice_id",
            "qwentts_cpp_base_model",
            "qwentts_cpp_customvoice_model",
            "qwentts_cpp_voicedesign_model",
        }
        if not (set(normalized) & qwen_keys):
            return normalized

        rows = conn.execute("select key, value_json from settings").fetchall()
        current = {row["key"]: json.loads(row["value_json"]) for row in rows}
        merged = {**current, **normalized}
        mode = str(merged.get("qwentts_cpp_voice_mode") or "default").strip().lower()
        base_model = str(merged.get("qwentts_cpp_base_model") or "")
        custom_model = str(merged.get("qwentts_cpp_customvoice_model") or "")
        design_model = str(merged.get("qwentts_cpp_voicedesign_model") or "")

        derived = {
            "qwentts_cpp_speaker": "",
            "qwentts_cpp_instruct": "",
            "qwentts_cpp_ref_wav": "",
            "qwentts_cpp_ref_text": "",
            "qwentts_cpp_ref_spk": "",
            "qwentts_cpp_ref_rvq": "",
        }
        if mode == "preset":
            derived["qwentts_cpp_model"] = custom_model
            derived["qwentts_cpp_speaker"] = str(merged.get("qwentts_cpp_voice_preset") or "")
        elif mode == "design":
            derived["qwentts_cpp_model"] = design_model
            derived["qwentts_cpp_instruct"] = str(merged.get("qwentts_cpp_voice_design") or "")
        elif mode == "clone":
            derived["qwentts_cpp_model"] = base_model
            clone_id = str(merged.get("qwentts_cpp_clone_voice_id") or "")
            clone = conn.execute("select * from voice_profiles where id=?", (clone_id,)).fetchone()
            if clone:
                clone_dict = dict(clone)
                derived["qwentts_cpp_ref_wav"] = clone_dict.get("ref_wav", "") or ""
                derived["qwentts_cpp_ref_text"] = clone_dict.get("ref_text", "") or ""
                derived["qwentts_cpp_ref_spk"] = clone_dict.get("ref_spk", "") or ""
                derived["qwentts_cpp_ref_rvq"] = clone_dict.get("ref_rvq", "") or ""
        else:
            derived["qwentts_cpp_voice_mode"] = "default"
            derived["qwentts_cpp_model"] = base_model
        return {**normalized, **derived}

    def _with_omnivoice_voice_derivatives(self, conn: sqlite3.Connection, normalized: dict[str, Any]) -> dict[str, Any]:
        omni_keys = {
            "omnivoice_voice_mode",
            "omnivoice_voice_design",
            "omnivoice_clone_voice_id",
        }
        if not (set(normalized) & omni_keys):
            return normalized

        rows = conn.execute("select key, value_json from settings").fetchall()
        current = {row["key"]: json.loads(row["value_json"]) for row in rows}
        merged = {**current, **normalized}
        mode = str(merged.get("omnivoice_voice_mode") or "auto").strip().lower()
        derived = {
            "omnivoice_ref_wav": "",
            "omnivoice_ref_text": "",
            "omnivoice_ref_rvq": "",
        }
        if mode == "clone":
            clone_id = str(merged.get("omnivoice_clone_voice_id") or "")
            clone = conn.execute("select * from voice_profiles where id=?", (clone_id,)).fetchone()
            if clone:
                clone_dict = dict(clone)
                derived["omnivoice_ref_wav"] = clone_dict.get("ref_wav", "") or ""
                derived["omnivoice_ref_text"] = clone_dict.get("ref_text", "") or ""
                derived["omnivoice_ref_rvq"] = clone_dict.get("omnivoice_ref_rvq", "") or ""
        elif mode == "design":
            derived["omnivoice_voice_design"] = str(merged.get("omnivoice_voice_design") or "")
        else:
            derived["omnivoice_voice_mode"] = "auto"
        return {**normalized, **derived}

    def persona_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from persona_profiles order by updated_at").fetchall()
        return [dict(row) for row in rows]

    def persona_profile(self, persona_id: str) -> dict[str, Any] | None:
        if not persona_id:
            return None
        with self.connect() as conn:
            row = conn.execute("select * from persona_profiles where id=?", (persona_id,)).fetchone()
        return dict(row) if row else None

    def upsert_persona(self, profile: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("delete from deleted_persona_profiles where id=?", (profile["id"],))
            conn.execute(
                """
                insert or replace into persona_profiles
                (id, name, prompt, voice_mode, voice_ref, updated_at)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["id"],
                    profile["name"],
                    profile["prompt"],
                    profile.get("voice_mode", "default"),
                    profile.get("voice_ref", ""),
                    time.time(),
                ),
            )

    def delete_persona(self, persona_id: str) -> bool:
        now = time.time()
        with self.connect() as conn:
            cursor = conn.execute("delete from persona_profiles where id=?", (persona_id,))
            if cursor.rowcount <= 0:
                return False
            conn.execute(
                "insert or replace into deleted_persona_profiles values (?, ?)",
                (persona_id, now),
            )
            return True

    def voice_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from voice_profiles order by updated_at desc").fetchall()
        return [dict(row) for row in rows]

    def voice_profile(self, voice_id: str) -> dict[str, Any] | None:
        if not voice_id:
            return None
        with self.connect() as conn:
            row = conn.execute("select * from voice_profiles where id=?", (voice_id,)).fetchone()
        return dict(row) if row else None

    def upsert_voice(self, profile: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into voice_profiles
                (id, name, provider, mode, seed, tags, note, speaker, design_prompt, ref_wav, ref_text, ref_spk, ref_rvq, omnivoice_ref_rvq, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["id"],
                    profile["name"],
                    profile.get("provider", "qwen3tts"),
                    profile.get("mode", "clone"),
                    profile.get("seed"),
                    profile.get("tags", ""),
                    profile.get("note", ""),
                    profile.get("speaker", ""),
                    profile.get("design_prompt", ""),
                    profile.get("ref_wav", ""),
                    profile.get("ref_text", ""),
                    profile.get("ref_spk", ""),
                    profile.get("ref_rvq", ""),
                    profile.get("omnivoice_ref_rvq", ""),
                    time.time(),
                ),
            )

    def delete_voice(self, voice_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("delete from voice_profiles where id=?", (voice_id,))
            return cursor.rowcount > 0

    def llm_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from llm_profiles order by updated_at desc").fetchall()
        return [_llm_profile_row(dict(row)) for row in rows]

    def llm_profile(self, profile_id: str) -> dict[str, Any] | None:
        if not profile_id:
            return None
        with self.connect() as conn:
            row = conn.execute("select * from llm_profiles where id=?", (profile_id,)).fetchone()
        return _llm_profile_row(dict(row)) if row else None

    def upsert_llm_profile(self, profile: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into llm_profiles
                (id, name, provider, base_url, model, api_key, max_tokens, timeout_seconds,
                 voice_no_think, wait_fillers_enabled, max_wait_seconds, fallback_enabled,
                 web_search_enabled, notes, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["id"],
                    profile["name"],
                    profile["provider"],
                    profile["base_url"],
                    profile["model"],
                    profile.get("api_key", ""),
                    int(profile.get("max_tokens", 220)),
                    float(profile.get("timeout_seconds", 45.0)),
                    1 if profile.get("voice_no_think", True) else 0,
                    1 if profile.get("wait_fillers_enabled", False) else 0,
                    float(profile.get("max_wait_seconds", 60.0)),
                    1 if profile.get("fallback_enabled", True) else 0,
                    1 if profile.get("web_search_enabled", False) else 0,
                    str(profile.get("notes", ""))[:500],
                    time.time(),
                ),
            )

    def delete_llm_profile(self, profile_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("delete from llm_profiles where id=?", (profile_id,))
            return cursor.rowcount > 0

    def settings_for_llm_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        provider = str(profile.get("provider") or "hermes_agent")
        base_url = str(profile.get("base_url") or "")
        model = str(profile.get("model") or "")
        api_key = str(profile.get("api_key") or "")
        max_tokens = int(profile.get("max_tokens") or 220)
        timeout = float(profile.get("timeout_seconds") or 45.0)
        max_wait = float(profile.get("max_wait_seconds") or 60.0)
        wait_fillers = bool(profile.get("wait_fillers_enabled"))
        fallback = bool(profile.get("fallback_enabled", True))
        web_search = bool(profile.get("web_search_enabled"))
        values = {
            "active_llm_profile_id": profile["id"],
            "llm_provider": provider,
            "hermes_voice_no_think": bool(profile.get("voice_no_think", True)),
            "hermes_max_fillers": 1 if wait_fillers else 0,
            "hermes_agent_max_wait_seconds": max_wait,
            "hermes_allow_fallback": fallback,
            "llm_fallback_enabled": fallback,
            "web_search_enabled": web_search,
        }
        if provider == "hermes_agent":
            values.update(
                {
                    "hermes_base_url": base_url,
                    "hermes_model": model,
                    "hermes_api_key": api_key,
                    "hermes_max_tokens": max_tokens,
                    "hermes_timeout_seconds": timeout,
                    "hermes_read_timeout_seconds": timeout,
                    "llm_base_url": base_url,
                    "llm_model": model,
                    "llm_api_key": api_key,
                    "llm_max_tokens": max_tokens,
                    "llm_timeout_seconds": timeout,
                }
            )
        else:
            values.update(
                {
                    "llm_base_url": base_url,
                    "llm_model": model,
                    "llm_api_key": api_key,
                    "llm_max_tokens": max_tokens,
                    "llm_timeout_seconds": timeout,
                    "hermes_base_url": base_url,
                    "hermes_model": model,
                    "hermes_api_key": api_key,
                    "hermes_max_tokens": max_tokens,
                    "hermes_timeout_seconds": timeout,
                }
            )
        return values

    def add_metric(self, kind: str, value: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "insert into runtime_metrics(kind, value_json, created_at) values (?, ?, ?)",
                (kind, json.dumps(value, ensure_ascii=False), time.time()),
            )
            conn.execute(
                """
                delete from runtime_metrics
                where id not in (select id from runtime_metrics order by created_at desc limit 300)
                """
            )

    def metrics(self, limit: int = 120) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "select * from runtime_metrics order by created_at desc limit ?", (limit,)
            ).fetchall()
        return [
            {"id": row["id"], "kind": row["kind"], "value": json.loads(row["value_json"]), "created_at": row["created_at"]}
            for row in rows
        ]


def _coerce_attr(type_hint: Any, value: Any) -> Any:
    text = str(type_hint)
    if "bool" in text:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if "int" in text and not isinstance(value, bool):
        return int(value)
    if "float" in text:
        return float(value)
    if "Path" in text:
        return Path(_resolve_path(str(value)))
    return value


def _resolve_path(raw: str) -> str:
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return str(path)


def _path_exists(raw: Any) -> bool:
    return bool(raw and Path(str(raw)).expanduser().is_file())


def _clone_has_audio_refs(profile: dict[str, Any]) -> bool:
    return any(
        _path_exists(profile.get(key))
        for key in ("ref_wav", "ref_spk", "ref_rvq")
    )


def _llm_profile_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("voice_no_think", "wait_fillers_enabled", "fallback_enabled", "web_search_enabled"):
        row[key] = bool(row.get(key))
    return row
