from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket

from hermes_sts.config import settings
from hermes_sts.hermes import HermesClient
from hermes_sts.realtime import RealtimeSession
from hermes_sts.stt import build_stt
from hermes_sts.tts import build_tts


def create_app() -> FastAPI:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = FastAPI(title="Hermes STS Server", version="0.1.0")
    stt = build_stt(settings)
    tts = build_tts(settings)

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "service": "hermes-sts-server",
            "sample_rate": settings.sample_rate,
            "stt_provider": settings.stt_provider,
            "tts_provider": settings.tts_provider,
            "hermes_base_url": settings.hermes_base_url,
            "hermes_model": settings.hermes_model,
        }

    @app.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket) -> None:
        session = RealtimeSession(
            websocket=websocket,
            settings=settings,
            stt=stt,
            tts=tts,
            hermes=HermesClient(settings),
        )
        await session.run()

    return app


app = create_app()
