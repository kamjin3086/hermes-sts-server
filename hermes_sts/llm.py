from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from hermes_sts.config import Settings

logger = logging.getLogger(__name__)


Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str | dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMProvider(Protocol):
    async def chat(
        self,
        transcript: str | None = None,
        *,
        messages: list[Message] | None = None,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        ...


class BaseOpenAIChatProvider:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: list[Message] = []
        self.last_llm_call_started_at: float | None = None
        self._request_gate = asyncio.Semaphore(max(1, settings.llm_max_concurrent_requests))

    async def chat(
        self,
        transcript: str | None = None,
        *,
        messages: list[Message] | None = None,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
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
            )

    async def _chat_once(
        self,
        *,
        transcript: str | None,
        messages: list[Message] | None,
        instructions: str | None,
        tools: list[dict[str, Any]] | None,
    ) -> LLMResponse:
        now = time.monotonic()
        self._reset_history_if_idle(now)
        self.last_llm_call_started_at = now

        prompt_messages = messages or self._messages_for_transcript(transcript or "", instructions)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": prompt_messages,
            "stream": False,
            "max_tokens": self.max_tokens,
        }
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
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])
        text = (message.get("content") or "").strip()
        if transcript and text and not tool_calls:
            self.history.append({"role": "user", "content": transcript})
            self.history.append({"role": "assistant", "content": text})
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

    def reset_history(self, reason: str = "manual") -> None:
        if self.history:
            logger.info("Resetting local LLM history reason=%s messages=%s", reason, len(self.history))
        self.history.clear()

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
            "不要把工具名、JSON 参数、动作枚举或表情标签写进要播报的文字里。"
        )
        if instructions:
            return f"{system}\n\nReachy 会话附加指令：\n{instructions[:2500]}"
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

        recent = self.history[-max_messages:]
        total = 0
        kept_reversed: list[Message] = []
        for message in reversed(recent):
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
