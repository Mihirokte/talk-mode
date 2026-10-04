"""Voice adapters: a few narrow tools built on top of one MCP server's raw tools.

Use one when the server's own tools are too broad for a model that hears you
through a microphone (misheard words, no screen to confirm on). The model is
offered only the adapter's tools; the raw tools stay private to the adapter.

To add one:
1. Write a module here with an `ADAPTER = AdapterType(...)`: the raw tools it
   needs, and a `build(options, raw_call)` that returns an object with
   `prepare`, `definitions`, `call` and `instructions` (see `Adapter`).
2. Register it in `ADAPTERS` below.
3. Reference it from mcp_servers.json: "adapter": {"type": "<key>", ...options}.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from . import simplenote_tasks

# (raw MCP tool name, arguments, timeout seconds) -> result text
RawCall = Callable[[str, dict[str, Any], float], str]


class Adapter(Protocol):
    def prepare(self, timeout: float) -> None:
        """One-off setup after the server starts (may read from it). Must not raise."""

    def definitions(self) -> list[dict[str, Any]]:
        """OpenAI function-tool definitions, with unprefixed names."""
        ...

    def call(self, name: str, arguments: Mapping[str, Any], timeout: float) -> str: ...

    def instructions(self, public_names: Mapping[str, str]) -> str:
        """Voice guidance for the system prompt; public_names maps local -> offered tool name."""
        ...


@dataclass(frozen=True)
class AdapterType:
    needs: frozenset[str]
    build: Callable[[Mapping[str, str], RawCall], Adapter]


ADAPTERS: dict[str, AdapterType] = {
    "simplenote_tasks": AdapterType(needs=simplenote_tasks.NEEDS, build=simplenote_tasks.build),
}
