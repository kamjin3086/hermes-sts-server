from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.request import urlretrieve

import httpx
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from hermes_sts.config import ROOT, Settings
from hermes_sts.config_store import ATTR_TO_ENV, ENV_TO_ATTR, ConfigStore
from hermes_sts.persona import build_persona_instructions
from hermes_sts.tts import TtsVoice, build_tts

logger = logging.getLogger(__name__)

QWEN_SPEAKERS = [
    "serena",
    "vivian",
    "uncle_fu",
    "ryan",
    "aiden",
    "ono_anna",
    "sohee",
    "eric",
    "dylan",
]

QWEN_MODEL_FILES = {
    "base": "qwen-talker-1.7b-base-Q4_K_M.gguf",
    "customvoice": "qwen-talker-1.7b-customvoice-Q4_K_M.gguf",
    "voicedesign": "qwen-talker-1.7b-voicedesign-Q4_K_M.gguf",
    "codec": "qwen-tokenizer-12hz-Q4_K_M.gguf",
}
QWEN_MODEL_REPO = "https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF/resolve/main"
SERVER_STARTED_AT = time.time()


class SettingsPatch(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class PersonaPatch(BaseModel):
    id: str
    name: str
    prompt: str
    voice_mode: str = "default"
    voice_ref: str = ""
    apply: bool = True


class ModelInstallRequest(BaseModel):
    kinds: list[str] = Field(default_factory=list)


class CloneEncodeRequest(BaseModel):
    voice_id: str


class SeedVoiceRequest(BaseModel):
    name: str = "收藏声线"
    seed: int
    tags: list[str] = Field(default_factory=list)


class ApplyVoiceRequest(BaseModel):
    voice_id: str


class WorkshopSuggestRequest(BaseModel):
    brief: str
    persona_hint: str = ""


class PreviewRequest(BaseModel):
    text: str = "你好，我是 Hermes STS。现在使用当前角色和音色进行试听。"
    voice_mode: str | None = None
    speaker: str | None = None
    design_prompt: str | None = None
    clone_voice_id: str | None = None
    seed: int | None = None


def create_admin_router(settings: Settings, rebuild_components) -> APIRouter:
    store = ConfigStore.default()
    router = APIRouter()

    def refresh_settings() -> Settings:
        new_settings = store.load_settings()
        for key, value in vars(new_settings).items():
            object.__setattr__(settings, key, value)
        return settings

    @router.get("/", response_class=HTMLResponse)
    async def index():
        index_path = ROOT / "admin_ui" / "dist" / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return HTMLResponse(_fallback_html())

    @router.get("/assets/{path:path}")
    async def admin_asset(path: str):
        asset = (ROOT / "admin_ui" / "dist" / "assets" / path).resolve()
        assets_root = (ROOT / "admin_ui" / "dist" / "assets").resolve()
        if not str(asset).startswith(str(assets_root)) or not asset.exists():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(asset)

    @router.get("/api/admin/state")
    async def admin_state() -> dict[str, Any]:
        current = refresh_settings()
        return {
            "health": _health(current),
            "settings": _settings_payload(current, store),
            "setup": {
                "complete": store.setup_complete(),
                "env_imported": False,
            },
            "runtime": {
                "started_at": SERVER_STARTED_AT,
                "uptime_seconds": max(0, int(time.time() - SERVER_STARTED_AT)),
            },
            "personas": store.persona_profiles(),
            "voices": store.voice_profiles(),
            "qwen": {
                "speakers": QWEN_SPEAKERS,
                "models": _qwen_model_status(current),
                "modes": ["default", "preset", "design", "clone"],
            },
            "kokoro_voices": _kokoro_voice_options(),
            "metrics": store.metrics(80),
        }

    @router.get("/api/settings")
    async def legacy_settings() -> dict[str, Any]:
        current = refresh_settings()
        return {
            "health": _health(current),
            "categories": _legacy_categories(current, store),
            "kokoro_voices": _kokoro_voice_options(),
            "config_db": str(settings.config_db),
        }

    @router.patch("/api/settings")
    async def patch_settings(payload: SettingsPatch) -> dict[str, Any]:
        _validate_settings_patch(payload.values)
        previous = {
            ENV_TO_ATTR.get(key, key): getattr(settings, ENV_TO_ATTR.get(key, key))
            for key in payload.values
            if hasattr(settings, ENV_TO_ATTR.get(key, key))
        }
        changed = store.set_settings(payload.values)
        current = refresh_settings()
        if _requires_rebuild(changed):
            try:
                rebuild_components()
            except Exception as exc:
                if previous:
                    store.set_settings(previous)
                    refresh_settings()
                raise HTTPException(status_code=500, detail=f"component rebuild failed: {exc}") from exc
        return {
            "changed": changed,
            "restart_required": [],
            "rebuild_required": _requires_rebuild(changed),
            "health": _health(current),
            "state": await admin_state(),
        }

    @router.post("/api/setup/import-env")
    async def import_env() -> dict[str, Any]:
        refresh_settings()
        return {"ok": False, "deprecated": True, "state": await admin_state()}

    @router.post("/api/setup/complete")
    async def complete_setup() -> dict[str, Any]:
        store.set_setup_value("complete", True)
        return {"ok": True}

    @router.post("/api/personas")
    async def upsert_persona(payload: PersonaPatch) -> dict[str, Any]:
        if payload.voice_mode not in {"default", "preset", "design", "clone"}:
            raise HTTPException(status_code=422, detail=f"unsupported qwen voice mode: {payload.voice_mode}")
        if payload.voice_mode == "preset" and payload.voice_ref and payload.voice_ref not in QWEN_SPEAKERS:
            raise HTTPException(status_code=422, detail=f"unsupported qwen speaker: {payload.voice_ref}")
        profile = payload.model_dump()
        profile.pop("apply", None)
        store.upsert_persona(profile)
        if not payload.apply:
            return {"ok": True, "applied": False, "state": await admin_state()}
        values = {
            "sts_persona_preset": payload.id,
            "sts_persona_custom": payload.prompt,
            "qwentts_cpp_voice_mode": payload.voice_mode,
        }
        voice_ref_key = _voice_ref_key(payload.voice_mode)
        if voice_ref_key:
            values[voice_ref_key] = payload.voice_ref
        changed = store.set_settings(values)
        refresh_settings()
        if _requires_rebuild(changed):
            rebuild_components()
        return {"ok": True, "applied": True, "state": await admin_state()}

    @router.delete("/api/personas/{persona_id}")
    async def delete_persona(persona_id: str) -> dict[str, Any]:
        if not store.persona_profile(persona_id):
            raise HTTPException(status_code=404, detail="persona not found")
        if len(store.persona_profiles()) <= 1:
            raise HTTPException(status_code=422, detail="cannot delete the last persona")
        if not store.delete_persona(persona_id):
            raise HTTPException(status_code=404, detail="persona not found")
        current = refresh_settings()
        changed: dict[str, Any] = {}
        if current.sts_persona_preset == persona_id:
            fallback = store.persona_profiles()[0]
            values = {
                "sts_persona_preset": fallback["id"],
                "sts_persona_custom": fallback["prompt"],
                "qwentts_cpp_voice_mode": fallback.get("voice_mode", "default"),
            }
            voice_ref_key = _voice_ref_key(values["qwentts_cpp_voice_mode"])
            if voice_ref_key:
                values[voice_ref_key] = fallback.get("voice_ref", "")
            changed = store.set_settings(values)
            refresh_settings()
        if _requires_rebuild(changed):
            rebuild_components()
        return {"ok": True, "state": await admin_state()}

    @router.post("/api/qwen/models/install")
    async def install_qwen_models(payload: ModelInstallRequest | None = None) -> dict[str, Any]:
        current = refresh_settings()
        models_dir = Path(current.qwentts_cpp_base_model).parent
        models_dir.mkdir(parents=True, exist_ok=True)
        kinds = payload.kinds if payload and payload.kinds else list(QWEN_MODEL_FILES)
        unsupported = sorted(set(kinds) - set(QWEN_MODEL_FILES))
        if unsupported:
            raise HTTPException(status_code=422, detail=f"unsupported qwen model kind: {', '.join(unsupported)}")
        for kind in kinds:
            filename = QWEN_MODEL_FILES[kind]
            target = models_dir / filename
            if target.exists():
                continue
            url = f"{QWEN_MODEL_REPO}/{filename}"
            logger.info("Downloading Qwen model %s", url)
            await asyncio.to_thread(urlretrieve, url, target)
        store.set_settings(
            {
                "qwentts_cpp_base_model": str(models_dir / QWEN_MODEL_FILES["base"]),
                "qwentts_cpp_customvoice_model": str(models_dir / QWEN_MODEL_FILES["customvoice"]),
                "qwentts_cpp_voicedesign_model": str(models_dir / QWEN_MODEL_FILES["voicedesign"]),
                "qwentts_cpp_codec": str(models_dir / QWEN_MODEL_FILES["codec"]),
            }
        )
        refresh_settings()
        return {"ok": True, "models": _qwen_model_status(settings), "state": await admin_state()}

    @router.post("/api/qwen/clone/upload")
    async def upload_clone(
        name: str = Form(...),
        reference_text: str = Form(""),
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        safe_id = f"voice_{uuid.uuid4().hex[:12]}"
        voice_dir = settings.data_dir / "voices" / safe_id
        voice_dir.mkdir(parents=True, exist_ok=True)
        wav_path = voice_dir / "reference.wav"
        text_path = voice_dir / "reference.txt"
        with wav_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        text_path.write_text(reference_text.strip(), encoding="utf-8")
        profile = {
            "id": safe_id,
            "name": name.strip() or "克隆音色",
            "provider": "qwen3tts",
            "mode": "clone",
            "ref_wav": str(wav_path),
            "ref_text": str(text_path),
        }
        store.upsert_voice(profile)
        return {"ok": True, "voice": store.voice_profile(safe_id), "state": await admin_state()}

    @router.post("/api/qwen/clone/encode")
    async def encode_clone(payload: CloneEncodeRequest) -> dict[str, Any]:
        current = refresh_settings()
        voice = store.voice_profile(payload.voice_id)
        if not voice:
            raise HTTPException(status_code=404, detail="voice not found")
        ref_wav = Path(voice["ref_wav"])
        if not ref_wav.exists():
            raise HTTPException(status_code=422, detail="reference wav missing")
        codec_bin = Path(current.qwentts_cpp_bin).with_name("qwen-codec")
        if not codec_bin.exists():
            raise HTTPException(status_code=422, detail=f"qwen-codec not found: {codec_bin}")
        result = await asyncio.to_thread(
            subprocess.run,
            [
                str(codec_bin),
                "--model",
                current.qwentts_cpp_codec,
                "--talker",
                current.qwentts_cpp_base_model,
                "-i",
                str(ref_wav),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(ref_wav.parent),
            env={**__import__("os").environ, "GGML_BACKEND": current.qwentts_cpp_backend},
        )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr[-1200:])
        spk = ref_wav.with_suffix(".spk")
        rvq = ref_wav.with_suffix(".rvq")
        profile = dict(voice)
        profile.update({"ref_spk": str(spk), "ref_rvq": str(rvq)})
        store.upsert_voice(profile)
        store.set_settings({"qwentts_cpp_voice_mode": "clone", "qwentts_cpp_clone_voice_id": payload.voice_id})
        refresh_settings()
        rebuild_components()
        return {"ok": True, "voice": store.voice_profile(payload.voice_id), "state": await admin_state()}

    @router.post("/api/qwen/voices/seed")
    async def save_seed_voice(payload: SeedVoiceRequest) -> dict[str, Any]:
        voice_id = f"seed_{uuid.uuid4().hex[:12]}"
        profile = {
            "id": voice_id,
            "name": payload.name.strip() or f"Seed {payload.seed}",
            "provider": "qwen3tts",
            "mode": "seed",
            "seed": int(payload.seed),
            "tags": ",".join(_normalize_tags(payload.tags)),
        }
        store.upsert_voice(profile)
        return {"ok": True, "voice": store.voice_profile(voice_id), "state": await admin_state()}

    @router.post("/api/qwen/voices/apply")
    async def apply_voice(payload: ApplyVoiceRequest) -> dict[str, Any]:
        voice = store.voice_profile(payload.voice_id)
        if not voice:
            raise HTTPException(status_code=404, detail="voice not found")
        values = _settings_for_voice_profile(voice)
        changed = store.set_settings(values)
        refresh_settings()
        if _requires_rebuild(changed):
            rebuild_components()
        return {"ok": True, "voice": voice, "state": await admin_state()}

    @router.delete("/api/qwen/voices/{voice_id}")
    async def delete_voice(voice_id: str) -> dict[str, Any]:
        if voice_id == "qwen-default":
            raise HTTPException(status_code=422, detail="default voice cannot be deleted")
        if not store.delete_voice(voice_id):
            raise HTTPException(status_code=404, detail="voice not found")
        current = refresh_settings()
        active_clone = current.qwentts_cpp_voice_mode == "clone" and current.qwentts_cpp_clone_voice_id == voice_id
        if active_clone:
            store.set_settings({"qwentts_cpp_voice_mode": "default", "qwentts_cpp_clone_voice_id": ""})
            refresh_settings()
            rebuild_components()
        return {"ok": True, "state": await admin_state()}

    @router.post("/api/qwen/workshop/suggest")
    async def suggest_voice(payload: WorkshopSuggestRequest) -> dict[str, Any]:
        current = refresh_settings()
        suggestion = await _suggest_voice_with_llm(payload, current)
        return {"ok": True, "suggestion": suggestion}

    @router.post("/api/tts/preview")
    async def tts_preview(payload: PreviewRequest) -> dict[str, Any]:
        current = refresh_settings()
        preview_settings = replace(current, qwentts_cpp_seed=payload.seed) if payload.seed is not None else current
        voice = _preview_voice(payload, preview_settings, store)
        started = time.perf_counter()
        pcm = await build_tts(preview_settings).synthesize(payload.text, voice=voice)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        wav = _pcm_to_wav_bytes(pcm, preview_settings.sample_rate)
        audio_seconds = len(pcm) / 2 / preview_settings.sample_rate
        store.add_metric(
            "tts_preview",
            {
                "elapsed_ms": elapsed_ms,
                "audio_seconds": audio_seconds,
                "rtf": elapsed_ms / 1000 / max(audio_seconds, 0.001),
                "seed": preview_settings.qwentts_cpp_seed,
            },
        )
        return {
            "ok": True,
            "elapsed_ms": elapsed_ms,
            "audio_seconds": audio_seconds,
            "seed": preview_settings.qwentts_cpp_seed,
            "audio_wav_base64": base64.b64encode(wav).decode("ascii"),
        }

    @router.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return {"metrics": store.metrics(120)}

    @router.get("/api/logs")
    async def logs(lines: int = 120) -> dict[str, str]:
        safe_lines = max(20, min(lines, 500))
        return {
            "stdout": _tail(settings.log_dir / "sts-server.out.log", safe_lines),
            "stderr": _tail(settings.log_dir / "sts-server.err.log", safe_lines),
        }

    @router.get("/api/diagnostics/hermes")
    async def hermes_diagnostics() -> dict[str, Any]:
        current = refresh_settings()
        headers = {}
        if current.hermes_api_key:
            headers["Authorization"] = f"Bearer {current.hermes_api_key}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{current.hermes_base_url.rstrip('/')}/models", headers=headers)
                return {"ok": resp.is_success, "status_code": resp.status_code, "body": resp.json()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return router


def _settings_payload(settings: Settings, store: ConfigStore) -> dict[str, Any]:
    data = store.settings_dict()
    visible = {
        "server": ["host", "port", "log_level"],
        "llm": ["hermes_base_url", "hermes_model", "hermes_api_key", "hermes_max_tokens", "hermes_voice_no_think"],
        "stt": ["stt_provider", "sherpa_sensevoice_model", "sherpa_sensevoice_tokens"],
        "tts": [
            "tts_provider",
            "tts_voice_source",
            "sts_persona_source",
            "sts_persona_preset",
            "sts_persona_custom",
            "qwentts_cpp_voice_mode",
            "qwentts_cpp_voice_preset",
            "qwentts_cpp_voice_design",
            "qwentts_cpp_clone_voice_id",
            "qwentts_cpp_backend",
            "qwentts_cpp_seed",
            "dashboard_wave_style",
            "sherpa_kokoro_voice",
        ],
        "conversation": [
            "vad_threshold",
            "vad_min_silence_seconds",
            "hermes_first_filler_delay_seconds",
            "suppress_input_while_speaking",
            "tts_segment_min_chars",
            "tts_segment_max_chars",
        ],
        "advanced": [
            "qwentts_cpp_bin",
            "qwentts_cpp_base_model",
            "qwentts_cpp_customvoice_model",
            "qwentts_cpp_voicedesign_model",
            "qwentts_cpp_codec",
            "qwentts_cpp_extra_args",
            "qwentts_cpp_seed",
        ],
    }
    return {
        "values": {key: getattr(settings, key) for keys in visible.values() for key in keys if hasattr(settings, key)},
        "groups": visible,
        "raw": data,
    }


def _legacy_categories(settings: Settings, store: ConfigStore) -> list[dict[str, Any]]:
    payload = _settings_payload(settings, store)
    return [
        {
            "name": group,
            "settings": [
                {
                    "env": ATTR_TO_ENV.get(key, key),
                    "key": key,
                    "label": _label_for(key),
                    "kind": "password" if key.endswith("api_key") else "text",
                    "value": str(value),
                    "help": "",
                    "choices": [],
                    "choice_labels": {},
                    "live": True,
                    "secret": key.endswith("api_key"),
                }
                for key, value in payload["values"].items()
                if key in keys
            ],
        }
        for group, keys in payload["groups"].items()
    ]


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
        "persona_source": settings.sts_persona_source,
        "persona_preset": settings.sts_persona_preset,
        "persona_prompt": build_persona_instructions(settings),
        "voice": settings.sherpa_kokoro_voice,
        "tts_voice_source": settings.tts_voice_source,
        "qwen_voice_mode": settings.qwentts_cpp_voice_mode,
        "qwen_speaker": settings.qwentts_cpp_voice_preset or settings.qwentts_cpp_speaker,
        "qwen_backend": settings.qwentts_cpp_backend,
        "qwen_clone": bool(settings.qwentts_cpp_ref_wav or settings.qwentts_cpp_ref_spk or settings.qwentts_cpp_ref_rvq),
    }


def _qwen_model_status(settings: Settings) -> dict[str, Any]:
    paths = {
        "base": settings.qwentts_cpp_base_model,
        "customvoice": settings.qwentts_cpp_customvoice_model,
        "voicedesign": settings.qwentts_cpp_voicedesign_model,
        "codec": settings.qwentts_cpp_codec,
    }
    return {
        key: {"path": value, "installed": bool(value and Path(value).exists())}
        for key, value in paths.items()
    }


def _requires_rebuild(changed: dict[str, Any]) -> bool:
    keys = set(changed)
    rebuild_keys = {
        "stt_provider",
        "tts_provider",
        "llm_provider",
        "qwentts_cpp_voice_mode",
        "qwentts_cpp_voice_preset",
        "qwentts_cpp_voice_design",
        "qwentts_cpp_clone_voice_id",
        "qwentts_cpp_bin",
        "qwentts_cpp_base_model",
        "qwentts_cpp_customvoice_model",
        "qwentts_cpp_voicedesign_model",
        "qwentts_cpp_codec",
        "qwentts_cpp_backend",
        "qwentts_cpp_seed",
        "sherpa_kokoro_model",
        "sherpa_kokoro_voices",
        "sherpa_kokoro_tokens",
        "sherpa_kokoro_lexicon",
        "sherpa_kokoro_data_dir",
        "sherpa_sensevoice_model",
        "sherpa_sensevoice_tokens",
    }
    return bool(keys & rebuild_keys)


def _validate_settings_patch(values: dict[str, Any]) -> None:
    mode = values.get("qwentts_cpp_voice_mode")
    if mode is not None and mode not in {"default", "preset", "design", "clone"}:
        raise HTTPException(status_code=422, detail=f"unsupported qwen voice mode: {mode}")
    speaker = values.get("qwentts_cpp_voice_preset")
    if speaker and speaker not in QWEN_SPEAKERS:
        raise HTTPException(status_code=422, detail=f"unsupported qwen speaker: {speaker}")
    seed = values.get("qwentts_cpp_seed")
    if seed is not None:
        try:
            int(seed)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="qwen seed must be an integer") from exc
    provider = values.get("tts_provider")
    if provider is not None and provider not in {"qwen3tts", "sherpa_kokoro", "tone", "sapi", "sherpa_onnx"}:
        raise HTTPException(status_code=422, detail=f"unsupported tts provider: {provider}")


def _voice_ref_key(mode: str) -> str:
    if mode == "preset":
        return "qwentts_cpp_voice_preset"
    if mode == "design":
        return "qwentts_cpp_voice_design"
    if mode == "clone":
        return "qwentts_cpp_clone_voice_id"
    return ""


def _settings_for_voice_profile(voice: dict[str, Any]) -> dict[str, Any]:
    mode = str(voice.get("mode") or "default")
    if mode == "seed":
        return {"qwentts_cpp_voice_mode": "default", "qwentts_cpp_seed": int(voice.get("seed") or 42)}
    if mode == "preset":
        return {"qwentts_cpp_voice_mode": "preset", "qwentts_cpp_voice_preset": voice.get("speaker", "")}
    if mode == "design":
        return {"qwentts_cpp_voice_mode": "design", "qwentts_cpp_voice_design": voice.get("design_prompt", "")}
    if mode == "clone":
        return {"qwentts_cpp_voice_mode": "clone", "qwentts_cpp_clone_voice_id": voice.get("id", "")}
    return {"qwentts_cpp_voice_mode": "default"}


async def _suggest_voice_with_llm(payload: WorkshopSuggestRequest, settings: Settings) -> dict[str, Any]:
    brief = payload.brief.strip()
    if not brief:
        raise HTTPException(status_code=422, detail="brief is required")
    messages = [
        {
            "role": "system",
            "content": (
                "你是语音助手音色设计师。根据用户想要的气质，生成 Qwen3TTS 可用的音色方案。"
                "只返回 JSON，不要 Markdown。字段必须包含："
                "name, persona_prompt, voice_mode, design_prompt, seed, tags, preview_text, notes。"
                "voice_mode 只能是 default 或 design。"
                "design_prompt 用英文短语，适合 Qwen3TTS VoiceDesign，例如 gender, age, pitch, tone, accent, pace。"
                "seed 是 1 到 2147483647 的整数，用于 Base 默认音色微调。"
                "persona_prompt 用中文，适合语音助手系统提示词，克制、明确、可长期使用。"
                "tags 是 2 到 4 个中文短标签，例如：沉稳、清晰、冷感、亲和、播报、低频、甜、快速。"
            ),
        },
        {
            "role": "user",
            "content": f"需求：{brief}\n当前人格参考：{payload.persona_hint[:1200]}",
        },
    ]
    body = {
        "model": settings.hermes_model or settings.llm_model,
        "messages": messages,
        "stream": False,
        "max_tokens": min(max(settings.hermes_max_tokens, 300), 900),
        "temperature": 0.7,
    }
    headers = {}
    if settings.hermes_api_key:
        headers["Authorization"] = f"Bearer {settings.hermes_api_key}"
    try:
        async with httpx.AsyncClient(timeout=settings.hermes_timeout_seconds) as client:
            resp = await client.post(f"{settings.hermes_base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM suggestion failed: {exc}") from exc

    content = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    try:
        parsed = json.loads(_extract_json_object(content))
    except Exception:
        parsed = {}
    seed = parsed.get("seed")
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = int(time.time() * 1000) % 2147483647 or 42
    seed = max(1, min(seed, 2147483647))
    mode = str(parsed.get("voice_mode") or "design").strip().lower()
    if mode not in {"default", "design"}:
        mode = "design"
    return {
        "name": str(parsed.get("name") or "AI 音色方案")[:80],
        "persona_prompt": str(parsed.get("persona_prompt") or "").strip(),
        "voice_mode": mode,
        "design_prompt": str(parsed.get("design_prompt") or "").strip(),
        "seed": seed,
        "tags": _normalize_tags(parsed.get("tags")),
        "preview_text": str(parsed.get("preview_text") or "你好，我正在用新的声线和你说话。").strip(),
        "notes": str(parsed.get("notes") or "").strip(),
        "raw": content,
    }


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _normalize_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        raw_tags: list[Any] = re.split(r"[，,\s]+", tags)
    elif isinstance(tags, list):
        raw_tags = tags
    else:
        raw_tags = []
    normalized: list[str] = []
    for tag in raw_tags:
        value = str(tag).strip().replace(",", " ")
        if value and value not in normalized:
            normalized.append(value[:12])
        if len(normalized) >= 6:
            break
    return normalized


def _preview_voice(payload: PreviewRequest, settings: Settings, store: ConfigStore) -> TtsVoice:
    mode = (payload.voice_mode or settings.qwentts_cpp_voice_mode).strip().lower()
    if mode == "preset":
        return TtsVoice(speaker=payload.speaker or settings.qwentts_cpp_voice_preset)
    if mode == "design":
        return TtsVoice(instruct=payload.design_prompt or settings.qwentts_cpp_voice_design)
    if mode == "clone":
        voice_id = payload.clone_voice_id or settings.qwentts_cpp_clone_voice_id
        voice = store.voice_profile(voice_id)
        if voice:
            return TtsVoice(
                ref_wav=voice.get("ref_wav", ""),
                ref_text=voice.get("ref_text", ""),
                ref_spk=voice.get("ref_spk", ""),
                ref_rvq=voice.get("ref_rvq", ""),
            )
    return TtsVoice.from_settings(settings)


def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    import io

    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return out.getvalue()


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


def _label_for(key: str) -> str:
    return key.replace("_", " ").title()


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _fallback_html() -> str:
    return """
<!doctype html>
<html lang="zh-CN">
  <head><meta charset="utf-8"><title>Hermes STS 控制台</title></head>
  <body style="font-family: system-ui; background:#111; color:#eee; padding:32px">
    <h1>Hermes STS 控制台</h1>
    <p>前端构建产物不存在。请运行 <code>cd admin_ui && npm install && npm run build</code>。</p>
  </body>
</html>
"""
