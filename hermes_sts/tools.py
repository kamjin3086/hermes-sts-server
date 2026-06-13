from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[str] | str]
ToolMode = Literal["local", "client"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    kind: str
    parameters: dict[str, Any]
    mode: ToolMode
    handler: ToolHandler | None = None

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    result: str
    mode: ToolMode
    forwarded: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._local_tools: dict[str, ToolSpec] = {}
        self._client_tools: dict[str, ToolSpec] = {}
        self.register_local(
            ToolSpec(
                name="noop",
                description="A no-op tool reserved for testing the tool call path.",
                kind="fast",
                mode="local",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _args: "noop completed",
            )
        )
        self.register_local(
            ToolSpec(
                name="current_time",
                description="Return the server local time.",
                kind="fast",
                mode="local",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _args: datetime.now().isoformat(timespec="seconds"),
            )
        )

    def register_local(self, tool: ToolSpec) -> None:
        if tool.mode != "local" or tool.handler is None:
            raise ValueError("local tools require mode='local' and a handler")
        self._local_tools[tool.name] = tool

    def set_client_tools(self, raw_tools: list[dict[str, Any]] | None) -> None:
        self._client_tools.clear()
        for raw_tool in raw_tools or []:
            tool = self._tool_from_realtime_schema(raw_tool)
            if tool is not None:
                self._client_tools[tool.name] = tool
        if self._client_tools:
            logger.info("Registered client tools: %s", ", ".join(sorted(self._client_tools)))

    def openai_tools(self) -> list[dict[str, Any]]:
        tools: dict[str, ToolSpec] = {}
        tools.update(self._local_tools)
        tools.update(self._client_tools)
        return [tool.as_openai_tool() for tool in tools.values()]

    def client_tool_names(self) -> list[str]:
        return sorted(self._client_tools)

    def local_tool_names(self) -> list[str]:
        return sorted(self._local_tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._client_tools.get(name) or self._local_tools.get(name)

    async def execute(self, name: str, arguments: str | dict[str, Any] | None) -> ToolExecution:
        args = self._parse_arguments(arguments)
        tool = self.get(name)
        if tool is None:
            return ToolExecution(
                name=name,
                arguments=args,
                result=f"Tool {name!r} is not registered or enabled in this session.",
                mode="client",
                forwarded=False,
            )

        if tool.mode == "client":
            return ToolExecution(
                name=name,
                arguments=args,
                result=f"Forwarded client tool {name!r} for execution.",
                mode="client",
                forwarded=True,
            )

        if tool.handler is None:
            return ToolExecution(name=name, arguments=args, result="Tool has no handler.", mode="local")

        result = tool.handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        return ToolExecution(name=name, arguments=args, result=str(result), mode="local")

    @classmethod
    def _tool_from_realtime_schema(cls, raw_tool: dict[str, Any]) -> ToolSpec | None:
        if raw_tool.get("type") != "function":
            return None

        if isinstance(raw_tool.get("function"), dict):
            function = raw_tool["function"]
            name = str(function.get("name") or "").strip()
            description = str(function.get("description") or "")
            parameters = function.get("parameters") or {}
        else:
            name = str(raw_tool.get("name") or "").strip()
            description = str(raw_tool.get("description") or "")
            parameters = raw_tool.get("parameters") or {}

        if not cls._is_valid_tool_name(name):
            logger.warning("Ignoring invalid client tool name: %r", name)
            return None
        if not isinstance(parameters, dict):
            logger.warning("Ignoring client tool %s because parameters is not an object", name)
            return None

        return ToolSpec(
            name=name,
            description=description[:1000],
            kind="client",
            mode="client",
            parameters=parameters,
        )

    @staticmethod
    def _parse_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
        if arguments is None:
            return {}
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"raw": arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    @staticmethod
    def _is_valid_tool_name(name: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", name))
