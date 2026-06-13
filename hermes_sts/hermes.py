from __future__ import annotations

import logging
import random

import httpx

from hermes_sts.config import Settings

logger = logging.getLogger(__name__)


class HermesClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.history: list[dict[str, str]] = []

    async def ask(self, transcript: str, instructions: str | None = None) -> str:
        messages: list[dict[str, str]] = []
        system = (
            "你正在通过 Reachy Mini Lite 机器人和用户语音对话。"
            "请用用户正在使用的语言自然、简短地回答；如果用户说中文，就优先使用中文。"
            "回答要适合语音播报，通常控制在 1 到 3 句。"
            "除非用户明确要求详细说明，否则不要长篇自我介绍、不要列清单、不要使用 Markdown 表格。"
            "不要解释内部系统、模型、接口、内存、服务状态或运行限制。"
        )
        if instructions:
            system = f"{system}\n\nReachy 会话附加指令：\n{instructions[:2500]}"
        messages.append({"role": "system", "content": system})
        messages.extend(self._history_for_prompt())
        messages.append({"role": "user", "content": transcript})

        body = {
            "model": self.settings.hermes_model,
            "messages": messages,
            "stream": False,
            "max_tokens": self.settings.hermes_max_tokens,
        }
        headers = {}
        if self.settings.hermes_api_key:
            headers["Authorization"] = f"Bearer {self.settings.hermes_api_key}"

        timeout = httpx.Timeout(
            connect=self.settings.hermes_connect_timeout_seconds,
            read=self.settings.hermes_read_timeout_seconds,
            write=10.0,
            pool=self.settings.hermes_connect_timeout_seconds,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{self.settings.hermes_base_url.rstrip('/')}/chat/completions",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return await self._fallback_or_raise(exc, messages)

        if data.get("hermes", {}).get("failed"):
            error = data.get("hermes", {}).get("error") or "Hermes reported a failed completion"
            return await self._fallback_or_raise(RuntimeError(str(error)), messages)

        choice = data["choices"][0]
        if choice.get("finish_reason") == "error":
            text = (choice.get("message", {}).get("content") or "Hermes returned finish_reason=error").strip()
            return await self._fallback_or_raise(RuntimeError(text), messages)

        text = (choice["message"].get("content") or "").strip()
        if text:
            self.history.append({"role": "user", "content": transcript})
            self.history.append({"role": "assistant", "content": text})
        return text or self._static_fallback_text(messages)

    async def _fallback_or_raise(self, exc: Exception, messages: list[dict[str, str]]) -> str:
        if self.settings.llm_fallback_enabled:
            fallback = await self._ask_llm_fallback(messages, exc)
            if fallback:
                return fallback
        if self.settings.hermes_allow_fallback:
            logger.warning("Hermes request failed, using static local fallback: %s", exc)
            return self._static_fallback_text(messages)
        raise exc

    async def _ask_llm_fallback(self, messages: list[dict[str, str]], original_exc: Exception) -> str:
        if not self.settings.llm_fallback_base_url or not self.settings.llm_fallback_model:
            return ""

        logger.warning("Hermes request failed, trying local LLM fallback: %s", original_exc)
        fallback_messages = list(messages)
        fallback_messages[0] = {
            "role": "system",
            "content": (
                fallback_messages[0]["content"]
                + "\n\nHermes agent 暂时没有返回可用结果。"
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
            logger.warning("Local LLM fallback failed: %s", exc)
            return ""

        choice = data["choices"][0]
        text = (choice.get("message", {}).get("content") or "").strip()
        if text:
            self.history.append(messages[-1])
            self.history.append({"role": "assistant", "content": text})
            return text
        return ""

    def _static_fallback_text(self, messages: list[dict[str, str]]) -> str:
        configured = [
            item.strip()
            for item in self.settings.hermes_fallback_texts.split("|")
            if item.strip()
        ]
        if configured:
            return random.choice(configured)

        last_user = next(
            (message.get("content", "") for message in reversed(messages) if message.get("role") == "user"),
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

    def _history_for_prompt(self) -> list[dict[str, str]]:
        max_messages = max(0, self.settings.hermes_history_max_messages)
        max_chars = max(0, self.settings.hermes_history_max_chars)
        if not self.history or max_messages == 0 or max_chars == 0:
            return []

        recent = self.history[-max_messages:]
        total = 0
        kept_reversed: list[dict[str, str]] = []
        for message in reversed(recent):
            total += len(message.get("content", ""))
            if total > max_chars and kept_reversed:
                break
            kept_reversed.append(message)
        return list(reversed(kept_reversed))
