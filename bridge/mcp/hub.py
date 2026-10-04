"""Starts the MCP servers once over stdio and offers their tools to the model.

Raw tools are offered as `<server>__<tool>` (filtered by the server's "tools"
allow-list). A server with an "adapter" offers only the adapter's tools, as
`<server>__<adapter tool>`; its raw tools are callable by the adapter alone.
A server that fails to start, or lacks what its adapter needs, is logged and
left out; the skill keeps working without it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Mapping
from concurrent.futures import Future
from typing import Any, Protocol

from .adapters import ADAPTERS, Adapter
from .config import ServerSpec

log = logging.getLogger("bridge.mcp")

_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")
ADAPTER_PREPARE_SECONDS = 8.0


class ToolBox(Protocol):
    def definitions(self) -> list[dict[str, Any]]: ...
    def instructions(self) -> str: ...
    def call(self, name: str, arguments: Mapping[str, Any], timeout: float) -> str: ...


class NoTools:
    def definitions(self) -> list[dict[str, Any]]:
        return []

    def instructions(self) -> str:
        return ""

    def call(self, name: str, arguments: Mapping[str, Any], timeout: float) -> str:
        return f"Unknown tool {name}."


def public_name(server: str, tool: str) -> str:
    return _NAME_OK.sub("_", f"{server}__{tool}")[:64]


def _function(name: str, description: str, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description[:600],
            "parameters": dict(parameters or {"type": "object", "properties": {}}),
        },
    }


def _result_text(result: Any, limit: int) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"[{getattr(block, 'type', 'content')} omitted]")
    out = "\n".join(parts).strip() or "(empty result)"
    if result.is_error:
        out = f"Tool error: {out}"
    return out[:limit]


class McpHub:
    """Owns an asyncio loop on a daemon thread with one open MCP client per server."""

    def __init__(self, specs: list[ServerSpec], max_result_chars: int) -> None:
        self._specs = specs
        self._limit = max_result_chars
        self._loop = asyncio.new_event_loop()
        self._ready: Future[None] = Future()
        self._stop: asyncio.Event | None = None
        self._clients: dict[str, Any] = {}
        self._routes: dict[str, tuple[str, str]] = {}  # offered name -> (server, raw tool)
        self._adapter_routes: dict[str, tuple[Adapter, str]] = {}  # offered name -> (adapter, local tool)
        self._adapter_specs: list[ServerSpec] = []
        self._defs: list[dict[str, Any]] = []
        self._notes: list[str] = []
        self._thread = threading.Thread(target=self._loop.run_forever, name="mcp-hub", daemon=True)

    def start(self, timeout: float = 30.0) -> McpHub:
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        self._ready.result(timeout=timeout)
        # Adapters may call their server while preparing, which needs the loop
        # thread free, so they are built here rather than inside _run.
        for spec in self._adapter_specs:
            self._attach_adapter(spec)
        return self

    async def _run(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import Client, StdioServerParameters

        self._stop = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                for spec in self._specs:
                    if spec.adapter is not None and spec.adapter.type not in ADAPTERS:
                        log.error("MCP server %s: unknown adapter %r; skipped", spec.name, spec.adapter.type)
                        continue
                    try:
                        params = StdioServerParameters(command=spec.command, args=list(spec.args), env=dict(spec.env) or None)
                        client = await stack.enter_async_context(Client(params, mode="legacy"))
                        listed = await client.list_tools()
                    except Exception:
                        log.exception("MCP server %s failed to start; continuing without it", spec.name)
                        continue
                    if spec.adapter is not None:
                        missing = ADAPTERS[spec.adapter.type].needs - {tool.name for tool in listed.tools}
                        if missing:
                            log.error(
                                "MCP server %s lacks %s needed by adapter %s; skipped",
                                spec.name, ", ".join(sorted(missing)), spec.adapter.type,
                            )
                            continue
                        self._clients[spec.name] = client
                        self._adapter_specs.append(spec)
                        continue
                    self._clients[spec.name] = client
                    for tool in listed.tools:
                        if spec.allow is not None and tool.name not in spec.allow:
                            continue
                        name = public_name(spec.name, tool.name)
                        self._routes[name] = (spec.name, tool.name)
                        self._defs.append(_function(name, tool.description or tool.name, tool.input_schema))
                    offered = sum(1 for route in self._routes.values() if route[0] == spec.name)
                    if offered and spec.instructions:
                        self._notes.append(spec.instructions)
                    log.info("MCP server %s: %d tool(s) offered", spec.name, offered)
                self._ready.set_result(None)
                await self._stop.wait()
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    def _attach_adapter(self, spec: ServerSpec) -> None:
        assert spec.adapter is not None
        kind = ADAPTERS[spec.adapter.type]

        def raw(tool: str, arguments: dict[str, Any], timeout: float, server: str = spec.name) -> str:
            return self.call_raw(server, tool, arguments, timeout)

        try:
            adapter = kind.build(spec.adapter.options, raw)
        except ValueError as exc:
            log.error("MCP server %s: adapter %s not configured (%s); skipped", spec.name, spec.adapter.type, exc)
            return
        adapter.prepare(ADAPTER_PREPARE_SECONDS)
        names: dict[str, str] = {}
        for definition in adapter.definitions():
            fn = definition["function"]
            name = public_name(spec.name, fn["name"])
            names[fn["name"]] = name
            self._adapter_routes[name] = (adapter, fn["name"])
            self._defs.append(_function(name, fn.get("description", name), fn.get("parameters")))
        self._notes.append(spec.instructions or adapter.instructions(names))
        log.info("MCP server %s: adapter %s, %d tool(s) offered", spec.name, spec.adapter.type, len(names))

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._defs)

    def instructions(self) -> str:
        return "\n".join(self._notes)

    def servers(self) -> list[str]:
        return list(self._clients)

    def call(self, name: str, arguments: Mapping[str, Any], timeout: float) -> str:
        if (adapted := self._adapter_routes.get(name)) is not None:
            adapter, local = adapted
            return adapter.call(local, arguments, timeout)
        route = self._routes.get(name)
        if route is None:
            return f"Unknown tool {name}."
        return self.call_raw(route[0], route[1], arguments, timeout)

    def call_raw(self, server: str, tool: str, arguments: Mapping[str, Any], timeout: float) -> str:
        """Call a server's tool directly, bypassing the allow-list and adapters."""
        client = self._clients.get(server)
        if client is None:
            return f"Unknown MCP server {server}."
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool, dict(arguments), read_timeout_seconds=timeout), self._loop
        )
        try:
            return _result_text(future.result(timeout=timeout + 1), self._limit)
        except Exception as exc:  # any transport or server failure becomes a tool error the model can speak
            future.cancel()
            log.warning("MCP tool %s.%s failed: %s", server, tool, exc)
            return f"Tool {tool} failed: {type(exc).__name__}."

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
