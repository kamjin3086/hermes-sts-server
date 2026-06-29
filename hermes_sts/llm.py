from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, TYPE_CHECKING

import httpx

from hermes_sts.config import Settings

if TYPE_CHECKING:
    from hermes_sts.conversation_store import ConversationStore

logger = logging.getLogger(__name__)


Message = dict[str, Any]

INLINE_TOOL_TAG_RE = re.compile(
    r"<\s*(?:tool[_-]?call|function)(?:\s|=|>|$)",
    re.IGNORECASE,
)
INLINE_TOOL_BLOCK_RE = re.compile(
    r"<\s*tool[_-]?call\b[^>]*>.*?<\s*/\s*tool[_-]?call\s*>"
    r"|<\s*function(?:\s*=\s*[^>\s]+|\b[^>]*)>.*?<\s*/\s*function\s*>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_TOOL_NAME_RE = re.compile(
    r"<\s*function\s*=\s*([A-Za-z0-9_.:-]+)\s*>"
    r"|<\s*tool[_-]?call\s*>\s*([A-Za-z0-9_.:-]+)\s*>?",
    re.IGNORECASE,
)
INLINE_TOOL_PARAM_RE = re.compile(
    r"<\s*parameter\s*=\s*([A-Za-z0-9_.:-]+)\s*>\s*(.*?)\s*<\s*/\s*parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)
INLINE_TOOL_PREFIXES = ("<tool_call", "<tool-call", "<toolcall", "<function", "<function=")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMToolCallDetected(RuntimeError):
    """Raised when a streaming chat response switches to tool-call mode."""


class LLMProvider(Protocol):
    async def chat(
        self,
        transcript: str | None = None,
        *,
        messages: list[Message] | None = None,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> LLMResponse:
        ...


class BaseOpenAIChatProvider:
    supports_text_streaming = True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: list[Message] = []
        self.last_llm_call_started_at: float | None = None
        self._request_gate = asyncio.Semaphore(max(1, settings.llm_max_concurrent_requests))
        self.conversation_id: str | None = None
        self.conversation_store: "ConversationStore | None" = None

    async def chat(
        self,
        transcript: str | None = None,
        *,
        messages: list[Message] | None = None,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> LLMResponse:
        queued_at = time.monotonic()
        async with self._request_gate:
            queue_ms = int((time.monotonic() - queued_at) * 1000)
            if queue_ms > 250:
                logger.info("LLM request waited %sms for local concurrency gate", queue_ms)
            return await self._chat_once(
                transcript=transcript,
                messages=messages,
                instructions=instructions,
                tools=tools,
                conversation_id=conversation_id,
            )

    async def stream_text(
        self,
        transcript: str,
        *,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[str]:
        queued_at = time.monotonic()
        async with self._request_gate:
            queue_ms = int((time.monotonic() - queued_at) * 1000)
            if queue_ms > 250:
                logger.info("LLM stream waited %sms for local concurrency gate", queue_ms)
            async for chunk in self._stream_text_once(
                transcript=transcript,
                instructions=instructions,
                tools=tools,
                conversation_id=conversation_id,
            ):
                yield chunk

    async def _stream_text_once(
        self,
        *,
        transcript: str,
        instructions: str | None,
        tools: list[dict[str, Any]] | None,
        conversation_id: str | None = None,
    ) -> AsyncIterator[str]:
        now = time.monotonic()
        self._reset_history_if_idle(now)
        self.last_llm_call_started_at = now
        prompt_messages = self._prepare_messages(
            self._sanitize_prompt_messages(self._messages_for_transcript(transcript, instructions))
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": prompt_messages,
            "stream": True,
            "max_tokens": self.max_tokens,
        }
        self._apply_prompt_cache_options(body)
        if self.conversation_id is not None:
            body["user"] = self.conversation_id
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        text_parts: list[str] = []
        guard_buffer = ""
        try:
            async for chunk in self._post_chat_completions_stream(body):
                if guard_buffer or chunk.lstrip().startswith("<"):
                    guard_buffer += chunk
                    if self._looks_like_inline_tool_markup(guard_buffer):
                        raise LLMToolCallDetected("streaming response emitted inline tool-call markup")
                    if self._could_be_inline_tool_markup_prefix(guard_buffer):
                        continue
                    chunk = guard_buffer
                    guard_buffer = ""
                elif self._looks_like_inline_tool_markup(chunk):
                    raise LLMToolCallDetected("streaming response emitted inline tool-call markup")
                text_parts.append(chunk)
                yield chunk
        except LLMToolCallDetected:
            raise
        except Exception:
            raise
        if guard_buffer:
            text_parts.append(guard_buffer)
            yield guard_buffer

        text, stripped_inline_tool = self._strip_inline_tool_markup("".join(text_parts).strip())
        if stripped_inline_tool:
            logger.warning("Dropped inline tool-call markup from streamed assistant text before saving history")
        if text:
            self.history.append({"role": "user", "content": transcript})
            self.history.append({"role": "assistant", "content": text})
            if self.conversation_store is not None and self.conversation_id is not None:
                self.conversation_store.append_message(
                    self.conversation_id, "user", transcript, set_title_if_first=True
                )
                self.conversation_store.append_message(
                    self.conversation_id, "assistant", text
                )

    async def _chat_once(
        self,
        *,
        transcript: str | None,
        messages: list[Message] | None,
        instructions: str | None,
        tools: list[dict[str, Any]] | None,
        conversation_id: str | None = None,
    ) -> LLMResponse:
        now = time.monotonic()
        self._reset_history_if_idle(now)
        self.last_llm_call_started_at = now

        prompt_messages = self._prepare_messages(
            self._sanitize_prompt_messages(messages or self._messages_for_transcript(transcript or "", instructions))
        )
        body: dict[str, Any] = {
            "model": self.model,
            "messages": prompt_messages,
            "stream": False,
            "max_tokens": self.max_tokens,
        }
        self._apply_prompt_cache_options(body)
        if self.conversation_id is not None:
            body["user"] = self.conversation_id
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        try:
            data = await self._post_chat_completions(body)
        except Exception as exc:
            return await self._fallback_or_raise(exc, prompt_messages)

        if data.get("hermes", {}).get("failed"):
            error = data.get("hermes", {}).get("error") or "Hermes reported a failed completion"
            return await self._fallback_or_raise(RuntimeError(str(error)), prompt_messages)

        choice = data["choices"][0]
        if choice.get("finish_reason") == "error":
            text = (choice.get("message", {}).get("content") or "LLM returned finish_reason=error").strip()
            return await self._fallback_or_raise(RuntimeError(text), prompt_messages)

        message = choice.get("message") or {}
        tool_calls = self._parse_message_tool_calls(message)
        raw_text = (message.get("content") or "").strip()
        if not tool_calls:
            tool_calls = self._parse_inline_tool_calls(raw_text, tools)
            if tool_calls:
                logger.warning("Parsed inline tool-call markup from assistant content; backend did not return structured tool_calls")
        text, stripped_inline_tool = self._strip_inline_tool_markup(raw_text)
        if stripped_inline_tool and not tool_calls:
            logger.warning("Dropped inline tool-call markup from assistant content; backend did not return structured tool_calls")
        if transcript and text and not tool_calls:
            self.history.append({"role": "user", "content": transcript})
            self.history.append({"role": "assistant", "content": text})
            # Write-through must stay inside _request_gate critical section.
            if self.conversation_store is not None and self.conversation_id is not None:
                self.conversation_store.append_message(
                    self.conversation_id, "user", transcript, set_title_if_first=True
                )
                self.conversation_store.append_message(
                    self.conversation_id, "assistant", text
                )
        return LLMResponse(text=text, tool_calls=tool_calls)

    async def _post_chat_completions(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def _post_chat_completions_stream(self, body: dict[str, Any]) -> AsyncIterator[str]:
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    if payload == "[DONE]":
                        break
                    data = json.loads(payload)
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        finish_reason = choice.get("finish_reason")
                        if delta.get("tool_calls") or delta.get("function_call") or finish_reason in {"tool_calls", "function_call"}:
                            raise LLMToolCallDetected("streaming response requested tool calls")
                        content = delta.get("content")
                        if content:
                            text = str(content)
                            if self._looks_like_inline_tool_markup(text):
                                raise LLMToolCallDetected("streaming response emitted inline tool-call markup")
                            yield text

    async def _fallback_or_raise(self, exc: Exception, messages: list[Message]) -> LLMResponse:
        if self.settings.llm_fallback_enabled:
            fallback = await self._ask_llm_fallback(messages, exc)
            if fallback:
                return LLMResponse(text=fallback)
        if self.settings.hermes_allow_fallback:
            logger.warning("LLM request failed, using static local fallback: %s", exc)
            return LLMResponse(text=self._static_fallback_text(messages))
        raise exc

    async def _ask_llm_fallback(self, messages: list[Message], original_exc: Exception) -> str:
        if not self.settings.llm_fallback_base_url or not self.settings.llm_fallback_model:
            return ""

        logger.warning("Primary LLM request failed, trying fallback: %s", original_exc)
        fallback_messages = list(messages)
        fallback_messages[0] = {
            "role": "system",
            "content": (
                str(fallback_messages[0]["content"])
                + "\n\n主 LLM 暂时没有返回可用结果。"
                + "请直接作为机器人回答用户，继续使用用户的语言；如果用户使用中文，就用中文。"
                + "不要提到 fallback、错误、API 或内部系统。"
                + "回答保持简短，适合语音播报。"
            ),
        }
        body = {
            "model": self.settings.llm_fallback_model,
            "messages": fallback_messages,
            "stream": False,
            "max_tokens": self.settings.llm_fallback_max_tokens,
        }
        headers = {}
        if self.settings.llm_fallback_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_fallback_api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_fallback_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.settings.llm_fallback_base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("Fallback LLM failed: %s", exc)
            return ""

        text = (data["choices"][0].get("message", {}).get("content") or "").strip()
        if text:
            last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if last_user:
                self.history.append(last_user)
            self.history.append({"role": "assistant", "content": text})
        return text

    def _messages_for_transcript(self, transcript: str, instructions: str | None) -> list[Message]:
        messages: list[Message] = [{"role": "system", "content": self._system_prompt(instructions)}]
        messages.extend(self._history_for_prompt())
        messages.append({"role": "user", "content": transcript})
        return messages

    def _prepare_messages(self, messages: list[Message]) -> list[Message]:
        return messages

    @classmethod
    def _sanitize_prompt_messages(cls, messages: list[Message]) -> list[Message]:
        sanitized: list[Message] = []
        for message in messages:
            if message.get("role") != "assistant":
                sanitized.append(message)
                continue
            content = message.get("content")
            if not isinstance(content, str):
                sanitized.append(message)
                continue
            clean, stripped = cls._strip_inline_tool_markup(content)
            if not stripped:
                sanitized.append(message)
                continue
            updated = dict(message)
            updated["content"] = clean
            sanitized.append(updated)
        return sanitized

    @staticmethod
    def _looks_like_inline_tool_markup(text: str) -> bool:
        if INLINE_TOOL_TAG_RE.search(text):
            return True
        stripped = text.lstrip().lower()
        if not stripped.startswith("<"):
            return False
        return any(stripped.startswith(prefix) for prefix in INLINE_TOOL_PREFIXES)

    @staticmethod
    def _could_be_inline_tool_markup_prefix(text: str) -> bool:
        stripped = text.lstrip().lower()
        if not stripped.startswith("<"):
            return False
        return any(prefix.startswith(stripped) for prefix in INLINE_TOOL_PREFIXES)

    @staticmethod
    def _strip_inline_tool_markup(text: str) -> tuple[str, bool]:
        if not text or not INLINE_TOOL_TAG_RE.search(text):
            return text, False
        stripped = INLINE_TOOL_BLOCK_RE.sub("", text)
        if stripped != text:
            return stripped.strip(), True
        if text.lstrip().lower().startswith(INLINE_TOOL_PREFIXES):
            return "", True
        return text, True

    @classmethod
    def _parse_inline_tool_calls(cls, text: str, tools: list[dict[str, Any]] | None) -> list[ToolCall]:
        if not text or not tools or not INLINE_TOOL_TAG_RE.search(text):
            return []
        allowed_names = cls._tool_names(tools)
        if not allowed_names:
            return []
        calls: list[ToolCall] = []
        for index, block in enumerate(INLINE_TOOL_BLOCK_RE.findall(text) or [text]):
            match = INLINE_TOOL_NAME_RE.search(block)
            if not match:
                continue
            name = (match.group(1) or match.group(2) or "").strip()
            if name not in allowed_names:
                logger.warning("Ignoring inline tool-call markup for unknown tool: %s", name)
                continue
            args: dict[str, Any] = {}
            for key, value in INLINE_TOOL_PARAM_RE.findall(block):
                args[key.strip()] = cls._coerce_inline_tool_value(value.strip())
            calls.append(
                ToolCall(
                    id=f"call_inline_{index}",
                    name=name,
                    arguments=json.dumps(args, ensure_ascii=False),
                )
            )
        return calls

    @staticmethod
    def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
        names: set[str] = set()
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if isinstance(function, dict) and function.get("name"):
                names.add(str(function["name"]))
            elif isinstance(tool, dict) and tool.get("name"):
                names.add(str(tool["name"]))
        return names

    @staticmethod
    def _coerce_inline_tool_value(value: str) -> Any:
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
        try:
            return int(value)
        except ValueError:
            return value

    def _apply_prompt_cache_options(self, body: dict[str, Any]) -> None:
        if self.settings.llm_cache_prompt:
            body["cache_prompt"] = True
        if self.settings.llm_cache_slot >= 0:
            body["id_slot"] = self.settings.llm_cache_slot

    def archive_current_conversation(self, reason: str) -> None:
        if self.conversation_store is None or self.conversation_id is None:
            self.history.clear()
            return
        self.conversation_store.archive_conversation(self.conversation_id, reason)
        self.history.clear()
        self.conversation_id = None

    def reset_history(self, reason: str = "manual") -> None:
        if self.history:
            logger.info("Resetting local LLM history reason=%s messages=%s", reason, len(self.history))
        self.archive_current_conversation(reason)

    async def ensure_active_conversation(self) -> str:
        if self.conversation_id is not None:
            return self.conversation_id
        if self.conversation_store is not None:
            cid = await asyncio.to_thread(self.conversation_store.create_conversation)
            self.conversation_id = cid
            await asyncio.to_thread(
                self.conversation_store.reload_history_into, cid, self
            )
            return cid
        cid = f"conv_{uuid.uuid4().hex}"
        self.conversation_id = cid
        return cid

    def _reset_history_if_idle(self, now: float) -> None:
        idle_limit = max(0.0, self.settings.hermes_history_idle_reset_seconds)
        if not self.history or idle_limit <= 0 or self.last_llm_call_started_at is None:
            return
        idle_seconds = now - self.last_llm_call_started_at
        if idle_seconds >= idle_limit:
            self.reset_history(reason=f"idle_{int(idle_seconds)}s")

    @staticmethod
    def _system_prompt(instructions: str | None) -> str:
        system = (
            "你正在通过 Reachy Mini Lite 机器人和用户语音对话。"
            "请用用户正在使用的语言自然、简短地回答；如果用户说中文，就优先使用中文。"
            "回答要适合语音播报，通常控制在 1 到 3 句。"
            "除非用户明确要求详细说明，否则不要长篇自我介绍、不要列清单、不要使用 Markdown 表格。"
            "不要解释内部系统、模型、接口、内存、服务状态或运行限制。"
            "如果需要 Reachy Mini 做动作、看相机、跟踪或调用外部能力，请使用可用工具。"
            "工具调用必须通过 API 的 tool_calls/function_call 结构化字段完成，"
            "不要在正文输出 <tool_call>、<function>、JSON 工具调用或任何工具标签。"
            "不要把工具名、JSON 参数、动作枚举或表情标签写进要播报的文字里。"
            "只输出要被朗读的自然语言；不要输出 emoji、颜文字、舞台提示、括号里的情绪动作，"
            "也不要写音色、嗓音、语速、语调或口音描述。音色描述由 TTS 声音配置单独控制。"
        )
        if instructions:
            return f"{system}\n\n当前人格和表达风格：\n{instructions[:2500]}"
        return system

    @staticmethod
    def _parse_tool_calls(raw_tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for item in raw_tool_calls:
            function = item.get("function") or {}
            name = function.get("name")
            if not name:
                continue
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{len(tool_calls)}"),
                    name=str(name),
                    arguments=function.get("arguments"),
                )
            )
        return tool_calls

    @classmethod
    def _parse_message_tool_calls(cls, message: dict[str, Any]) -> list[ToolCall]:
        tool_calls = cls._parse_tool_calls(message.get("tool_calls") or [])
        if tool_calls:
            return tool_calls
        function = message.get("function_call") or {}
        name = function.get("name") if isinstance(function, dict) else ""
        if not name:
            return []
        return [
            ToolCall(
                id="call_0",
                name=str(name),
                arguments=function.get("arguments"),
            )
        ]

    def _static_fallback_text(self, messages: list[Message]) -> str:
        configured = [
            item.strip()
            for item in self.settings.hermes_fallback_texts.split("|")
            if item.strip()
        ]
        if configured:
            return random.choice(configured)

        last_user = next(
            (str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user"),
            "",
        )
        if any("\u4e00" <= char <= "\u9fff" for char in last_user):
            return random.choice(
                [
                    "我这边还没有等到完整结果，不过本地语音链路正常。你可以再问一次，我会继续接。",
                    "这次后端响应有点慢，我先不断开。你再说一遍或者稍等一下都可以。",
                    "我还在等处理结果，当前听和说都正常，我们可以继续。",
                ]
            )
        return self.settings.hermes_fallback_text

    def _history_for_prompt(self) -> list[Message]:
        max_messages = max(0, self.settings.hermes_history_max_messages)
        max_chars = max(0, self.settings.hermes_history_max_chars)
        if not self.history or max_messages == 0 or max_chars == 0:
            return []

        selected, anchor_count = self._history_window_with_anchor(max_messages)
        kept = self._trim_history_to_char_budget(selected, max_chars, anchor_count)
        return self._sanitize_prompt_messages(kept)

    def _history_window_with_anchor(self, max_messages: int) -> tuple[list[Message], int]:
        if len(self.history) <= max_messages:
            return list(self.history), len(self.history)

        anchor_messages = max(0, getattr(self.settings, "hermes_history_anchor_messages", 0))
        anchor_count = min(anchor_messages, max_messages)
        if anchor_count == 0:
            return list(self.history[-max_messages:]), 0

        tail_count = max_messages - anchor_count
        if tail_count == 0:
            return list(self.history[:anchor_count]), anchor_count

        anchor = self.history[:anchor_count]
        tail_start = max(anchor_count, len(self.history) - tail_count)
        return list(anchor) + list(self.history[tail_start:]), anchor_count

    @staticmethod
    def _trim_history_to_char_budget(messages: list[Message], max_chars: int, anchor_count: int) -> list[Message]:
        total = sum(len(str(message.get("content", ""))) for message in messages)
        if total <= max_chars:
            return messages

        kept = list(messages)
        index = min(max(anchor_count, 0), max(len(kept) - 1, 0))
        while len(kept) > max(anchor_count, 1) and total > max_chars and index < len(kept):
            content_len = len(str(kept[index].get("content", "")))
            total -= content_len
            del kept[index]
        if total <= max_chars:
            return kept

        kept_reversed: list[Message] = []
        total = 0
        for message in reversed(messages):
            total += len(str(message.get("content", "")))
            if total > max_chars and kept_reversed:
                break
            kept_reversed.append(message)
        return list(reversed(kept_reversed))

    @property
    def base_url(self) -> str:
        raise NotImplementedError

    @property
    def model(self) -> str:
        raise NotImplementedError

    @property
    def api_key(self) -> str:
        raise NotImplementedError

    @property
    def max_tokens(self) -> int:
        raise NotImplementedError

    @property
    def timeout(self) -> httpx.Timeout | float:
        raise NotImplementedError


class HermesAgentProvider(BaseOpenAIChatProvider):
    def _prepare_messages(self, messages: list[Message]) -> list[Message]:
        if not self.settings.hermes_voice_no_think:
            return messages
        prepared = [dict(message) for message in messages]
        for message in reversed(prepared):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                stripped = content.lstrip()
                if not stripped.startswith("/no_think"):
                    message["content"] = f"/no_think\n{content}"
            break
        return prepared

    @property
    def base_url(self) -> str:
        return self.settings.hermes_base_url

    @property
    def model(self) -> str:
        return self.settings.hermes_model

    @property
    def api_key(self) -> str:
        return self.settings.hermes_api_key

    @property
    def max_tokens(self) -> int:
        return self.settings.hermes_max_tokens

    @property
    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.settings.hermes_connect_timeout_seconds,
            read=self.settings.hermes_read_timeout_seconds,
            write=10.0,
            pool=self.settings.hermes_connect_timeout_seconds,
        )


class OpenAICompatibleProvider(BaseOpenAIChatProvider):
    @property
    def base_url(self) -> str:
        return self.settings.llm_base_url

    @property
    def model(self) -> str:
        return self.settings.llm_model

    @property
    def api_key(self) -> str:
        return self.settings.llm_api_key

    @property
    def max_tokens(self) -> int:
        return self.settings.llm_max_tokens

    @property
    def timeout(self) -> float:
        return self.settings.llm_timeout_seconds


def build_llm(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.strip().lower()
    if provider == "hermes_agent":
        return HermesAgentProvider(settings)
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    raise RuntimeError(f"Unsupported STS_LLM_PROVIDER={settings.llm_provider!r}")
