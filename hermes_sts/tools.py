from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_sts.config import Settings
    from hermes_sts.websearch import WebSearchProvider

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
    needs_response: bool = True
    category: str = "general"

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _canonical_json_value(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolExecution:
    name: str
    arguments: dict[str, Any]
    result: str
    mode: ToolMode
    forwarded: bool = False
    needs_response: bool = True
    category: str = "general"


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
        return [tool.as_openai_tool() for tool in sorted(tools.values(), key=lambda tool: tool.name)]

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "local": [self._snapshot_tool(tool) for tool in self._local_tools.values()],
            "client": [self._snapshot_tool(tool) for tool in self._client_tools.values()],
        }

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
                needs_response=tool.needs_response,
                category=tool.category,
            )

        if tool.handler is None:
            return ToolExecution(
                name=name,
                arguments=args,
                result="Tool has no handler.",
                mode="local",
                needs_response=tool.needs_response,
                category=tool.category,
            )

        result = tool.handler(args)
        if asyncio.iscoroutine(result):
            result = await result
        return ToolExecution(
            name=name,
            arguments=args,
            result=str(result),
            mode="local",
            needs_response=tool.needs_response,
            category=tool.category,
        )

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
            needs_response=cls._client_tool_needs_response(name, description),
            category=cls._client_tool_category(name, description),
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

    @staticmethod
    def _snapshot_tool(tool: ToolSpec) -> dict[str, Any]:
        properties = tool.parameters.get("properties") if isinstance(tool.parameters, dict) else {}
        required = tool.parameters.get("required") if isinstance(tool.parameters, dict) else []
        if not isinstance(properties, dict):
            properties = {}
        if not isinstance(required, list):
            required = []
        return {
            "name": tool.name,
            "description": tool.description,
            "mode": tool.mode,
            "kind": tool.kind,
            "parameters_count": len(properties),
            "required": [str(item) for item in required],
            "parameters": sorted(str(key) for key in properties),
            "needs_response": tool.needs_response,
            "category": tool.category,
            "injected": True,
            "last_called_at": None,
            "last_result": None,
        }

    @classmethod
    def _client_tool_needs_response(cls, name: str, description: str) -> bool:
        category = cls._client_tool_category(name, description)
        if category in {"motion", "emotion", "idle"}:
            return False
        return True

    @staticmethod
    def _client_tool_category(name: str, description: str) -> str:
        key = name.strip().lower()
        text = f"{key} {description}".lower()
        if key in {"dance", "stop_dance", "move_head", "sweep_look", "go_to_sleep"}:
            return "motion"
        if key in {"play_emotion", "stop_emotion"}:
            return "emotion"
        if key == "idle_do_nothing":
            return "idle"
        if key == "camera" or "camera" in text or "picture" in text:
            return "vision"
        if key in {"remember", "forget"}:
            return "memory"
        if key in {"task_status", "task_cancel"}:
            return "task"
        return "general"


def register_default_local_tools(
    registry: ToolRegistry,
    settings: Settings,
    *,
    web_search_provider: WebSearchProvider | None = None,
) -> None:
    """Register STS-local tools gated by settings and provider.

    Currently only web_search for openai_compatible mode.
    """
    if settings.llm_provider.strip().lower() != "openai_compatible":
        return
    if not settings.web_search_enabled:
        return
    if web_search_provider is None or web_search_provider.description() == "noop":
        return
    registry.register_local(
        ToolSpec(
            name="web_search",
            description="Search the web for current information. Returns titles, URLs and short snippets. Use when user asks about current events, weather, news, prices or anything you don't know. Keep queries concise.",
            kind="slow",
            mode="local",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query in user's language"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_make_web_search_handler(web_search_provider),
        )
    )


def _make_web_search_handler(provider: WebSearchProvider) -> ToolHandler:
    async def handler(args: dict[str, Any]) -> str:
        query = args.get("query", "") if isinstance(args, dict) else ""
        if not query:
            return "No search query provided."
        try:
            hits = await provider.search(query)
        except Exception:
            return "Search temporarily unavailable."
        if not hits:
            return "No results found."
        lines = [
            "Web search returned the following live results. Use these results to answer the user directly; do not tell the user to search again."
        ]
        for i, h in enumerate(hits[:5], 1):
            snippet = (h.content or "")[:300]
            lines.append(f"{i}. {h.title or '(no title)'}\n   {h.url}\n   {snippet}")
        return "\n\n".join(lines)[:2000]

    return handler


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    return value
