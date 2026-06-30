from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_sts.config import Settings
    from hermes_sts.websearch import WebSearchProvider

logger = logging.getLogger(__name__)
SHELL_CONTROL_TOKENS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "$(", "`"}
TERMINAL_RESULT_HEADER = (
    "Terminal command completed. Use stdout/stderr below to answer the user directly; "
    "do not ask the user to run the command again."
)

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

    These tools are intended for direct OpenAI-compatible model calls. Hermes
    agent mode has its own tool loop, so STS-local tools stay out of that path.
    """
    if settings.llm_provider.strip().lower() != "openai_compatible":
        return

    if settings.web_search_enabled and web_search_provider is not None and web_search_provider.description() != "noop":
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

    if settings.terminal_tool_enabled:
        registry.register_local(
            ToolSpec(
                name="terminal_exec",
                description="Run one configured, non-interactive terminal command on the STS server. Use for simple API calls, diagnostics or small code snippets when a dedicated tool is unavailable. The server enforces an executable allowlist, working directory boundary, timeout and output limit.",
                kind="slow",
                mode="local",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Command line to run. Shell pipelines and redirection are not supported; call one allowlisted executable with arguments.",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "Optional working directory relative to the configured terminal tool root.",
                        },
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
                handler=_make_terminal_handler(settings),
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


def _make_terminal_handler(settings: Settings) -> ToolHandler:
    async def handler(args: dict[str, Any]) -> str:
        try:
            command = str(args.get("command") or "").strip()
            if not command:
                return "No terminal command provided."
            argv = _parse_terminal_command(command)
            executable = Path(argv[0]).name
            allowed = _allowed_terminal_commands(settings)
            if executable not in allowed:
                return f"Terminal command rejected: executable {executable!r} is not allowlisted."
            cwd = _terminal_cwd(settings, str(args.get("cwd") or ""))
            timeout = _terminal_timeout(settings)
            env = _terminal_env()
            executable_path = argv[0] if os.path.sep in argv[0] else shutil.which(argv[0])
            if not executable_path:
                return f"Terminal command rejected: executable {executable!r} was not found on PATH."
        except ValueError as exc:
            return f"Terminal command rejected: {exc}"

        process = await asyncio.create_subprocess_exec(
            executable_path,
            *argv[1:],
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return f"Terminal command timed out after {timeout:.1f}s."

        return _format_terminal_result(process.returncode or 0, stdout, stderr, settings.terminal_tool_max_output_chars)

    return handler


def _parse_terminal_command(command: str) -> list[str]:
    if len(command) > 2000:
        raise ValueError("command is too long")
    if any(ch in command for ch in ("\x00", "\n", "\r")):
        raise ValueError("command must be a single line")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"could not parse command: {exc}") from exc
    if not argv:
        raise ValueError("command is empty")
    if any(token in SHELL_CONTROL_TOKENS or token.startswith("$(") for token in argv):
        raise ValueError("shell operators are not supported")
    return argv


def _allowed_terminal_commands(settings: Settings) -> set[str]:
    return {
        item.strip()
        for item in str(settings.terminal_tool_allowed_commands or "").split(",")
        if item.strip() and "/" not in item.strip()
    }


def _terminal_cwd(settings: Settings, raw_cwd: str) -> Path:
    root = Path(settings.terminal_tool_cwd or ".").expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"configured working directory does not exist: {root}")
    cwd = root
    if raw_cwd.strip():
        requested = Path(raw_cwd.strip()).expanduser()
        cwd = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError("cwd must stay under the configured terminal tool root") from exc
    if not cwd.is_dir():
        raise ValueError(f"cwd does not exist: {cwd}")
    return cwd


def _terminal_timeout(settings: Settings) -> float:
    try:
        return max(0.2, min(30.0, float(settings.terminal_tool_timeout_seconds)))
    except (TypeError, ValueError):
        return 6.0


def _terminal_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _format_terminal_result(returncode: int, stdout: bytes, stderr: bytes, limit: int) -> str:
    try:
        max_chars = max(500, min(12000, int(limit)))
    except (TypeError, ValueError):
        max_chars = 4000
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    body = f"{TERMINAL_RESULT_HEADER}\nexit_code: {returncode}\nstdout:\n{stdout_text}\nstderr:\n{stderr_text}"
    if len(body) <= max_chars:
        return body
    omitted = len(body) - max_chars
    return body[:max_chars] + f"\n...[truncated {omitted} chars]"


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    return value
