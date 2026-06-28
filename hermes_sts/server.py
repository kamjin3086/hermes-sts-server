from __future__ import annotations

import logging
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from hermes_sts.admin import create_admin_router
from hermes_sts.config import settings
from hermes_sts.config_store import ConfigStore
from hermes_sts.conversation_store import ConversationStore
from hermes_sts.llm import build_llm
from hermes_sts.realtime import RealtimeSession
from hermes_sts.singleton import acquire_singleton_lock
from hermes_sts.stt import build_stt
from hermes_sts.tools import ToolRegistry
from hermes_sts.tts import build_tts
from hermes_sts.memory import build_memory
from hermes_sts.websearch import build_websearch

logger = logging.getLogger(__name__)


def _build_components(app: FastAPI) -> None:
    app.state.stt = build_stt(settings)
    app.state.tts = build_tts(settings)
    app.state.llm = build_llm(settings)
    app.state.memory = build_memory(settings, app.state.llm)
    app.state.web_search = build_websearch(settings)
    app.state.tools = ToolRegistry()
    if not hasattr(app.state, "turn_gate"):
        app.state.turn_gate = asyncio.Lock()


async def _wire_conversation_store(app: FastAPI) -> None:
    """No-op when ``settings.sts_conversations_enabled`` is False."""
    if not settings.sts_conversations_enabled:
        return

    store = ConversationStore(db_path=settings.sts_conversations_db_path)
    app.state.conversation_store = store
    app.state.llm.conversation_store = store

    archived = await asyncio.to_thread(
        store.maybe_archive_on_idle, settings.hermes_history_idle_reset_seconds
    )
    active = await asyncio.to_thread(store.get_active_conversation)
    if active is not None:
        await asyncio.to_thread(
            store.reload_history_into,
            active["id"],
            app.state.llm,
            settings.sts_conversations_reload_max_messages,
        )
        app.state.llm.conversation_id = active["id"]
    else:
        app.state.llm.conversation_id = None

    # CRITICAL: prevent idle fire immediately after restart.
    app.state.llm.last_llm_call_started_at = time.monotonic()

    logger.info(
        "Conversation store wired: archived_on_start=%s active=%s",
        archived,
        app.state.llm.conversation_id,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _wire_conversation_store(app)
    yield
    store = getattr(app.state, "conversation_store", None)
    if store is not None:
        store.close()


def create_app() -> FastAPI:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    acquire_singleton_lock(settings.log_dir / "sts-server.lock")
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(title="Hermes STS Server", version="0.1.0", lifespan=lifespan)
    app.state.config_store = ConfigStore.default()
    _build_components(app)
    logger.info(
        (
            "STS providers ready sample_rate=%d vad=%s stt=%s tts=%s llm=%s "
            "response_chunk_ms=%d"
        ),
        settings.sample_rate,
        settings.vad_provider,
        settings.stt_provider,
        settings.tts_provider,
        settings.llm_provider,
        settings.response_audio_chunk_ms,
    )

    def rebuild_components() -> None:
        _build_components(app)
        logger.info(
            "STS components rebuilt vad=%s stt=%s tts=%s llm=%s memory=%s web_search=%s voice=%s",
            settings.vad_provider,
            settings.stt_provider,
            settings.tts_provider,
            settings.llm_provider,
            settings.memory_provider,
            settings.web_search_enabled,
            settings.sherpa_kokoro_voice,
        )

    app.include_router(create_admin_router(settings, rebuild_components, lambda: app.state.llm, lambda: app.state.memory))

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": "hermes-sts-server",
            "sample_rate": settings.sample_rate,
            "vad_provider": settings.vad_provider,
            "stt_provider": settings.stt_provider,
            "tts_provider": settings.tts_provider,
            "llm_provider": settings.llm_provider,
            "hermes_base_url": settings.hermes_base_url,
            "hermes_model": settings.hermes_model,
        }

    @app.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket) -> None:
        session = RealtimeSession(
            websocket=websocket,
            settings=settings,
            stt=websocket.app.state.stt,
            tts=websocket.app.state.tts,
            llm=websocket.app.state.llm,
            tools=websocket.app.state.tools,
            turn_gate=websocket.app.state.turn_gate,
            memory=websocket.app.state.memory,
            web_search=websocket.app.state.web_search,
        )
        await session.run()

    return app


app = create_app()
