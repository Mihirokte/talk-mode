"""MCP tools for the model: any stdio MCP server, plus optional voice adapters.

    config.py    parses mcp_servers.json (with ${ENV_VAR} substitution)
    hub.py       starts the servers, offers their tools, routes the model's calls
    adapters/    narrow voice-friendly tools built on top of one server's tools

Adding a server is a config change only (see mcp_servers.example.json). An
adapter is for when the raw tools are too broad or too risky to hand to a model
listening through a microphone; see adapters/__init__.py.
"""

from __future__ import annotations

from pathlib import Path

from .config import read_specs
from .hub import McpHub, NoTools, ToolBox

__all__ = ["McpHub", "NoTools", "ToolBox", "load_toolbox"]


def load_toolbox(config_path: Path, max_result_chars: int) -> ToolBox:
    specs = read_specs(config_path)
    if not specs:
        return NoTools()
    return McpHub(specs, max_result_chars).start()
