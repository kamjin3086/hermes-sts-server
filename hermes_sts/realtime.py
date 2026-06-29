from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import random
import re
import string
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from hermes_sts.audio import Utterance, chunk_pcm16
from hermes_sts.config import Settings
from hermes_sts.config_store import ConfigStore
from hermes_sts.llm import LLMProvider, LLMResponse, LLMToolCallDetected, Message, ToolCall
from hermes_sts.persona import build_persona_instructions
from hermes_sts.stt import SttProvider
from hermes_sts.tools import ToolExecution, ToolRegistry
from hermes_sts.tts import TtsProvider, TtsVoice
from hermes_sts.vad import VadProvider, build_vad

logger = logging.getLogger(__name__)

SessionState = Literal["idle", "listening", "processing", "speaking", "cancelled"]


@dataclass
class TurnMetrics:
    turn_id: str
    started_at: float
    utterance_ms: int = 0
    stt_ms: float = 0.0
    llm_ms: float = 0.0
    first_tts_ms: float = 0.0
    first_audio_ms: float = 0.0
    tts_segments: int = 0
    audio_chunks: int = 0


@dataclass
class RealtimeSession:
    websocket: WebSocket
    settings: Settings
    stt: SttProvider
    tts: TtsProvider
    llm: LLMProvider
    tools: ToolRegistry
    turn_gate: asyncio.Lock | None = None
    instructions: str = ""
    state: SessionState = "idle"
    vad: VadProvider = field(init=False)
    processing: asyncio.Task[None] | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_response_id: str | None = None
    active_item_id: str | None = None
    active_metrics: TurnMetrics | None = None
    pending_text_inputs: list[str] = field(default_factory=list)
    pending_tool_results: list[Message] = field(default_factory=list)
    pending_tool_context: list[Message] | None = None
    next_response_instructions: str = ""
    session_voice: TtsVoice | None = None
    next_response_voice: TtsVoice | None = None
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex}")
    memory: Any = None
    web_search: Any = None

    def __post_init__(self) -> None:
        self.vad = build_vad(self.settings)
        self.tools.set_client_tools(None)
        from hermes_sts.tools import register_default_local_tools

        register_default_local_tools(self.tools, self.settings, web_search_provider=self.web_search)

    async def run(self) -> None:
        await self.websocket.accept()
        logger.info("Realtime session connected session_id=%s", self.session_id)
        await self._send(
            {
                "type": "session.created",
                "event_id": self._event_id(),
                "session": {"id": self.session_id, "object": "realtime.session"},
            }
        )
        try:
            while True:
                message = await self.websocket.receive_text()
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError as exc:
                    await self._send_error("invalid_json", str(exc))
                    continue
                await self._handle_message(payload)
        except WebSocketDisconnect:
            logger.info("Realtime client disconnected session_id=%s", self.session_id)
        finally:
            await self._cancel_processing(send_done=False)
            if self.settings.memory_enabled:
                memory = getattr(self, "memory", None)
                if memory is not None:
                    try:
                        await memory.final_commit(self.session_id)
                    except Exception:
                        logger.warning("Memory final_commit failed on disconnect", exc_info=True)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "session.update":
            session = msg.get("session") or {}
            self._apply_session_config(session)
            await self._send(
                {
                    "type": "session.updated",
                    "event_id": self._event_id(),
                    "session": {"id": self.session_id, "object": "realtime.session"},
                }
            )
            return

        if msg_type == "input_audio_buffer.append":
            if self.state == "speaking" and self.settings.suppress_input_while_speaking:
                logger.debug(
                    "Dropping input audio to avoid self-listening session_id=%s state=%s",
                    self.session_id,
                    self.state,
                )
                return
            raw = await self._decode_audio_append(msg)
            if raw is None:
                return
            event, utterance = self.vad.accept(raw)
            if event == "speech_started":
                self.state = "listening"
                if self.processing and not self.processing.done():
                    await self._cancel_processing(reason="barge_in")
                await self._send({"type": "input_audio_buffer.speech_started", "event_id": self._event_id()})
            if event == "speech_stopped":
                await self._send({"type": "input_audio_buffer.speech_stopped", "event_id": self._event_id()})
            if utterance is not None:
                await self._cancel_processing(send_done=False)
                self.state = "processing"
                metrics = TurnMetrics(
                    turn_id=f"turn_{uuid.uuid4().hex}",
                    started_at=time.perf_counter(),
                    utterance_ms=utterance.duration_ms,
                )
                logger.info(
                    "VAD committed session_id=%s turn_id=%s utterance_ms=%d",
                    self.session_id,
                    metrics.turn_id,
                    metrics.utterance_ms,
                )
                self.processing = self._create_processing_task(
                    lambda: self._process_turn(utterance.pcm16, metrics),
                    metrics=metrics,
                )
            return

        if msg_type == "response.create":
            response_config = msg.get("response") if isinstance(msg.get("response"), dict) else {}
            if response_config:
                self._apply_response_config(response_config)
            if not self.processing or self.processing.done():
                transcript = self._pop_pending_text()
                if transcript:
                    self.state = "processing"
                    metrics = TurnMetrics(
                        turn_id=f"turn_{uuid.uuid4().hex}",
                        started_at=time.perf_counter(),
                    )
                    turn_instructions = self._consume_response_instructions()
                    turn_voice = self._consume_response_voice()
                    self.processing = self._create_processing_task(
                        lambda: self._process_text_turn(
                            transcript,
                            metrics,
                            instructions=turn_instructions,
                            voice=turn_voice,
                        ),
                        metrics=metrics,
                    )
                elif self.pending_tool_context and self.pending_tool_results:
                    self.state = "processing"
                    metrics = TurnMetrics(
                        turn_id=f"turn_{uuid.uuid4().hex}",
                        started_at=time.perf_counter(),
                    )
                    turn_instructions = self._consume_response_instructions()
                    turn_voice = self._consume_response_voice()
                    self.processing = self._create_processing_task(
                        lambda: self._process_tool_result_turn(
                            metrics,
                            instructions=turn_instructions,
                            voice=turn_voice,
                        ),
                        metrics=metrics,
                    )
                else:
                    self.processing = self._create_processing_task(
                        lambda: self._send_response("I'm here.", transcript="I'm here."),
                    )
            return

        if msg_type == "response.cancel":
            self.vad.reset()
            await self._cancel_processing(reason="client_cancel")
            return

        if msg_type == "input_audio_buffer.clear":
            self.vad.reset()
            return

        if msg_type == "conversation.item.create":
            self._handle_conversation_item(msg.get("item") or {})
            return

        logger.debug("Ignoring realtime client event: %s", msg_type)

    def _create_processing_task(
        self,
        factory: Callable[[], Awaitable[None]],
        *,
        metrics: TurnMetrics | None = None,
    ) -> asyncio.Task[None]:
        return asyncio.create_task(self._run_serialized_turn(factory, metrics=metrics))

    async def _run_serialized_turn(
        self,
        factory: Callable[[], Awaitable[None]],
        *,
        metrics: TurnMetrics | None = None,
    ) -> None:
        if self.turn_gate is None:
            await factory()
            return

        waited_started = time.perf_counter()
        if self.turn_gate.locked():
            logger.info("Waiting for STS serial turn gate session_id=%s", self.session_id)
        async with self.turn_gate:
            waited_ms = int((time.perf_counter() - waited_started) * 1000)
            if waited_ms > 250:
                logger.info(
                    "Acquired STS serial turn gate session_id=%s turn_id=%s waited_ms=%s",
                    self.session_id,
                    metrics.turn_id if metrics else "",
                    waited_ms,
                )
            await factory()

    def _apply_session_config(self, session: dict[str, Any]) -> None:
        if "instructions" in session:
            self.instructions = str(session.get("instructions") or "")
        if "voice" in session:
            self.session_voice = TtsVoice.from_realtime(session.get("voice"))
        if isinstance(session.get("tools"), list):
            self.tools.set_client_tools(session.get("tools"))
        logger.info(
            "DBG session.update session_id=%s instructions_chars=%d persona_source=%s voice_source=%s"
            " ws_voice=%s settings_persona_preset=%s settings_persona_custom=%.60s settings_voice_mode=%s"
            " client_tools=%d local_tools=%d",
            self.session_id,
            len(self.instructions),
            self.settings.sts_persona_source,
            self.settings.tts_voice_source,
            self._voice_label(self.session_voice),
            self.settings.sts_persona_preset,
            self.settings.sts_persona_custom or "(empty)",
            self.settings.qwentts_cpp_voice_mode,
            len(self.tools.client_tool_names()),
            len(self.tools.local_tool_names()),
        )

    def _apply_response_config(self, response_config: dict[str, Any]) -> None:
        instructions = response_config.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            self.next_response_instructions = instructions.strip()
        if "voice" in response_config:
            self.next_response_voice = TtsVoice.from_realtime(response_config.get("voice"))
        if isinstance(response_config.get("tools"), list):
            self.tools.set_client_tools(response_config.get("tools"))
        logger.debug(
            "Response config updated session_id=%s response_instructions_chars=%d client_tools=%d",
            self.session_id,
            len(self.next_response_instructions),
            len(self.tools.client_tool_names()),
        )

    @staticmethod
    def _merge_instructions(current: str, extra: str) -> str:
        current = current.strip()
        extra = extra.strip()
        if not current:
            return extra
        if not extra or extra in current:
            return current
        return f"{current}\n\nResponse-specific instructions:\n{extra}"

    def _handle_conversation_item(self, item: dict[str, Any]) -> None:
        item_type = item.get("type")
        if item_type == "function_call_output":
            call_id = str(item.get("call_id") or "")
            output = item.get("output")
            if not self.pending_tool_context:
                logger.info(
                    "Ignoring tool result without pending follow-up session_id=%s call_id=%s",
                    self.session_id,
                    call_id,
                )
                return
            if call_id:
                self.pending_tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                    }
                )
                logger.info(
                    "Queued tool result session_id=%s call_id=%s pending_results=%d",
                    self.session_id,
                    call_id,
                    len(self.pending_tool_results),
                )
            return

        text = self._extract_text_from_item(item)
        if text:
            self.pending_text_inputs.append(text)
            logger.info(
                "Queued text input session_id=%s chars=%d pending_text_items=%d",
                self.session_id,
                len(text),
                len(self.pending_text_inputs),
            )
            return

        logger.debug("Ignoring conversation.item.create item: %s", item_type)

    @staticmethod
    def _extract_text_from_item(item: dict[str, Any]) -> str:
        content = item.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"input_text", "text"} and isinstance(part.get("text"), str):
                    parts.append(part["text"].strip())
        if not parts and isinstance(item.get("text"), str):
            parts.append(item["text"].strip())
        return "\n".join(part for part in parts if part).strip()

    def _pop_pending_text(self) -> str:
        if not self.pending_text_inputs:
            return ""
        text = "\n".join(self.pending_text_inputs).strip()
        self.pending_text_inputs.clear()
        logger.debug("Popped pending text session_id=%s chars=%d", self.session_id, len(text))
        return text

    def _consume_response_instructions(self) -> str:
        instructions = self.next_response_instructions
        self.next_response_instructions = ""
        return self._effective_instructions(instructions)

    def _effective_instructions(self, response_instructions: str | None = None) -> str:
        settings_persona = build_persona_instructions(self.settings)
        ws_instructions = self._merge_instructions(self.instructions, response_instructions or "")
        if self.settings.sts_persona_source.strip().lower() == "ws":
            result = ws_instructions or settings_persona
            logger.info(
                "DBG effective_instructions session_id=%s source=ws preset=%s chars=%d",
                self.session_id, self.settings.sts_persona_preset, len(result),
            )
            return result
        result = self._merge_instructions(settings_persona, ws_instructions)
        logger.info(
            "DBG effective_instructions session_id=%s source=settings preset=%s chars=%d",
            self.session_id, self.settings.sts_persona_preset, len(result),
        )
        return result

    def _consume_response_voice(self) -> TtsVoice | None:
        voice = self.next_response_voice
        self.next_response_voice = None
        return voice

    def _effective_tts_voice(self, response_voice: TtsVoice | None = None) -> TtsVoice:
        if self.settings.tts_voice_source.strip().lower() == "ws":
            logger.warning(
                "Ignoring websocket-provided voice because STS now uses settings as the single voice source"
            )
        return TtsVoice.from_settings(self.settings)

    def _turn_tts_voice(self, response_voice: TtsVoice | None = None) -> TtsVoice:
        voice = self._effective_tts_voice(response_voice)
        logger.info(
            "DBG turn_tts_voice session_id=%s voice_mode=%s speaker=%s instruct=%.50s source=%s",
            self.session_id,
            self.settings.qwentts_cpp_voice_mode,
            voice.speaker or "(none)",
            voice.instruct or "(none)",
            self.settings.tts_voice_source,
        )
        return voice

    @staticmethod
    def _voice_label(voice: TtsVoice | None) -> str:
        if not voice or voice.is_empty():
            return ""
        if voice.speaker:
            return f"speaker:{voice.speaker}"
        if voice.ref_spk or voice.ref_rvq:
            return "clone:preencoded"
        if voice.ref_wav:
            return "clone:wav"
        if voice.instruct:
            return "design"
        return "custom"

    async def _inject_memory(self, transcript: str, instructions: str) -> str:
        if not self.settings.memory_enabled:
            return instructions
        if not transcript.strip():
            return instructions
        if self.settings.llm_provider.strip().lower() == "hermes_agent" and not self.settings.memory_remember_in_hermes:
            return instructions
        memory = getattr(self, "memory", None)
        if memory is None:
            return instructions
        started = time.perf_counter()
        try:
            hits = await memory.recall(
                transcript,
                limit=self.settings.memory_recall_limit,
                min_score=self.settings.memory_recall_min_score,
            )
        except Exception:
            logger.warning("Memory recall failed, skipping injection", exc_info=True)
            return instructions
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not hits:
            return instructions
        budget = max(100, self.settings.memory_injection_budget)
        lines: list[str] = []
        for h in hits:
            line = f"- {h.abstract}"
            if len("\n".join(lines + [line])) > budget:
                break
            lines.append(line)
        block = "\n\n参考记忆（不要逐条复述，只用于回答更准确）：\n" + "\n".join(lines)
        combined = instructions + block
        if len(combined) > 2500:
            excess = len(combined) - 2500
            block = block[:-excess]
            if len(block) < 20:
                return instructions
            logger.warning("Memory injection truncated by %d chars to fit 2500 limit", excess)
        try:
            ConfigStore.default().add_metric(
                "memory_read",
                {"query": transcript[:80], "hits": len(hits), "ms": elapsed_ms},
            )
        except Exception:
            pass
        return instructions + block

    async def _record_memory_turn(self, transcript: str, answer: str) -> None:
        try:
            await self.memory.record_turn(transcript, answer, session_id=self.session_id)
            ConfigStore.default().add_metric(
                "memory_record_turn",
                {"session_id": self.session_id[:16], "transcript_chars": len(transcript)},
            )
        except Exception:
            logger.warning("Memory record_turn failed", exc_info=True)

    async def _decode_audio_append(self, msg: dict[str, Any]) -> bytes | None:
        encoded = msg.get("audio")
        if not isinstance(encoded, str) or not encoded:
            await self._send_error("invalid_audio", "input_audio_buffer.append requires non-empty audio")
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            await self._send_error("invalid_audio_base64", str(exc))
            return None
        if not raw:
            await self._send_error("invalid_audio", "audio chunk is empty")
            return None
        if len(raw) % 2:
            await self._send_error("invalid_audio_format", "PCM16 audio must contain an even number of bytes")
            return None
        if len(raw) > self.settings.max_audio_chunk_bytes:
            await self._send_error(
                "audio_chunk_too_large",
                f"audio chunk exceeds {self.settings.max_audio_chunk_bytes} bytes",
            )
            return None
        return raw

    async def _process_turn(self, pcm16: bytes, metrics: TurnMetrics | None = None) -> None:
        item_id = f"item_{uuid.uuid4().hex}"
        started = time.perf_counter()
        try:
            duration_ms = int(len(pcm16) / 2 / self.settings.sample_rate * 1000)
            transcript = (
                await self.stt.transcribe(Utterance(pcm16=pcm16, duration_ms=duration_ms, rms=0.0))
            ).strip()
            if not self._is_meaningful_transcript(transcript):
                logger.info("Ignoring empty/non-speech transcript: %r", transcript)
                self.state = "idle"
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
            stt_ms = (time.perf_counter() - started) * 1000
            if metrics:
                metrics.stt_ms = stt_ms
            logger.info(
                "STT completed session_id=%s turn_id=%s stt_ms=%.0f transcript_chars=%d",
                self.session_id,
                metrics.turn_id if metrics else "-",
                stt_ms,
                len(transcript),
            )
            await self._respond_with_agent_wait(transcript, metrics=metrics)
            self.state = "idle"
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except Exception as exc:
            self.state = "idle"
            logger.exception("Turn processing failed")
            await self._send_error("server_error", str(exc))

    async def _process_text_turn(
        self,
        transcript: str,
        metrics: TurnMetrics | None = None,
        *,
        instructions: str | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        try:
            transcript = transcript.strip()
            if not transcript:
                self.state = "idle"
                return
            await self._respond_with_agent_wait(
                transcript,
                metrics=metrics,
                instructions=instructions,
                voice=voice,
            )
            self.state = "idle"
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except Exception as exc:
            self.state = "idle"
            logger.exception("Text turn processing failed")
            await self._send_error("server_error", str(exc))

    async def _process_tool_result_turn(
        self,
        metrics: TurnMetrics | None = None,
        *,
        instructions: str | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        context = self.pending_tool_context or []
        results = list(self.pending_tool_results)
        self.pending_tool_context = None
        self.pending_tool_results.clear()
        try:
            started = time.perf_counter()
            messages = [*context, *results]
            if messages and messages[0].get("role") == "system":
                messages[0] = {"role": "system", "content": self._tool_system_prompt(instructions=instructions)}
            logger.info(
                "Processing tool result turn session_id=%s context_messages=%d tool_results=%d",
                self.session_id,
                len(context),
                len(results),
            )
            instructions = instructions if instructions is not None else self._effective_instructions()
            final = await self.llm.chat(messages=messages, instructions=instructions)
            if metrics:
                metrics.llm_ms = (time.perf_counter() - started) * 1000
            text = final.text.strip() or "好的，已完成。"
            tool_transcript = next(
                (m.get("content", "") for m in context if m.get("role") == "user"),
                "",
            )
            self._fire_record_turn(tool_transcript, text)
            await self._send_response(text, transcript=text, metrics=metrics, voice=voice)
            self.state = "idle"
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except Exception as exc:
            self.state = "idle"
            logger.exception("Tool result turn processing failed")
            await self._send_error("server_error", str(exc))

    @staticmethod
    def _is_meaningful_transcript(transcript: str) -> bool:
        text = transcript.strip()
        if not text:
            return False
        return bool(re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text))

    async def _respond_with_agent_wait(
        self,
        transcript: str,
        *,
        metrics: TurnMetrics | None = None,
        instructions: str | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        instructions = instructions if instructions is not None else self._effective_instructions()
        voice = self._turn_tts_voice(voice)
        logger.info(
            "DBG respond_with_agent_wait session_id=%s transcript_chars=%d instructions_chars=%d",
            self.session_id, len(transcript), len(instructions),
        )
        if await self._route_direct_client_action(transcript, instructions=instructions, metrics=metrics):
            return
        if self.settings.llm_streaming_enabled and callable(getattr(self.llm, "stream_text", None)):
            try:
                if await self._respond_with_llm_stream(
                    transcript,
                    metrics=metrics,
                    instructions=instructions,
                    voice=voice,
                ):
                    return
            except Exception:
                logger.warning("LLM streaming path failed before speech; falling back to complete turn", exc_info=True)
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        llm_task = asyncio.create_task(
            self._ask_llm_with_tools(
                transcript,
                response_id=response_id,
                item_id=item_id,
                metrics=metrics,
                instructions=instructions,
            )
        )
        filler_texts = self._filler_texts_for(transcript)
        filler_count = 0
        started = time.perf_counter()
        first_filler_pcm_task = None
        if filler_texts and self.settings.hermes_max_fillers > 0:
            first_filler_pcm_task = asyncio.create_task(
                self._synthesize_tts(filler_texts[0], metrics=metrics, voice=voice)
            )

        try:
            try:
                answer = await asyncio.wait_for(
                    asyncio.shield(llm_task),
                    timeout=max(0.0, self.settings.hermes_first_filler_delay_seconds),
                )
                if first_filler_pcm_task and not first_filler_pcm_task.done():
                    first_filler_pcm_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await first_filler_pcm_task
                if answer is None:
                    return
                await self._send_response(answer, transcript=answer, metrics=metrics, voice=voice)
                return
            except asyncio.TimeoutError:
                pass

            await self._send_response_created(response_id, item_id, metrics=metrics)
            if first_filler_pcm_task is not None:
                await self._send_pcm_segment(
                    await first_filler_pcm_task,
                    response_id=response_id,
                    item_id=item_id,
                    metrics=metrics,
                )
                filler_count = 1

            answer: str | None = None
            while not llm_task.done():
                elapsed = time.perf_counter() - started
                remaining = self.settings.hermes_agent_max_wait_seconds - elapsed
                if remaining <= 0:
                    llm_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await llm_task
                    answer = self._fallback_text_for(transcript)
                    break

                timeout = min(self.settings.hermes_filler_interval_seconds, remaining)
                try:
                    answer = await asyncio.wait_for(asyncio.shield(llm_task), timeout=timeout)
                    break
                except asyncio.TimeoutError:
                    if filler_count < self.settings.hermes_max_fillers and filler_count < len(filler_texts):
                        await self._send_audio_segment(
                            filler_texts[filler_count],
                            response_id=response_id,
                            item_id=item_id,
                            metrics=metrics,
                            voice=voice,
                        )
                        filler_count += 1

            if answer is None:
                answer = await llm_task

            if answer is None:
                return
            await self._send_text_segments(
                answer,
                response_id=response_id,
                item_id=item_id,
                metrics=metrics,
                voice=voice,
            )
            await self._send_response_done(response_id=response_id, item_id=item_id, transcript=answer)
        except asyncio.CancelledError:
            logger.info("Cancelling in-flight LLM turn session_id=%s response_id=%s", self.session_id, response_id)
            raise
        finally:
            if not llm_task.done():
                llm_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await llm_task
            if first_filler_pcm_task is not None and not first_filler_pcm_task.done():
                first_filler_pcm_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await first_filler_pcm_task

    async def _route_direct_client_action(
        self,
        transcript: str,
        *,
        instructions: str,
        metrics: TurnMetrics | None = None,
    ) -> bool:
        tool_call = self._direct_client_action_tool_call(transcript)
        if tool_call is None:
            return False
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        execution = await self.tools.execute(tool_call.name, tool_call.arguments)
        if not execution.forwarded:
            return False
        needs_response = execution.needs_response
        pending_context = [
            {"role": "system", "content": self._tool_system_prompt(instructions=instructions)},
            {"role": "user", "content": transcript},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [self._tool_call_message(tool_call)],
            },
        ]
        if needs_response:
            self.pending_tool_context = pending_context
        await self._send_response_created(response_id, item_id, metrics=metrics)
        await self._send_tool_call_event(
            tool_call=tool_call,
            execution=execution,
            response_id=response_id,
            item_id=item_id,
        )
        await self._send_response_done(response_id=response_id, item_id=item_id, transcript="")
        logger.info(
            "Routed direct client action session_id=%s transcript=%r tool=%s needs_response=%s",
            self.session_id,
            transcript[:80],
            tool_call.name,
            needs_response,
        )
        return True

    def _direct_client_action_tool_call(self, transcript: str) -> ToolCall | None:
        text = self._normalize_action_text(transcript)
        if not text or len(text) > 40:
            return None
        client_tools = set(self.tools.client_tool_names())

        def call(name: str, arguments: dict[str, Any]) -> ToolCall | None:
            if name not in client_tools:
                return None
            return ToolCall(id=f"call_{uuid.uuid4().hex}", name=name, arguments=json.dumps(arguments, ensure_ascii=False))

        if "stop_dance" in client_tools and any(token in text for token in ("别跳", "不跳", "停止跳舞", "停下跳舞", "stopdance", "stopdancing")):
            return call("stop_dance", {"dummy": True})
        if "dance" in client_tools and self._is_dance_command(text, recent_dance_context=self._has_recent_dance_context()):
            return call("dance", {"move": "random", "repeat": self._dance_repeat_for(text)})
        if "move_head" in client_tools and self._is_head_command(text):
            return call("move_head", {"direction": self._head_direction_for(text)})
        if "play_emotion" in client_tools and self._is_emotion_command(text):
            return call("play_emotion", {"emotion": self._emotion_for(text)})
        return None

    @staticmethod
    def _normalize_action_text(text: str) -> str:
        table = str.maketrans("", "", string.whitespace + string.punctuation + "，。！？；：、“”‘’（）()【】[]《》<>…")
        return text.strip().lower().translate(table)

    def _has_recent_dance_context(self) -> bool:
        history = getattr(getattr(self, "llm", None), "history", [])
        if not isinstance(history, list):
            return False
        recent = "".join(str(message.get("content", "")) for message in history[-6:] if isinstance(message, dict))
        normalized = self._normalize_action_text(recent)
        return "跳舞" in normalized or "舞蹈" in normalized or "三个舞" in normalized or "dance" in normalized

    @staticmethod
    def _is_dance_command(text: str, *, recent_dance_context: bool = False) -> bool:
        return (
            "跳舞" in text
            or "舞蹈" in text
            or "跳个舞" in text
            or "来个舞" in text
            or text in {"跳一下", "三个舞", "dance"}
            or (recent_dance_context and text in {"开始", "再来一个", "再来", "继续"})
        )

    @staticmethod
    def _dance_repeat_for(text: str) -> int:
        if any(token in text for token in ("三", "3", "three")):
            return 3
        if any(token in text for token in ("两", "二", "2", "two")):
            return 2
        return 1

    @staticmethod
    def _is_head_command(text: str) -> bool:
        return any(token in text for token in ("摇头", "点头", "抬头", "低头", "左看", "右看", "看左", "看右", "回正"))

    @staticmethod
    def _head_direction_for(text: str) -> str:
        if any(token in text for token in ("左看", "看左")):
            return "left"
        if any(token in text for token in ("右看", "看右")):
            return "right"
        if any(token in text for token in ("抬头", "点头")):
            return "up"
        if "低头" in text:
            return "down"
        return "front"

    @staticmethod
    def _is_emotion_command(text: str) -> bool:
        return any(token in text for token in ("开心", "高兴", "难过", "伤心", "惊讶", "害羞", "生气", "动作", "表情"))

    @staticmethod
    def _emotion_for(text: str) -> str:
        if any(token in text for token in ("难过", "伤心")):
            return "sad"
        if "惊讶" in text:
            return "surprised"
        if "害羞" in text:
            return "shy"
        if "生气" in text:
            return "angry"
        return "happy"

    async def _respond_with_llm_stream(
        self,
        transcript: str,
        *,
        metrics: TurnMetrics | None = None,
        instructions: str | None = None,
        voice: TtsVoice | None = None,
    ) -> bool:
        if instructions is None:
            instructions = self._effective_instructions()
        instructions = await self._inject_memory(transcript, instructions)
        await self.llm.ensure_active_conversation()
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        started = time.perf_counter()
        created = False
        sent_any = False
        pending = ""
        answer_parts: list[str] = []
        try:
            async for chunk in self.llm.stream_text(
                transcript,
                instructions=instructions,
                tools=self.tools.openai_tools(),
            ):
                answer_parts.append(chunk)
                pending += chunk
                ready, pending = self._pop_stream_tts_segments(pending)
                for segment in ready:
                    if not created:
                        await self._send_response_created(response_id, item_id, metrics=metrics)
                        created = True
                    await self._send_tts_segment(
                        segment,
                        response_id=response_id,
                        item_id=item_id,
                        metrics=metrics,
                        voice=voice,
                    )
                    sent_any = True
        except LLMToolCallDetected:
            if sent_any:
                logger.warning("LLM stream switched to tool calls after speech started; ending partial spoken response")
            else:
                return False
        except Exception:
            if not sent_any:
                raise
            logger.warning("LLM stream failed after speech started; ending partial spoken response", exc_info=True)

        if metrics:
            metrics.llm_ms = (time.perf_counter() - started) * 1000
        answer = "".join(answer_parts).strip()
        if pending.strip():
            if not created:
                await self._send_response_created(response_id, item_id, metrics=metrics)
                created = True
            await self._send_text_segments(
                pending,
                response_id=response_id,
                item_id=item_id,
                metrics=metrics,
                voice=voice,
            )
            sent_any = True
        if not sent_any or not answer:
            return False
        logger.info(
            "LLM streamed session_id=%s turn_id=%s llm_ms=%.0f text_chars=%d",
            self.session_id,
            metrics.turn_id if metrics else "-",
            (time.perf_counter() - started) * 1000,
            len(answer),
        )
        self._fire_record_turn(transcript, answer)
        await self._send_response_done(response_id=response_id, item_id=item_id, transcript=answer)
        return True

    async def _ask_llm_with_tools(
        self,
        transcript: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        instructions: str | None = None,
    ) -> str | None:
        if instructions is None:
            instructions = self._effective_instructions()
        instructions = await self._inject_memory(transcript, instructions)
        await self.llm.ensure_active_conversation()
        started = time.perf_counter()
        response = await self.llm.chat(
            transcript,
            instructions=instructions,
            tools=self.tools.openai_tools(),
        )
        if metrics:
            metrics.llm_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "LLM completed session_id=%s turn_id=%s llm_ms=%.0f tool_calls=%d text_chars=%d",
            self.session_id,
            metrics.turn_id if metrics else "-",
            (time.perf_counter() - started) * 1000,
            len(response.tool_calls),
            len(response.text),
        )
        if not response.tool_calls:
            final_text = response.text
            self._fire_record_turn(transcript, final_text)
            return final_text

        messages = self._tool_followup_messages(transcript, response, instructions=instructions)
        waiting_for_client_tool = False
        needs_client_followup = False
        created_for_tools = False
        for tool_call in response.tool_calls:
            execution = await self.tools.execute(tool_call.name, tool_call.arguments)
            if execution.forwarded:
                if not created_for_tools:
                    await self._send_response_created(response_id, item_id, metrics=metrics)
                    created_for_tools = True
                if execution.needs_response:
                    self.pending_tool_context = messages
                await self._send_tool_call_event(
                    tool_call=tool_call,
                    execution=execution,
                    response_id=response_id,
                    item_id=item_id,
                )
                waiting_for_client_tool = True
                needs_client_followup = needs_client_followup or execution.needs_response
                continue
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.name,
                    "content": execution.result,
                }
            )
        if waiting_for_client_tool:
            await self._send_response_done(response_id=response_id, item_id=item_id, transcript="")
            if not needs_client_followup:
                self.pending_tool_context = None
            logger.info(
                "Forwarded client tool batch session_id=%s needs_followup=%s pending_context_messages=%d",
                self.session_id,
                needs_client_followup,
                len(messages) if needs_client_followup else 0,
            )
            return None
        final_started = time.perf_counter()
        final = await self.llm.chat(messages=messages, instructions=instructions)
        if metrics:
            metrics.llm_ms += (time.perf_counter() - final_started) * 1000
        final_text = (final.text or self._fallback_text_for(transcript))
        self._fire_record_turn(transcript, final_text)
        return final_text

    def _fire_record_turn(self, transcript: str, answer: str) -> None:
        if self.settings.llm_provider.strip().lower() != "openai_compatible":
            return
        if not self.settings.memory_enabled:
            return
        memory = getattr(self, "memory", None)
        if memory is None or not answer:
            return
        asyncio.create_task(self._record_memory_turn(transcript, answer))

    async def _send_tool_call_event(
        self,
        *,
        tool_call: ToolCall,
        execution: ToolExecution,
        response_id: str,
        item_id: str,
    ) -> None:
        await self._send(
            {
                "type": "response.function_call_arguments.done",
                "event_id": self._event_id(),
                "response_id": response_id,
                "item_id": item_id,
                "output_index": 0,
                "call_id": tool_call.id,
                "name": execution.name,
                "arguments": json.dumps(execution.arguments, ensure_ascii=False),
            }
        )
        logger.info(
            "Forwarded client tool call session_id=%s response_id=%s call_id=%s tool=%s args_keys=%s",
            self.session_id,
            response_id,
            tool_call.id,
            execution.name,
            sorted(execution.arguments),
        )

    def _tool_followup_messages(
        self,
        transcript: str,
        response: LLMResponse,
        *,
        instructions: str | None = None,
    ) -> list[Message]:
        assistant_message: Message = {"role": "assistant", "content": response.text or ""}
        assistant_message["tool_calls"] = [self._tool_call_message(tool_call) for tool_call in response.tool_calls]
        return [
            {"role": "system", "content": self._tool_system_prompt(instructions=instructions)},
            {"role": "user", "content": transcript},
            assistant_message,
        ]

    @staticmethod
    def _tool_call_message(tool_call: ToolCall) -> dict[str, Any]:
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {"name": tool_call.name, "arguments": tool_call.arguments or "{}"},
        }

    async def _send_response(
        self,
        text: str,
        *,
        transcript: str,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        voice = self._effective_tts_voice(voice)
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        await self._send_response_created(response_id, item_id, metrics=metrics)
        await self._send_text_segments(
            text,
            response_id=response_id,
            item_id=item_id,
            metrics=metrics,
            voice=voice,
        )
        await self._send_response_done(response_id=response_id, item_id=item_id, transcript=transcript)

    async def _send_response_created(
        self,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
    ) -> None:
        self.active_response_id = response_id
        self.active_item_id = item_id
        self.active_metrics = metrics
        self.state = "speaking"
        await self._send(
            {
                "type": "response.created",
                "event_id": self._event_id(),
                "response": {"id": response_id, "object": "realtime.response", "status": "in_progress"},
            }
        )

    async def _send_text_segments(
        self,
        text: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        segments = self._split_tts_segments(self._sanitize_tts_text(text))
        if not segments:
            return
        if getattr(getattr(self, "tts", None), "supports_streaming", False):
            for segment in segments:
                await self._send_tts_segment(
                    segment,
                    response_id=response_id,
                    item_id=item_id,
                    metrics=metrics,
                    voice=voice,
                )
            return
        pending = asyncio.create_task(self._synthesize_tts(segments[0], metrics=metrics, voice=voice))
        for index, segment in enumerate(segments):
            pcm16 = await pending
            next_index = index + 1
            pending = (
                asyncio.create_task(self._synthesize_tts(segments[next_index], metrics=metrics, voice=voice))
                if next_index < len(segments)
                else None
            )
            await self._send_pcm_segment(pcm16, response_id=response_id, item_id=item_id, metrics=metrics)

    async def _send_audio_segment(
        self,
        text: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        await self._send_tts_segment(
            text,
            response_id=response_id,
            item_id=item_id,
            metrics=metrics,
            voice=voice,
        )

    async def _send_tts_segment(
        self,
        text: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        if getattr(getattr(self, "tts", None), "supports_streaming", False):
            try:
                await self._stream_tts_segment(
                    text,
                    response_id=response_id,
                    item_id=item_id,
                    metrics=metrics,
                    voice=voice,
                )
                return
            except Exception:
                logger.warning("Streaming TTS failed; falling back to complete segment synthesis", exc_info=True)
        pcm16 = await self._synthesize_tts(text, metrics=metrics, voice=voice)
        await self._send_pcm_segment(pcm16, response_id=response_id, item_id=item_id, metrics=metrics)

    async def _stream_tts_segment(
        self,
        text: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> None:
        voice = self._effective_tts_voice(voice)
        stream = getattr(self.tts, "stream_pcm")
        started = time.perf_counter()
        byte_limit = self._tts_audio_byte_limit(text)
        sent_bytes = 0
        first_chunk = True
        async for pcm16 in stream(text, voice=voice):
            if not pcm16:
                continue
            if self.active_response_id != response_id:
                break
            if byte_limit > 0:
                remaining = byte_limit - sent_bytes
                if remaining <= 0:
                    logger.warning(
                        "Stopping abnormal streaming TTS text_chars=%d allowed_seconds=%.2f voice=%s",
                        len(text),
                        byte_limit / 2 / self.settings.sample_rate,
                        self._voice_label(voice) or "default",
                    )
                    break
                if len(pcm16) > remaining:
                    pcm16 = pcm16[:remaining]
            if first_chunk:
                elapsed_ms = (time.perf_counter() - started) * 1000
                if metrics:
                    metrics.tts_segments += 1
                    if metrics.first_tts_ms == 0:
                        metrics.first_tts_ms = elapsed_ms
                logger.info(
                    "TTS stream started in %.0f ms chars=%d voice=%s seed=%s model=%s",
                    elapsed_ms,
                    len(text),
                    self._voice_label(voice) or "default",
                    voice.seed if voice and voice.seed is not None else self.settings.qwentts_cpp_seed,
                    Path(voice.model).name if voice and voice.model else Path(self.settings.qwentts_cpp_model).name,
                )
                first_chunk = False
            sent_bytes += len(pcm16)
            await self._send_pcm_segment(pcm16, response_id=response_id, item_id=item_id, metrics=metrics)
        if first_chunk and metrics:
            metrics.tts_segments += 1

    async def _synthesize_tts(
        self,
        text: str,
        *,
        metrics: TurnMetrics | None = None,
        voice: TtsVoice | None = None,
    ) -> bytes:
        voice = self._effective_tts_voice(voice)
        started = time.perf_counter()
        try:
            pcm16 = await self.tts.synthesize(text, voice=voice)
        except Exception:
            if self.settings.tts_voice_source.strip().lower() != "ws" or voice.is_empty():
                raise
            fallback_voice = TtsVoice.from_settings(self.settings)
            logger.warning(
                "TTS failed with WS voice %s; retrying configured voice",
                self._voice_label(voice),
                exc_info=True,
            )
            pcm16 = await self.tts.synthesize(text, voice=fallback_voice)
        pcm16 = self._limit_tts_audio(text, pcm16, voice=voice)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if metrics:
            metrics.tts_segments += 1
            if metrics.first_tts_ms == 0:
                metrics.first_tts_ms = elapsed_ms
        audio_seconds = len(pcm16) / 2 / self.settings.sample_rate if pcm16 else 0.0
        logger.info(
            "TTS segment completed in %.0f ms chars=%d audio_seconds=%.2f voice=%s seed=%s model=%s",
            elapsed_ms,
            len(text),
            audio_seconds,
            self._voice_label(voice) or "default",
            voice.seed if voice and voice.seed is not None else self.settings.qwentts_cpp_seed,
            Path(voice.model).name if voice and voice.model else Path(self.settings.qwentts_cpp_model).name,
        )
        return pcm16

    def _limit_tts_audio(self, text: str, pcm16: bytes, *, voice: TtsVoice | None = None) -> bytes:
        allowed_bytes = self._tts_audio_byte_limit(text)
        if allowed_bytes <= 0 or not pcm16:
            return pcm16
        sample_rate = self.settings.sample_rate
        actual_seconds = len(pcm16) / 2 / sample_rate
        allowed_seconds = allowed_bytes / 2 / sample_rate
        if len(pcm16) <= allowed_bytes:
            return pcm16
        logger.warning(
            "Trimming abnormal TTS audio text_chars=%d audio_seconds=%.2f allowed_seconds=%.2f voice=%s",
            len(text),
            actual_seconds,
            allowed_seconds,
            self._voice_label(voice) or "default",
        )
        return pcm16[:allowed_bytes]

    def _tts_audio_byte_limit(self, text: str) -> int:
        max_seconds = float(getattr(self.settings, "tts_max_audio_seconds", 0.0) or 0.0)
        if max_seconds <= 0:
            return 0
        text_budget = max_seconds
        if len(text) <= 16:
            text_budget = min(max_seconds, 8.0)
        elif len(text) <= 48:
            text_budget = min(max_seconds, 12.0)
        allowed_seconds = max(3.0, text_budget)
        return int(allowed_seconds * self.settings.sample_rate) * 2

    async def _send_pcm_segment(
        self,
        pcm16: bytes,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
    ) -> None:
        for part in chunk_pcm16(
            pcm16,
            chunk_ms=self.settings.response_audio_chunk_ms,
            sample_rate=self.settings.sample_rate,
        ):
            if self.active_response_id != response_id:
                return
            if metrics:
                metrics.audio_chunks += 1
                if metrics.first_audio_ms == 0:
                    metrics.first_audio_ms = (time.perf_counter() - metrics.started_at) * 1000
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
            delay_ms = max(0, int(getattr(self.settings, "response_audio_chunk_send_delay_ms", 0) or 0))
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000)

    def _tool_system_prompt(self, *, instructions: str | None = None) -> str:
        base = (
            "你正在通过 Reachy Mini Lite 机器人和用户语音对话。"
            "请根据工具结果继续用用户语言给出简短、自然、适合语音播报的回答。"
            "工具调用必须通过 API 的 tool_calls/function_call 结构化字段完成，"
            "不要在正文输出 <tool_call>、<function>、JSON 工具调用或任何工具标签。"
            "工具名、JSON、动作参数和表情标签不能作为语音内容读出来。"
            "不要描述或评论自己的动作、工具或执行过程；用户要求跳舞、摇头、表情等动作时，应调用工具而不是说自己正在做。"
            "只输出要被朗读的自然语言；不要输出 emoji、颜文字、舞台提示、括号里的情绪动作，"
            "也不要写音色、嗓音、语速、语调或口音描述。音色描述由 TTS 声音配置单独控制。"
            "如果工具已经转发给客户端执行，只需要自然回应用户，不要假装自己直接操作了硬件。"
        )
        effective = instructions if instructions is not None else self._effective_instructions()
        if effective:
            return f"{base}\n\n当前人格和表达风格：\n{effective[:2500]}"
        return base

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

    def _split_tts_segments(self, text: str) -> list[str]:
        min_chars = max(8, self.settings.tts_segment_min_chars)
        max_chars = max(min_chars, self.settings.tts_segment_max_chars)
        pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?.\n])\s*", text) if piece.strip()]
        if not pieces:
            return []

        segments: list[str] = []
        current = ""
        fast_first_min_chars = min(min_chars, 6)
        for index, piece in enumerate(pieces):
            is_first = index == 0
            if is_first and len(piece) >= fast_first_min_chars and re.search(r"[。！？!?]$", piece):
                segments.append(piece)
                current = ""
                continue
            if current and len(current) + len(piece) > max_chars and len(current) >= min_chars:
                segments.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            segments.append(current)
        return [part for segment in segments for part in self._split_long_tts_segment(segment, max_chars)]

    def _pop_stream_tts_segments(self, text: str) -> tuple[list[str], str]:
        if not text.strip():
            return [], ""
        pieces = [piece for piece in re.split(r"(?<=[。！？!?.\n])\s*", text) if piece]
        if not pieces:
            return [], text
        remainder = ""
        if not re.search(r"[。！？!?.\n]\s*$", text):
            remainder = pieces.pop() if pieces else text
        ready_text = "".join(pieces).strip()
        if not ready_text:
            return [], remainder
        max_chars = max(max(8, self.settings.tts_segment_min_chars), self.settings.tts_segment_max_chars)
        sentence_segments = [
            piece.strip()
            for piece in re.split(r"(?<=[。！？!?.\n])\s*", self._sanitize_tts_text(ready_text))
            if piece.strip()
        ]
        segments = [
            part
            for segment in sentence_segments
            for part in self._split_long_tts_segment(segment, max_chars)
        ]
        if not segments:
            return [], text
        return segments, remainder

    @staticmethod
    def _split_long_tts_segment(text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        pieces = [piece.strip() for piece in re.split(r"(?<=[，,；;、])\s*", text) if piece.strip()]
        if len(pieces) == 1:
            return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]

        segments: list[str] = []
        current = ""
        for piece in pieces:
            if current and len(current) + len(piece) > max_chars:
                segments.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            segments.append(current)
        return segments

    def _sanitize_tts_text(self, text: str) -> str:
        if not self.settings.tts_strip_bracketed_cues:
            return text.strip()

        bracketed = re.compile(r"[\[【（(](.*?)[\]】）)]")

        def replace(match: re.Match[str]) -> str:
            cue = match.group(1).strip()
            if self._looks_like_tts_cue(cue):
                logger.debug("Stripping TTS cue: %s", match.group(0))
                return ""
            return match.group(0)

        cleaned = bracketed.sub(replace, text)
        if self.settings.tts_strip_emoji:
            cleaned = self._strip_emoji_and_symbols(cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([。！？!?，,；;、])", r"\1", cleaned)
        return cleaned.strip()

    @staticmethod
    def _strip_emoji_and_symbols(text: str) -> str:
        # Qwen3TTS is more stable when spoken text contains only speakable text,
        # not emoji, pictographs, or decorative variation selectors.
        return re.sub(
            r"[\U0001F000-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF\uFE0F\u200D]+",
            "",
            text,
        )

    @staticmethod
    def _looks_like_tts_cue(cue: str) -> bool:
        if not cue or len(cue) > 12:
            return False
        if re.search(r"[A-Za-z0-9]", cue):
            return False
        cue_words = {
            "呲牙",
            "龙牙",
            "微笑",
            "笑",
            "大笑",
            "苦笑",
            "偷笑",
            "眨眼",
            "鼓掌",
            "点头",
            "摇头",
            "叹气",
            "沉思",
            "开心",
            "惊讶",
            "害羞",
            "害怕",
            "卖萌",
            "调皮",
            "思考",
            "流泪",
            "哭",
        }
        return cue in cue_words

    async def _send_response_done(self, *, response_id: str, item_id: str, transcript: str) -> None:
        if self.active_response_id != response_id:
            return
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
        await self._send_response_done_status(response_id=response_id, status="completed")
        self._log_turn_metrics(self.active_metrics, status="completed")
        logger.info(
            "Response completed session_id=%s response_id=%s transcript_chars=%d",
            self.session_id,
            response_id,
            len(transcript),
        )
        self.active_response_id = None
        self.active_item_id = None
        self.active_metrics = None
        self.state = "idle"

    async def _cancel_processing(self, *, reason: str = "cancelled", send_done: bool = True) -> None:
        task = self.processing
        self.processing = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if send_done and self.active_response_id:
            response_id = self.active_response_id
            self.active_response_id = None
            self.active_item_id = None
            self.state = "cancelled"
            await self._send_response_done_status(response_id=response_id, status="cancelled", reason=reason)
            self._log_turn_metrics(self.active_metrics, status="cancelled")
            logger.info(
                "Response cancelled session_id=%s response_id=%s reason=%s",
                self.session_id,
                response_id,
                reason,
            )
        else:
            self.active_response_id = None
            self.active_item_id = None
        self.active_metrics = None

    def _log_turn_metrics(self, metrics: TurnMetrics | None, *, status: str) -> None:
        if not metrics or not self.settings.latency_logging:
            return
        total_ms = (time.perf_counter() - metrics.started_at) * 1000
        value = {
            "turn_id": metrics.turn_id,
            "status": status,
            "utterance_ms": metrics.utterance_ms,
            "stt_ms": metrics.stt_ms,
            "llm_ms": metrics.llm_ms,
            "first_tts_ms": metrics.first_tts_ms,
            "first_audio_ms": metrics.first_audio_ms,
            "total_ms": total_ms,
            "tts_segments": metrics.tts_segments,
            "audio_chunks": metrics.audio_chunks,
        }
        try:
            ConfigStore.default().add_metric("turn", value)
        except Exception:
            logger.debug("Failed to persist turn metrics", exc_info=True)
        logger.info(
            (
                "Turn latency %s status=%s utterance_ms=%d stt_ms=%.0f llm_ms=%.0f "
                "first_tts_ms=%.0f first_audio_ms=%.0f total_ms=%.0f "
                "tts_segments=%d audio_chunks=%d"
            ),
            metrics.turn_id,
            status,
            metrics.utterance_ms,
            metrics.stt_ms,
            metrics.llm_ms,
            metrics.first_tts_ms,
            metrics.first_audio_ms,
            total_ms,
            metrics.tts_segments,
            metrics.audio_chunks,
        )

    async def _send_response_done_status(
        self,
        *,
        response_id: str,
        status: str,
        reason: str | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "id": response_id,
            "object": "realtime.response",
            "status": status,
            "usage": {
                "input_token_details": {"audio_tokens": 0, "text_tokens": 0, "image_tokens": 0},
                "output_token_details": {"audio_tokens": 0, "text_tokens": 0},
            },
        }
        if reason:
            response["status_details"] = {"type": "cancelled", "reason": reason}
        await self._send({"type": "response.done", "event_id": self._event_id(), "response": response})

    async def _send_error(self, code: str, message: str) -> None:
        await self._send(
            {
                "type": "error",
                "event_id": self._event_id(),
                "error": {"type": "server_error", "code": code, "message": message},
            }
        )

    async def _send(self, event: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_text(json.dumps(event, ensure_ascii=False))

    @staticmethod
    def _event_id() -> str:
        return f"evt_{uuid.uuid4().hex}"
