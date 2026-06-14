from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from hermes_sts.admin import create_admin_router
from hermes_sts.config import settings
from hermes_sts.llm import build_llm
from hermes_sts.realtime import RealtimeSession
from hermes_sts.stt import build_stt
from hermes_sts.tools import ToolRegistry
from hermes_sts.tts import build_tts

logger = logging.getLogger(__name__)


def _build_components(app: FastAPI) -> None:
    app.state.stt = build_stt(settings)
    app.state.tts = build_tts(settings)
    app.state.llm = build_llm(settings)
    app.state.tools = ToolRegistry()


def create_app() -> FastAPI:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(title="Hermes STS Server", version="0.1.0")
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
            "STS components rebuilt vad=%s stt=%s tts=%s llm=%s voice=%s",
            settings.vad_provider,
            settings.stt_provider,
            settings.tts_provider,
            settings.llm_provider,
            settings.sherpa_kokoro_voice,
        )

    app.include_router(create_admin_router(settings, rebuild_components))

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
        )
        await session.run()

    return app


app = create_app()
