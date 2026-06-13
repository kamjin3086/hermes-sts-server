from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable


ToolHandler = Callable[[dict[str, Any]], Awaitable[str] | str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    kind: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.register(
            ToolSpec(
                name="noop",
                description="A no-op tool reserved for testing the tool call path.",
                kind="fast",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _args: "noop completed",
            )
        )
        self.register(
            ToolSpec(
                name="current_time",
                description="Return the server local time.",
                kind="fast",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda _args: datetime.now().isoformat(timespec="seconds"),
            )
        )

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool.as_openai_tool() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: str | dict[str, Any] | None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"Tool {name!r} is not registered."
        args = self._parse_arguments(arguments)
        result = tool.handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

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
