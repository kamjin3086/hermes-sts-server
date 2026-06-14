from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from hermes_sts.audio import Utterance, chunk_pcm16
from hermes_sts.config import Settings
from hermes_sts.llm import LLMProvider, LLMResponse, Message, ToolCall
from hermes_sts.stt import SttProvider
from hermes_sts.tools import ToolExecution, ToolRegistry
from hermes_sts.tts import TtsProvider
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
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex}")

    def __post_init__(self) -> None:
        self.vad = build_vad(self.settings)
        self.tools = ToolRegistry()

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
                logger.debug("Dropping input audio while speaking to avoid self-listening")
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
                    self.processing = self._create_processing_task(
                        lambda: self._process_text_turn(transcript, metrics, instructions=turn_instructions),
                        metrics=metrics,
                    )
                elif self.pending_tool_context and self.pending_tool_results:
                    self.state = "processing"
                    metrics = TurnMetrics(
                        turn_id=f"turn_{uuid.uuid4().hex}",
                        started_at=time.perf_counter(),
                    )
                    turn_instructions = self._consume_response_instructions()
                    self.processing = self._create_processing_task(
                        lambda: self._process_tool_result_turn(metrics, instructions=turn_instructions),
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
        if isinstance(session.get("tools"), list):
            self.tools.set_client_tools(session.get("tools"))
        logger.info(
            "Session updated session_id=%s instructions_chars=%d client_tools=%d local_tools=%d",
            self.session_id,
            len(self.instructions),
            len(self.tools.client_tool_names()),
            len(self.tools.local_tool_names()),
        )

    def _apply_response_config(self, response_config: dict[str, Any]) -> None:
        instructions = response_config.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            self.next_response_instructions = instructions.strip()
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
        return self._merge_instructions(self.instructions, instructions)

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
    ) -> None:
        try:
            transcript = transcript.strip()
            if not transcript:
                self.state = "idle"
                return
            await self._respond_with_agent_wait(transcript, metrics=metrics, instructions=instructions)
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
            final = await self.llm.chat(messages=messages, instructions=instructions or self.instructions)
            if metrics:
                metrics.llm_ms = (time.perf_counter() - started) * 1000
            text = final.text.strip() or "好的，已完成。"
            await self._send_response(text, transcript=text, metrics=metrics)
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
    ) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        llm_task = asyncio.create_task(
            self._ask_llm_with_tools(
                transcript,
                response_id=response_id,
                item_id=item_id,
                metrics=metrics,
                instructions=instructions or self.instructions,
            )
        )
        filler_texts = self._filler_texts_for(transcript)
        filler_count = 0
        started = time.perf_counter()
        first_filler_pcm_task = None
        if filler_texts and self.settings.hermes_max_fillers > 0:
            first_filler_pcm_task = asyncio.create_task(self._synthesize_tts(filler_texts[0], metrics=metrics))

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
                await self._send_response(answer, transcript=answer, metrics=metrics)
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
                        )
                        filler_count += 1

            if answer is None:
                answer = await llm_task

            await self._send_text_segments(answer, response_id=response_id, item_id=item_id, metrics=metrics)
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

    async def _ask_llm_with_tools(
        self,
        transcript: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
        instructions: str | None = None,
    ) -> str:
        started = time.perf_counter()
        response = await self.llm.chat(
            transcript,
            instructions=instructions or self.instructions,
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
            return response.text

        messages = self._tool_followup_messages(transcript, response, instructions=instructions)
        waiting_for_client_tool = False
        for tool_call in response.tool_calls:
            execution = await self.tools.execute(tool_call.name, tool_call.arguments)
            if execution.forwarded:
                await self._send_tool_call_event(
                    tool_call=tool_call,
                    execution=execution,
                    response_id=response_id,
                    item_id=item_id,
                )
                waiting_for_client_tool = True
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
            self.pending_tool_context = messages
            logger.info(
                "Waiting for client tool result session_id=%s pending_context_messages=%d",
                self.session_id,
                len(messages),
            )
            return response.text.strip()
        final_started = time.perf_counter()
        final = await self.llm.chat(messages=messages, instructions=instructions or self.instructions)
        if metrics:
            metrics.llm_ms += (time.perf_counter() - final_started) * 1000
        return final.text or self._fallback_text_for(transcript)

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

    def _tool_system_prompt(self, *, instructions: str | None = None) -> str:
        base = (
            "你正在通过 Reachy Mini Lite 机器人和用户语音对话。"
            "请根据工具结果继续用用户语言给出简短、自然、适合语音播报的回答。"
            "工具名、JSON、动作参数和表情标签不能作为语音内容读出来。"
            "如果工具已经转发给客户端执行，只需要自然回应用户，不要假装自己直接操作了硬件。"
        )
        effective = instructions or self.instructions
        if effective:
            return f"{base}\n\nReachy 会话附加指令：\n{effective[:2500]}"
        return base

    @staticmethod
    def _tool_call_message(tool_call: ToolCall) -> dict[str, Any]:
        return {
            "id": tool_call.id,
            "type": "function",
            "function": {"name": tool_call.name, "arguments": tool_call.arguments or "{}"},
        }

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

    async def _send_response(
        self,
        text: str,
        *,
        transcript: str,
        metrics: TurnMetrics | None = None,
    ) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"item_{uuid.uuid4().hex}"
        await self._send_response_created(response_id, item_id, metrics=metrics)
        await self._send_text_segments(text, response_id=response_id, item_id=item_id, metrics=metrics)
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
    ) -> None:
        for segment in self._split_tts_segments(self._sanitize_tts_text(text)):
            await self._send_audio_segment(
                segment,
                response_id=response_id,
                item_id=item_id,
                metrics=metrics,
            )

    async def _send_audio_segment(
        self,
        text: str,
        *,
        response_id: str,
        item_id: str,
        metrics: TurnMetrics | None = None,
    ) -> None:
        pcm16 = await self._synthesize_tts(text, metrics=metrics)
        await self._send_pcm_segment(pcm16, response_id=response_id, item_id=item_id, metrics=metrics)

    async def _synthesize_tts(self, text: str, *, metrics: TurnMetrics | None = None) -> bytes:
        started = time.perf_counter()
        pcm16 = await self.tts.synthesize(text)
        elapsed_ms = (time.perf_counter() - started) * 1000
        if metrics:
            metrics.tts_segments += 1
            if metrics.first_tts_ms == 0:
                metrics.first_tts_ms = elapsed_ms
        logger.info("TTS segment completed in %.0f ms chars=%d", elapsed_ms, len(text))
        return pcm16

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
            await asyncio.sleep(0.01)

    def _split_tts_segments(self, text: str) -> list[str]:
        min_chars = max(8, self.settings.tts_segment_min_chars)
        max_chars = max(min_chars, self.settings.tts_segment_max_chars)
        pieces = [piece.strip() for piece in re.split(r"(?<=[。！？!?\.\n])\s*", text) if piece.strip()]
        if not pieces:
            return []

        segments: list[str] = []
        current = ""
        for piece in pieces:
            if current and len(current) + len(piece) > max_chars and len(current) >= min_chars:
                segments.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            segments.append(current)
        return [part for segment in segments for part in self._split_long_tts_segment(segment, max_chars)]

    @staticmethod
    def _split_long_tts_segment(text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        pieces = [piece.strip() for piece in re.split(r"(?<=[；;，,、])\s*", text) if piece.strip()]
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
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([。！？!?，,；;])", r"\1", cleaned)
        return cleaned.strip()

    @staticmethod
    def _looks_like_tts_cue(cue: str) -> bool:
        if not cue or len(cue) > 12:
            return False
        if re.search(r"[A-Za-z0-9]", cue):
            return False
        cue_words = {
            "呲牙",
            "龇牙",
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

    def _tool_system_prompt(self, *, instructions: str | None = None) -> str:
        base = (
            "你正在通过 Reachy Mini Lite 机器人和用户语音对话。"
            "请根据工具结果继续用用户语言给出简短、自然、适合语音播报的回答。"
            "工具名、JSON、动作参数和表情标签不能作为语音内容读出来。"
            "如果工具已经转发给客户端执行，只需要自然回应用户，不要假装自己直接操作了硬件。"
        )
        effective = instructions or self.instructions
        if effective:
            return f"{base}\n\nReachy 会话附加指令：\n{effective[:2500]}"
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
        for piece in pieces:
            if current and len(current) + len(piece) > max_chars and len(current) >= min_chars:
                segments.append(current)
                current = piece
            else:
                current = f"{current}{piece}" if current else piece
        if current:
            segments.append(current)
        return [part for segment in segments for part in self._split_long_tts_segment(segment, max_chars)]

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
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r"\s+([。！？!?，,；;、])", r"\1", cleaned)
        return cleaned.strip()

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
