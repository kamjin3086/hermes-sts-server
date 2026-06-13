from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from hermes_sts.audio import Utterance, chunk_pcm16
from hermes_sts.config import Settings
from hermes_sts.hermes import HermesClient
from hermes_sts.stt import SttProvider
from hermes_sts.tts import TtsProvider
from hermes_sts.vad import EnergyVad

logger = logging.getLogger(__name__)


@dataclass
class RealtimeSession:
    websocket: WebSocket
    settings: Settings
    stt: SttProvider
    tts: TtsProvider
    hermes: HermesClient
    instructions: str = ""
    vad: EnergyVad = field(init=False)
    processing: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.vad = EnergyVad(self.settings)

    async def run(self) -> None:
        await self.websocket.accept()
        await self._send(
            {
                "type": "session.created",
                "event_id": self._event_id(),
                "session": {"id": f"sess_{uuid.uuid4().hex}", "object": "realtime.session"},
            }
        )
        try:
            while True:
                message = await self.websocket.receive_text()
                await self._handle_message(json.loads(message))
        except WebSocketDisconnect:
            logger.info("Realtime client disconnected")
        finally:
            if self.processing and not self.processing.done():
                self.processing.cancel()

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "session.update":
            session = msg.get("session") or {}
            self.instructions = str(session.get("instructions") or "")
            await self._send(
                {
                    "type": "session.updated",
                    "event_id": self._event_id(),
                    "session": {"id": f"sess_{uuid.uuid4().hex}", "object": "realtime.session"},
                }
            )
            return

        if msg_type == "input_audio_buffer.append":
            raw = base64.b64decode(str(msg.get("audio") or ""))
            event, utterance = self.vad.accept(raw)
            if event == "speech_started":
                await self._send({"type": "input_audio_buffer.speech_started", "event_id": self._event_id()})
            if event == "speech_stopped":
                await self._send({"type": "input_audio_buffer.speech_stopped", "event_id": self._event_id()})
            if utterance is not None:
                if self.processing and not self.processing.done():
                    self.processing.cancel()
                self.processing = asyncio.create_task(self._process_turn(utterance.pcm16))
            return

        if msg_type == "response.create":
            # The server normally auto-responds after VAD commit. Acknowledge an
            # explicit create only when no turn is active so the client worker
            # does not stall forever.
            if not self.processing or self.processing.done():
                await self._send_response("I'm here.", transcript="I'm here.")
            return

        if msg_type in {"input_audio_buffer.clear", "response.cancel"}:
            self.vad.reset()
            if self.processing and not self.processing.done():
                self.processing.cancel()
            return

        if msg_type == "conversation.item.create":
            logger.debug("Ignoring conversation.item.create in minimal server: %s", msg)
            return

        logger.debug("Ignoring realtime client event: %s", msg_type)

    async def _process_turn(self, pcm16: bytes) -> None:
        item_id = f"item_{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            duration_ms = int(len(pcm16) / 2 / self.settings.sample_rate * 1000)
            transcript = (
                await self.stt.transcribe(Utterance(pcm16=pcm16, duration_ms=duration_ms, rms=0.0))
            ).strip()
            if not self._is_meaningful_transcript(transcript):
                logger.info("Ignoring empty/non-speech transcript: %r", transcript)
                return
            await self._send(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "event_id": self._event_id(),
                    "item_id": item_id,
                    "content_index": 0,
                    "transcript": transcript,
                }
            )
            logger.info("STT completed in %.0f ms: %s", (time.perf_counter() - started) * 1000, transcript)

            already_responded = await self._respond_with_agent_wait(transcript)
            if not already_responded:
                answer = await self.hermes.ask(transcript, instructions=self.instructions)
                await self._send_response(answer, transcript=answer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Turn processing failed")
            await self._send_error(str(exc))

    @staticmethod
    def _is_meaningful_transcript(transcript: str) -> bool:
        text = transcript.strip()
        if not text:
            return False
        return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text))

    async def _respond_with_agent_wait(self, transcript: str) -> bool:
        hermes_task = asyncio.create_task(self.hermes.ask(transcript, instructions=self.instructions))
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        filler_texts = self._filler_texts_for(transcript)
        filler_count = 0
        started = time.perf_counter()
        first_filler_pcm_task = None
        if filler_texts and self.settings.hermes_max_fillers > 0:
            first_filler_pcm_task = asyncio.create_task(self.tts.synthesize(filler_texts[0]))

        try:
            answer = await asyncio.wait_for(
                asyncio.shield(hermes_task),
                timeout=max(0.0, self.settings.hermes_first_filler_delay_seconds),
            )
            if first_filler_pcm_task and not first_filler_pcm_task.done():
                first_filler_pcm_task.cancel()
            await self._send_response(answer, transcript=answer)
            return True
        except asyncio.TimeoutError:
            pass

        await self._send_response_created(response_id)
        if first_filler_pcm_task is not None:
            await self._send_pcm_segment(await first_filler_pcm_task, response_id=response_id, item_id=item_id)
            filler_count = 1

        answer: str | None = None
        while not hermes_task.done():
            elapsed = time.perf_counter() - started
            remaining = self.settings.hermes_agent_max_wait_seconds - elapsed
            if remaining <= 0:
                hermes_task.cancel()
                answer = self._fallback_text_for(transcript)
                break

            timeout = min(self.settings.hermes_filler_interval_seconds, remaining)
            try:
                answer = await asyncio.wait_for(asyncio.shield(hermes_task), timeout=timeout)
                break
            except asyncio.TimeoutError:
                if filler_count < self.settings.hermes_max_fillers and filler_count < len(filler_texts):
                    await self._send_audio_segment(
                        filler_texts[filler_count],
                        response_id=response_id,
                        item_id=item_id,
                    )
                    filler_count += 1

        if answer is None:
            answer = await hermes_task

        await self._send_audio_segment(answer, response_id=response_id, item_id=item_id)
        await self._send_response_done(response_id=response_id, item_id=item_id, transcript=answer)
        return True

    def _filler_texts_for(self, transcript: str) -> list[str]:
        if any("\u4e00" <= char <= "\u9fff" for char in transcript):
            texts = [
                "我想一下，稍等。",
                "这个我需要多确认一点。",
                "还在处理，马上好。",
                "我正在查，先别急。",
                "收到，我再核对一下。",
            ]
            random.shuffle(texts)
            return texts
        return [
            "Let me think for a moment.",
            "I'm checking that now.",
            "I'm still working on it.",
        ]

    def _fallback_text_for(self, transcript: str) -> str:
        configured = [
            item.strip()
            for item in self.settings.hermes_fallback_texts.split("|")
            if item.strip()
        ]
        if configured:
            return random.choice(configured)
        if any("\u4e00" <= char <= "\u9fff" for char in transcript):
            return random.choice(
                [
                    "我这边还没有等到 Hermes 的结果，不过语音链路是正常的。你可以再问一次，我会继续接。",
                    "Hermes 这次响应有点慢，我先保留现场。你再说一遍或者稍等一下都可以。",
                    "我还在等后端返回，当前本地语音连接没问题。我们可以继续对话。",
                ]
            )
        return self.settings.hermes_fallback_text

    async def _send_response(self, text: str, *, transcript: str) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        await self._send_response_created(response_id)
        await self._send_audio_segment(text, response_id=response_id, item_id=item_id)
        await self._send_response_done(response_id=response_id, item_id=item_id, transcript=transcript)

    async def _send_response_created(self, response_id: str) -> None:
        await self._send(
            {
                "type": "response.created",
                "event_id": self._event_id(),
                "response": {"id": response_id, "object": "realtime.response", "status": "in_progress"},
            }
        )

    async def _send_audio_segment(self, text: str, *, response_id: str, item_id: str) -> None:
        pcm16 = await self.tts.synthesize(text)
        await self._send_pcm_segment(pcm16, response_id=response_id, item_id=item_id)

    async def _send_pcm_segment(self, pcm16: bytes, *, response_id: str, item_id: str) -> None:
        for part in chunk_pcm16(pcm16, chunk_ms=80, sample_rate=self.settings.sample_rate):
            await self._send(
                {
                    "type": "response.output_audio.delta",
                    "event_id": self._event_id(),
                    "response_id": response_id,
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": base64.b64encode(part).decode("ascii"),
                }
            )
            await asyncio.sleep(0.01)

    async def _send_response_done(self, *, response_id: str, item_id: str, transcript: str) -> None:
        await self._send(
            {
                "type": "response.output_audio.done",
                "event_id": self._event_id(),
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
            }
        )
        await self._send(
            {
                "type": "response.output_audio_transcript.done",
                "event_id": self._event_id(),
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "transcript": transcript,
            }
        )
        await self._send(
            {
                "type": "response.done",
                "event_id": self._event_id(),
                "response": {
                    "id": response_id,
                    "object": "realtime.response",
                    "status": "completed",
                    "usage": {
                        "input_token_details": {"audio_tokens": 0, "text_tokens": 0, "image_tokens": 0},
                        "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
                    },
                },
            }
        )

    async def _send_error(self, message: str) -> None:
        await self._send(
            {
                "type": "error",
                "event_id": self._event_id(),
                "error": {"type": "server_error", "code": "server_error", "message": message},
            }
        )

    async def _send(self, event: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(event, ensure_ascii=False))

    @staticmethod
    def _event_id() -> str:
        return f"evt_{uuid.uuid4().hex}"
