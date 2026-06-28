from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
import wave
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlretrieve

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
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
SERVICE_NAME = os.getenv("HERMES_STS_SYSTEMD_SERVICE_NAME", "hermes-sts-server.service")


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
    note: str = ""


class DesignVoiceRequest(BaseModel):
    name: str = "描述造声音色"
    design_prompt: str
    tags: list[str] = Field(default_factory=list)
    note: str = ""


class ApplyVoiceRequest(BaseModel):
    voice_id: str


class WorkshopSuggestRequest(BaseModel):
    brief: str
    persona_hint: str = ""
    scenario: str = "persona"
    current_voice: str = ""


class PersonaOptimizeRequest(BaseModel):
    prompt: str
    name: str = ""


class LlmProfilePatch(BaseModel):
    id: str = ""
    name: str
    provider: str = "hermes_agent"
    base_url: str
    model: str
    api_key: str = ""
    max_tokens: int = 220
    timeout_seconds: float = 45.0
    voice_no_think: bool = True
    wait_fillers_enabled: bool = False
    max_wait_seconds: float = 60.0
    fallback_enabled: bool = True
    web_search_enabled: bool = False
    notes: str = ""


class PreviewRequest(BaseModel):
    text: str = "你好，我是 Hermes STS。现在使用当前角色和音色进行试听。"
    voice_mode: str | None = None
    speaker: str | None = None
    design_prompt: str | None = None
    clone_voice_id: str | None = None
    seed: int | None = None


class MemoryAddRequest(BaseModel):
    content: str
    category: str = "manual"
    tags: list[str] = Field(default_factory=list)


class MemoryUpdateRequest(BaseModel):
    uri: str
    content: str
    category: str | None = None
    tags: list[str] | None = None


class MemoryRecallRequest(BaseModel):
    query: str
    limit: int = 5
    min_score: float = 0.0


def create_admin_router(
    settings: Settings,
    rebuild_components,
    get_llm: Callable[[], Any] | None = None,
    get_memory: Callable[[], Any] | None = None,
    get_tools: Callable[[], Any] | None = None,
    get_conversation_store: Callable[[], Any] | None = None,
    get_web_search: Callable[[], Any] | None = None,
) -> APIRouter:
    store = ConfigStore.default()
    router = APIRouter()

    def refresh_settings() -> Settings:
        new_settings = store.load_settings()
        for key, value in vars(new_settings).items():
            object.__setattr__(settings, key, value)
        logger.info(
            "DBG refresh_settings persona_preset=%s persona_custom=%.80s voice_mode=%s voice_source=%s persona_source=%s",
            settings.sts_persona_preset,
            settings.sts_persona_custom or "(empty)",
            settings.qwentts_cpp_voice_mode,
            settings.tts_voice_source,
            settings.sts_persona_source,
        )
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
        memory = get_memory() if get_memory else None
        tools = get_tools() if get_tools else None
        conv_store = get_conversation_store() if get_conversation_store else None
        web_search = get_web_search() if get_web_search else None
        metrics = store.metrics(80)
        return {
            "health": _health(current),
            "llm_context": _llm_context_payload(get_llm() if get_llm else None, current),
            "diagnostics": _diagnostics_payload(current, metrics, memory=memory, tools=tools, web_search=web_search),
            "conversation": _conversation_payload(conv_store),
            "web_search": _web_search_payload(current, web_search),
            "settings": _settings_payload(current, store),
            "llm_profiles": store.llm_profiles(),
            "active_llm_profile_id": current.active_llm_profile_id,
            "tools": _tools_payload(tools),
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
            "metrics": metrics,
            "memory": memory.stats() if memory else None,
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
        logger.info(
            "DBG PATCH /api/settings incoming keys=%s",
            sorted(payload.values),
        )
        _validate_settings_patch(payload.values)
        changed = store.set_settings(payload.values)
        current = refresh_settings()
        restart_scheduled = False
        if _requires_rebuild(changed):
            restart_scheduled = _schedule_service_restart("settings changed")
        return {
            "changed": changed,
            "restart_required": list(changed) if restart_scheduled else [],
            "restart_scheduled": restart_scheduled,
            "rebuild_required": _requires_rebuild(changed),
            "health": _health(current),
            "state": await admin_state(),
        }

    @router.post("/api/settings/reapply")
    async def reapply_settings() -> dict[str, Any]:
        current = refresh_settings()
        restart_scheduled = _schedule_service_restart("manual reapply")
        return {
            "ok": True,
            "restart_scheduled": restart_scheduled,
            "restart_required": ["service"],
            "health": _health(current),
            "state": await admin_state(),
        }

    @router.post("/api/settings/reset-default")
    async def reset_settings_default(payload: SettingsPatch) -> dict[str, Any]:
        for key in payload.values:
            store.delete_setting(key)
        refresh_settings()
        return {"ok": True, "state": await admin_state()}

    @router.post("/api/setup/import-env")
    async def import_env() -> dict[str, Any]:
        refresh_settings()
        return {"ok": False, "deprecated": True, "state": await admin_state()}

    @router.post("/api/setup/complete")
    async def complete_setup() -> dict[str, Any]:
        store.set_setup_value("complete", True)
        return {"ok": True}

    def _conversation_store(request: Request):
        store = getattr(request.app.state, "conversation_store", None)
        if store is None:
            raise HTTPException(status_code=400, detail="conversations disabled")
        return store

    async def _end_current_conversation(request: Request, reason: str) -> dict[str, Any]:
        """Archive the active conversation and start a new active one.

        Shared by /api/conversations/end and the repurposed
        /api/llm/context/reset so both expose identical archive + new semantics.
        """
        store = _conversation_store(request)
        turn_gate = request.app.state.turn_gate
        llm = get_llm() if get_llm else None

        async with turn_gate:
            current = await asyncio.to_thread(store.get_active_conversation)
            if current is None:
                return {"id": None, "previous_id": None, "archived": False}
            previous_id = current["id"]
            if llm is not None and callable(getattr(llm, "archive_current_conversation", None)):
                await asyncio.to_thread(llm.archive_current_conversation, reason)
            new_id = await asyncio.to_thread(store.create_conversation)
            if llm is not None:
                llm.conversation_id = new_id
                await asyncio.to_thread(store.reload_history_into, new_id, llm)
            return {"id": new_id, "previous_id": previous_id, "archived": True}

    @router.get("/api/conversations/active")
    async def get_active_conversation(request: Request) -> dict[str, Any]:
        store = _conversation_store(request)
        active = await asyncio.to_thread(store.get_active_conversation)
        if active is None:
            return {"id": None}
        return dict(active)

    @router.get("/api/conversations")
    async def list_conversations(
        request: Request,
        status: str | None = None,
        limit: int = Query(default=10, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        store = _conversation_store(request)
        return await asyncio.to_thread(store.list_conversations, status, limit, offset)

    @router.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(
        request: Request,
        conversation_id: str,
        limit: int = Query(default=80, ge=1, le=300),
    ) -> dict[str, Any]:
        store = _conversation_store(request)
        conversation = await asyncio.to_thread(store.get_conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        messages = await asyncio.to_thread(store.get_messages, conversation_id, limit)
        return {"conversation": conversation, "messages": messages}

    @router.post("/api/conversations/end")
    async def end_conversation(request: Request) -> dict[str, Any]:
        return await _end_current_conversation(request, "admin_end")

    @router.post("/api/llm/context/reset")
    async def reset_llm_context(request: Request) -> dict[str, Any]:
        """Archives the current conversation and starts a new one, mirroring /api/conversations/end."""
        return await _end_current_conversation(request, "admin_reset")

    @router.post("/api/personas")
    async def upsert_persona(payload: PersonaPatch) -> dict[str, Any]:
        if payload.voice_mode not in {"default", "preset", "design", "clone"}:
            raise HTTPException(status_code=422, detail=f"unsupported qwen voice mode: {payload.voice_mode}")
        if payload.voice_mode == "preset" and payload.voice_ref and payload.voice_ref not in QWEN_SPEAKERS:
            raise HTTPException(status_code=422, detail=f"unsupported qwen speaker: {payload.voice_ref}")
        profile = payload.model_dump()
        profile.pop("apply", None)
        store.upsert_persona(profile)
        logger.info(
            "DBG POST /api/personas id=%s apply=%s name=%s voice_mode=%s voice_ref=%s",
            payload.id, payload.apply, payload.name, payload.voice_mode, payload.voice_ref,
        )
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
        restart_scheduled = _restart_if_required(changed, "persona applied")
        return {
            "ok": True,
            "applied": True,
            "restart_scheduled": restart_scheduled,
            "state": await admin_state(),
        }

    @router.post("/api/llm/profiles")
    async def upsert_llm_profile(payload: LlmProfilePatch) -> dict[str, Any]:
        profile = payload.model_dump()
        profile["id"] = _safe_profile_id(profile.get("id") or f"llm_{uuid.uuid4().hex[:12]}")
        _validate_llm_profile(profile)
        store.upsert_llm_profile(profile)
        return {"ok": True, "profile": store.llm_profile(profile["id"]), "state": await admin_state()}

    @router.post("/api/llm/profiles/{profile_id}/apply")
    async def apply_llm_profile(profile_id: str) -> dict[str, Any]:
        profile = store.llm_profile(profile_id)
        if not profile:
            raise HTTPException(status_code=404, detail="llm profile not found")
        changed = store.set_settings(store.settings_for_llm_profile(profile))
        refresh_settings()
        restart_scheduled = _restart_if_required(changed, "llm profile applied")
        return {"ok": True, "profile": profile, "restart_scheduled": restart_scheduled, "state": await admin_state()}

    @router.delete("/api/llm/profiles/{profile_id}")
    async def delete_llm_profile(profile_id: str) -> dict[str, Any]:
        if len(store.llm_profiles()) <= 1:
            raise HTTPException(status_code=422, detail="cannot delete the last llm profile")
        if not store.delete_llm_profile(profile_id):
            raise HTTPException(status_code=404, detail="llm profile not found")
        current = refresh_settings()
        restart_scheduled = False
        if current.active_llm_profile_id == profile_id:
            fallback = store.llm_profiles()[0]
            changed = store.set_settings(store.settings_for_llm_profile(fallback))
            refresh_settings()
            restart_scheduled = _restart_if_required(changed, "active llm profile deleted")
        return {"ok": True, "restart_scheduled": restart_scheduled, "state": await admin_state()}

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
            logger.info(
                "DBG DELETE persona %s was active, falling back to %s",
                persona_id, fallback["id"],
            )
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
        restart_scheduled = _restart_if_required(changed, "active persona deleted")
        return {"ok": True, "restart_scheduled": restart_scheduled, "state": await admin_state()}

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
        changed = store.set_settings(
            {
                "qwentts_cpp_base_model": str(models_dir / QWEN_MODEL_FILES["base"]),
                "qwentts_cpp_customvoice_model": str(models_dir / QWEN_MODEL_FILES["customvoice"]),
                "qwentts_cpp_voicedesign_model": str(models_dir / QWEN_MODEL_FILES["voicedesign"]),
                "qwentts_cpp_codec": str(models_dir / QWEN_MODEL_FILES["codec"]),
            }
        )
        refresh_settings()
        restart_scheduled = _restart_if_required(changed, "qwen models installed")
        return {
            "ok": True,
            "models": _qwen_model_status(settings),
            "restart_scheduled": restart_scheduled,
            "state": await admin_state(),
        }

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
            "note": payload.note.strip()[:240],
        }
        store.upsert_voice(profile)
        return {"ok": True, "voice": store.voice_profile(voice_id), "state": await admin_state()}

    @router.post("/api/qwen/voices/design")
    async def save_design_voice(payload: DesignVoiceRequest) -> dict[str, Any]:
        design_prompt = payload.design_prompt.strip()
        if not design_prompt:
            raise HTTPException(status_code=422, detail="design_prompt is required")
        voice_id = f"design_{uuid.uuid4().hex[:12]}"
        profile = {
            "id": voice_id,
            "name": payload.name.strip() or "描述造声音色",
            "provider": "qwen3tts",
            "mode": "design",
            "design_prompt": design_prompt,
            "tags": ",".join(_normalize_tags(payload.tags)),
            "note": payload.note.strip()[:240],
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
        restart_scheduled = _restart_if_required(changed, "voice applied")
        return {"ok": True, "voice": voice, "restart_scheduled": restart_scheduled, "state": await admin_state()}

    @router.delete("/api/qwen/voices/{voice_id}")
    async def delete_voice(voice_id: str) -> dict[str, Any]:
        if voice_id == "qwen-default":
            raise HTTPException(status_code=422, detail="default voice cannot be deleted")
        if not store.delete_voice(voice_id):
            raise HTTPException(status_code=404, detail="voice not found")
        current = refresh_settings()
        active_clone = current.qwentts_cpp_voice_mode == "clone" and current.qwentts_cpp_clone_voice_id == voice_id
        restart_scheduled = False
        if active_clone:
            changed = store.set_settings({"qwentts_cpp_voice_mode": "default", "qwentts_cpp_clone_voice_id": ""})
            refresh_settings()
            restart_scheduled = _restart_if_required(changed, "active clone voice deleted")
        return {"ok": True, "restart_scheduled": restart_scheduled, "state": await admin_state()}

    @router.post("/api/qwen/workshop/suggest")
    async def suggest_voice(payload: WorkshopSuggestRequest) -> dict[str, Any]:
        current = refresh_settings()
        suggestion = await _suggest_voice_with_llm(payload, current)
        return {"ok": True, "suggestion": suggestion}

    @router.post("/api/persona/optimize")
    async def persona_optimize(payload: PersonaOptimizeRequest) -> dict[str, Any]:
        current = refresh_settings()
        if not payload.prompt.strip():
            raise HTTPException(status_code=422, detail="prompt is required")
        prompt_name = payload.name.strip()[:60] or "当前人格"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专门优化语音助手人格提示词的专家。你的目标：保留原意，去掉冗余，让指令更精炼。\n"
                    "规则：只返回纯文本优化后的提示词，不要任何解释、不要 Markdown、不要编号、不要 JSON。\n"
                    f"当前人格名称：{prompt_name}\n"
                    "优化方向：保持角色核心设定；删除重复或啰嗦的描述；让语言更口语化、更适合短轮次语音对话；"
                    "输出结果不要比原文长。"
                ),
            },
            {
                "role": "user",
                "content": f"请优化以下人格提示词：\n\n{payload.prompt}",
            },
        ]
        body = {
            "model": current.hermes_model or current.llm_model,
            "messages": messages,
            "stream": False,
            "max_tokens": min(max(current.hermes_max_tokens, 300), 900),
            "temperature": 0.5,
        }
        headers = {}
        if current.hermes_api_key:
            headers["Authorization"] = f"Bearer {current.hermes_api_key}"
        try:
            async with httpx.AsyncClient(timeout=current.hermes_timeout_seconds) as client:
                resp = await client.post(f"{current.hermes_base_url.rstrip('/')}/chat/completions", json=body, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM optimize failed: {exc}") from exc
        optimized = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        if not optimized:
            optimized = payload.prompt
        return {"optimized_prompt": optimized}

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

    @router.post("/api/diagnostics/llm")
    async def llm_diagnostics() -> dict[str, Any]:
        current = refresh_settings()
        base_url = current.hermes_base_url if current.llm_provider == "hermes_agent" else current.llm_base_url
        model = current.hermes_model if current.llm_provider == "hermes_agent" else current.llm_model
        api_key = current.hermes_api_key if current.llm_provider == "hermes_agent" else current.llm_api_key
        timeout = min(
            8.0,
            float(current.hermes_connect_timeout_seconds + current.hermes_read_timeout_seconds)
            if current.llm_provider == "hermes_agent"
            else float(current.llm_timeout_seconds),
        )
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "Return a short plain-text OK."},
                            {"role": "user", "content": "ping"},
                        ],
                        "stream": False,
                        "max_tokens": 8,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return {"ok": False, "ms": int((time.perf_counter() - started) * 1000), "error": str(exc)}
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        return {"ok": True, "ms": int((time.perf_counter() - started) * 1000), "model": model, "text": text[:120]}

    @router.post("/api/diagnostics/web-search")
    async def web_search_diagnostics() -> dict[str, Any]:
        current = refresh_settings()
        provider = get_web_search() if get_web_search else None
        if provider is None:
            from hermes_sts.websearch import build_websearch

            provider = build_websearch(current)
        started = time.perf_counter()
        try:
            hits = await provider.search("Hermes STS diagnostic", max_results=2)
        except Exception as exc:
            return {"ok": False, "ms": int((time.perf_counter() - started) * 1000), "error": str(exc), "state": _web_search_payload(current, provider)}
        return {
            "ok": bool(hits),
            "ms": int((time.perf_counter() - started) * 1000),
            "hits": [asdict(hit) for hit in hits],
            "state": _web_search_payload(current, provider),
            "error": "" if hits else "no results",
        }

    @router.get("/api/memories")
    async def list_memories(limit: int = 50, offset: int = 0, q: str = "") -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        try:
            hits = await prov.list_memories(limit=limit, offset=offset, q=q)
        except Exception as exc:
            logger.warning("list_memories failed: %s", exc)
            return {"memories": []}
        return {"memories": [asdict(h) for h in hits]}

    @router.post("/api/memories")
    async def add_memory(payload: MemoryAddRequest) -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        uri = await prov.add_memory(content=payload.content, category=payload.category, tags=payload.tags)
        return {"ok": True, "uri": uri}

    @router.get("/api/memories/activity")
    async def memory_activity(limit: int = 20) -> dict[str, Any]:
        metrics = store.metrics(limit)
        memory_activity = [
            m for m in metrics
            if m["kind"] in ("memory_read", "memory_commit", "memory_extract", "memory_record_turn")
        ][:limit]
        return {"activity": memory_activity}

    @router.post("/api/memories/recall")
    async def recall_memories(payload: MemoryRecallRequest) -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        started = time.perf_counter()
        hits = await prov.recall(payload.query, limit=payload.limit, min_score=payload.min_score)
        ms = int((time.perf_counter() - started) * 1000)
        return {"hits": [asdict(h) for h in hits], "ms": ms}

    @router.get("/api/memories/{uri:path}")
    async def get_memory_endpoint(uri: str) -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        hit = await prov.get_memory(uri)
        if not hit:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"memory": asdict(hit)}

    @router.put("/api/memories/{uri:path}")
    async def update_memory_endpoint(uri: str, payload: MemoryUpdateRequest) -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        await prov.update_memory(uri, content=payload.content, category=payload.category, tags=payload.tags)
        return {"ok": True}

    @router.delete("/api/memories/{uri:path}")
    async def delete_memory_endpoint(uri: str) -> dict[str, Any]:
        if not get_memory or (prov := get_memory()) is None:
            raise HTTPException(status_code=422, detail="memory not configured")
        ok = await prov.delete_memory(uri)
        if not ok:
            raise HTTPException(status_code=404, detail="memory not found")
        return {"ok": True}

    return router


def _settings_payload(settings: Settings, store: ConfigStore) -> dict[str, Any]:
    data = store.settings_dict()
    visible = {
        "server": ["host", "port", "log_level"],
        "llm": [
            "active_llm_profile_id",
            "llm_provider",
            "hermes_base_url",
            "hermes_model",
            "hermes_api_key",
            "hermes_max_tokens",
            "hermes_timeout_seconds",
            "llm_base_url",
            "llm_model",
            "llm_api_key",
            "llm_max_tokens",
            "llm_timeout_seconds",
            "llm_streaming_enabled",
            "hermes_voice_no_think",
            "hermes_history_max_messages",
            "hermes_history_max_chars",
            "hermes_history_idle_reset_seconds",
            "memory_enabled",
            "memory_provider",
            "memory_remember_in_hermes",
            "web_search_enabled",
            "web_search_providers",
        ],
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
            "hermes_agent_max_wait_seconds",
            "hermes_first_filler_delay_seconds",
            "hermes_filler_interval_seconds",
            "hermes_max_fillers",
            "suppress_input_while_speaking",
            "response_audio_chunk_send_delay_ms",
            "tts_segment_min_chars",
            "tts_segment_max_chars",
            "tts_strip_emoji",
            "tts_max_audio_seconds",
        ],
        "advanced": [
            "qwentts_cpp_bin",
            "qwentts_cpp_base_model",
            "qwentts_cpp_customvoice_model",
            "qwentts_cpp_voicedesign_model",
            "qwentts_cpp_codec",
            "qwentts_cpp_extra_args",
            "qwentts_cpp_seed",
            "qwentts_cpp_max_new_frames",
        ],
        "memory": [
            "memory_enabled",
            "memory_provider",
            "memory_remember_in_hermes",
            "memory_injection_budget",
            "memory_recall_limit",
            "memory_recall_min_score",
            "memory_commit_interval_turns",
            "memory_commit_idle_seconds",
            "memory_extract_enabled",
            "memory_extract_max_per_turn",
            "openviking_base_url",
            "openviking_api_key",
            "openviking_account",
            "openviking_user",
            "openviking_target_uri",
            "openviking_timeout_seconds",
            "openviking_commit_timeout_seconds",
            "sqlite_memory_path",
            "web_search_enabled",
            "tavily_api_key",
            "tavily_search_depth",
            "tavily_max_results",
            "tavily_timeout_seconds",
            "tavily_base_url",
            "duckduckgo_timeout_seconds",
            "searxng_base_url",
            "searxng_timeout_seconds",
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


def _llm_context_payload(llm: Any, settings: Settings) -> dict[str, Any]:
    history = getattr(llm, "history", None)
    if not isinstance(history, list):
        history = []
    chars = sum(
        len(str(message.get("content", "")))
        for message in history
        if isinstance(message, dict)
    )
    return {
        "messages": len(history),
        "chars": chars,
        "max_messages": settings.hermes_history_max_messages,
        "max_chars": settings.hermes_history_max_chars,
        "idle_reset_seconds": settings.hermes_history_idle_reset_seconds,
        "last_llm_call_started_at": getattr(llm, "last_llm_call_started_at", None),
        "reset_available": callable(getattr(llm, "reset_history", None)),
    }


def _tools_payload(tools: Any) -> dict[str, Any]:
    if tools is None or not callable(getattr(tools, "snapshot", None)):
        return {"local": [], "client": []}
    return tools.snapshot()


def _conversation_payload(store: Any) -> dict[str, Any]:
    if store is None:
        return {"enabled": False, "active": None, "reset_available": False}
    try:
        active = store.get_active_conversation()
    except Exception as exc:
        return {"enabled": True, "active": None, "reset_available": False, "error": str(exc)}
    return {
        "enabled": True,
        "active": active,
        "reset_available": active is not None,
    }


def _web_search_payload(settings: Settings, provider: Any) -> dict[str, Any]:
    configured = [item.strip() for item in settings.web_search_providers.split(",") if item.strip()]
    base = {
        "enabled": settings.web_search_enabled,
        "configured_providers": configured,
        "provider": provider.description() if provider is not None and callable(getattr(provider, "description", None)) else "noop",
        "providers": [],
        "recent_success": None,
        "cooldowns": {},
        "last_error": None,
    }
    if provider is not None and callable(getattr(provider, "state", None)):
        base.update(provider.state())
    return base


def _diagnostics_payload(
    settings: Settings,
    metrics: list[dict[str, Any]],
    *,
    memory: Any = None,
    tools: Any = None,
    web_search: Any = None,
) -> dict[str, Any]:
    recent_turns = [item for item in metrics if item.get("kind") == "turn"]
    latest_turn = recent_turns[0]["value"] if recent_turns else {}
    local_tools = tools.local_tool_names() if tools is not None and callable(getattr(tools, "local_tool_names", None)) else []
    client_tools = tools.client_tool_names() if tools is not None and callable(getattr(tools, "client_tool_names", None)) else []
    memory_stats = memory.stats() if memory is not None and callable(getattr(memory, "stats", None)) else {}
    web_state = _web_search_payload(settings, web_search)
    return {
        "llm": _diag_item("ok", f"{settings.llm_provider} · {settings.hermes_model if settings.llm_provider == 'hermes_agent' else settings.llm_model}"),
        "stt": _diag_item("ok", settings.stt_provider),
        "tts": _diag_item("ok", f"{settings.tts_provider} · {settings.qwentts_cpp_backend if settings.tts_provider == 'qwen3tts' else settings.sherpa_kokoro_voice}"),
        "tools": _diag_item("ok" if local_tools or client_tools else "warn", f"{len(local_tools)} 系统 / {len(client_tools)} 客户端"),
        "memory": _diag_item("ok" if settings.memory_enabled else "warn", f"{settings.memory_provider} · {memory_stats.get('count', 0)} 条" if settings.memory_enabled else "未开启"),
        "web_search": _diag_item("ok" if settings.web_search_enabled and web_state.get("provider") != "noop" else "warn", web_state.get("provider") or "未开启"),
        "recent": _recent_diag(latest_turn),
    }


def _diag_item(status: str, message: str, last_error: str = "") -> dict[str, str]:
    return {"status": status, "message": message, "last_error": last_error}


def _recent_diag(latest_turn: dict[str, Any]) -> dict[str, Any]:
    if not latest_turn:
        return {"status": "warn", "message": "暂无语音回合数据", "last_error": ""}
    status = str(latest_turn.get("status") or "unknown")
    total_ms = int(latest_turn.get("total_ms") or 0)
    first_audio_ms = int(latest_turn.get("first_audio_ms") or 0)
    if status != "completed":
        return {"status": "warn", "message": f"最近回合 {status}", "last_error": str(latest_turn.get("reason") or "")}
    if first_audio_ms > 3500 or total_ms > 12000:
        return {"status": "warn", "message": f"最近首声 {first_audio_ms}ms / 总耗时 {total_ms}ms", "last_error": ""}
    return {"status": "ok", "message": f"最近首声 {first_audio_ms or '-'}ms", "last_error": ""}


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


def _restart_if_required(changed: dict[str, Any], reason: str) -> bool:
    if not _requires_rebuild(changed):
        return False
    return _schedule_service_restart(reason)


def _schedule_service_restart(reason: str) -> bool:
    if os.getenv("HERMES_STS_DISABLE_SELF_RESTART") == "1":
        logger.info("Self restart disabled; restart required reason=%s", reason)
        return False
    command = f"sleep 0.8; systemctl --user restart {shlex.quote(SERVICE_NAME)}"
    try:
        subprocess.Popen(
            ["sh", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        logger.exception("Failed to schedule service restart reason=%s", reason)
        return False
    logger.info("Scheduled service restart service=%s reason=%s", SERVICE_NAME, reason)
    return True


def _requires_rebuild(changed: dict[str, Any]) -> bool:
    keys = set(changed)
    rebuild_keys = {
        "stt_provider",
        "tts_provider",
        "llm_provider",
        "active_llm_profile_id",
        "qwentts_cpp_bin",
        "sherpa_kokoro_model",
        "sherpa_kokoro_voices",
        "sherpa_kokoro_tokens",
        "sherpa_kokoro_lexicon",
        "sherpa_kokoro_data_dir",
        "sherpa_sensevoice_model",
        "sherpa_sensevoice_tokens",
        "memory_enabled",
        "memory_provider",
        "web_search_enabled",
        "web_search_providers",
        "tavily_api_key",
        "searxng_base_url",
        "openviking_base_url",
        "openviking_api_key",
        "openviking_account",
        "openviking_user",
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
    max_new = values.get("qwentts_cpp_max_new_frames")
    if max_new is not None:
        try:
            max_new_int = int(max_new)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="qwen max-new must be an integer") from exc
        if max_new_int < 0:
            raise HTTPException(status_code=422, detail="qwen max-new must be >= 0")
    provider = values.get("tts_provider")
    if provider is not None and provider not in {"qwen3tts", "sherpa_kokoro", "tone", "sapi", "sherpa_onnx"}:
        raise HTTPException(status_code=422, detail=f"unsupported tts provider: {provider}")
    llm_provider = values.get("llm_provider")
    if llm_provider is not None and llm_provider not in {"hermes_agent", "openai_compatible"}:
        raise HTTPException(status_code=422, detail=f"unsupported llm provider: {llm_provider}")
    memory_provider = values.get("memory_provider")
    if memory_provider is not None and memory_provider not in {"sqlite", "openviking", "noop"}:
        raise HTTPException(status_code=422, detail=f"unsupported memory provider: {memory_provider}")
    if memory_provider == "openviking":
        api_key = values.get("openviking_api_key")
        if api_key is not None and not api_key.strip():
            raise HTTPException(status_code=422, detail="openviking_api_key is required when memory_provider=openviking")
    search_depth = values.get("tavily_search_depth")
    if search_depth is not None and search_depth not in {"ultra-fast", "fast", "basic"}:
        raise HTTPException(status_code=422, detail=f"unsupported tavily search depth: {search_depth}")
    tavily_timeout = values.get("tavily_timeout_seconds")
    if tavily_timeout is not None:
        try:
            tv = float(tavily_timeout)
            if tv > 3.0:
                raise HTTPException(status_code=422, detail="tavily_timeout_seconds must not exceed 3.0")
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="tavily_timeout_seconds must be a number")
    providers = values.get("web_search_providers")
    if providers is not None:
        allowed = {"tavily", "duckduckgo", "ddg", "searxng"}
        unsupported = sorted({item.strip().lower() for item in str(providers).split(",") if item.strip()} - allowed)
        if unsupported:
            raise HTTPException(status_code=422, detail=f"unsupported web search provider: {', '.join(unsupported)}")
    for key in ("duckduckgo_timeout_seconds", "searxng_timeout_seconds"):
        if key in values:
            try:
                if float(values[key]) <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail=f"{key} must be a positive number")


def _safe_profile_id(raw: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", raw.strip())[:64].strip("_")
    return value or f"llm_{uuid.uuid4().hex[:12]}"


def _validate_llm_profile(profile: dict[str, Any]) -> None:
    if profile.get("provider") not in {"hermes_agent", "openai_compatible"}:
        raise HTTPException(status_code=422, detail=f"unsupported llm provider: {profile.get('provider')}")
    if not str(profile.get("name") or "").strip():
        raise HTTPException(status_code=422, detail="profile name is required")
    if not str(profile.get("base_url") or "").strip():
        raise HTTPException(status_code=422, detail="base_url is required")
    if not str(profile.get("model") or "").strip():
        raise HTTPException(status_code=422, detail="model is required")
    try:
        if int(profile.get("max_tokens", 0)) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="max_tokens must be a positive integer")
    for key in ("timeout_seconds", "max_wait_seconds"):
        try:
            if float(profile.get(key, 0)) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"{key} must be a positive number")


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
                "你是个人语音助手的音色设计师，目标是生成可试听、可启用、可收藏的 Qwen3TTS 声线方案。"
                "你需要综合：当前人格、实际使用场景、用户偏好、当前声线状态。"
                "优先让声线适合长期对话、短句快答、等待提示和正式回答保持一致。"
                "只返回 JSON，不要 Markdown。字段必须包含："
                "name, persona_prompt, voice_mode, design_prompt, seed, tags, preview_text, notes, rationale, use_case, save_note。"
                "voice_mode 只能是 default 或 design。"
                "当用户需要明确气质、角色感、播报感、甜度、冷感、性别、年龄、口音、节奏时，选择 design。"
                "当用户只是在默认音色中探索稳定随机声线，或需求极简时，选择 default 并给 seed。"
                "design_prompt 必须是英文短语，适合 Qwen3TTS VoiceDesign，包含 gender/age/pitch/tone/accent/pace/energy/texture 等要素，不要写模型名。"
                "seed 是 1 到 2147483647 的整数，用于 Base 默认音色微调。"
                "persona_prompt 用中文，适合语音助手系统提示词，可以为空；不要夸张，不要二次元套话。"
                "tags 是 2 到 4 个中文短标签，例如：沉稳、清晰、冷感、亲和、播报、低频、甜感、快答。"
                "rationale 用一句中文说明为什么这样设计；save_note 用一句短备注，方便用户以后识别这条收藏声线。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"偏好/需求：{brief}\n"
                f"使用场景：{payload.scenario[:120] or 'persona'}\n"
                f"当前人格参考：{payload.persona_hint[:1200]}\n"
                f"当前声线：{payload.current_voice[:400]}"
            ),
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
        "rationale": str(parsed.get("rationale") or "").strip(),
        "use_case": str(parsed.get("use_case") or payload.scenario or "").strip(),
        "save_note": str(parsed.get("save_note") or parsed.get("notes") or "").strip()[:240],
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
    base_voice = TtsVoice.from_settings(settings)
    if mode == "preset":
        return replace(
            base_voice,
            speaker=payload.speaker or settings.qwentts_cpp_voice_preset,
            instruct="",
            ref_wav="",
            ref_text="",
            ref_spk="",
            ref_rvq="",
        )
    if mode == "design":
        return replace(
            base_voice,
            speaker="",
            instruct=payload.design_prompt or settings.qwentts_cpp_voice_design,
            ref_wav="",
            ref_text="",
            ref_spk="",
            ref_rvq="",
        )
    if mode == "clone":
        voice_id = payload.clone_voice_id or settings.qwentts_cpp_clone_voice_id
        voice = store.voice_profile(voice_id)
        if voice:
            return replace(
                base_voice,
                speaker="",
                instruct="",
                ref_wav=voice.get("ref_wav", ""),
                ref_text=voice.get("ref_text", ""),
                ref_spk=voice.get("ref_spk", ""),
                ref_rvq=voice.get("ref_rvq", ""),
            )
    return base_voice


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
