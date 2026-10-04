"""Parse mcp_servers.json into immutable server specs.

The file uses the common `mcpServers` shape, with a few optional extras:

    {"mcpServers": {"<name>": {
        "command": "npx", "args": ["-y", "some-mcp@1.2.3"],
        "env": {"API_TOKEN": "${SOME_TOKEN}"},     # ${VAR} is read from the environment (.env)
        "tools": ["search", "get"],                # allow-list; every definition costs tokens
        "adapter": {"type": "simplenote_tasks", "note_id": "${SIMPLENOTE_TASK_NOTE_ID}"},
        "instructions": "How to use these tools by voice.",
        "lambda": {"packages": [...], "command": "python", "args": [...]},   # or false
        "disabled": false}}}

Keep secrets and personal ids in .env and reference them as ${VAR}: then the
JSON holds nothing private. A server whose ${VAR} is unset is skipped, not fatal.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("bridge.mcp")

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingVariable(KeyError):
    pass


@dataclass(frozen=True)
class AdapterSpec:
    type: str
    options: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerSpec:
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    allow: frozenset[str] | None
    instructions: str = ""
    adapter: AdapterSpec | None = None


def substitute(value: str, env: Mapping[str, str]) -> str:
    """Replace each ${VAR} with env[VAR]; raise MissingVariable if one is unset or empty."""

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        found = env.get(name, "")
        if not found:
            raise MissingVariable(name)
        return found

    return _VAR.sub(one, value)


def _strings(raw: Any, env: Mapping[str, str]) -> dict[str, str]:
    return {str(k): substitute(str(v), env) for k, v in dict(raw or {}).items()}


def parse_specs(raw: Mapping[str, Any], env: Mapping[str, str], python: str = sys.executable) -> list[ServerSpec]:
    """Pure: the parsed config plus an environment -> the servers to start."""
    on_lambda = bool(env.get("LAMBDA_TASK_ROOT"))
    specs = []
    for name, cfg in (raw.get("mcpServers") or {}).items():
        if cfg.get("disabled"):
            continue
        if on_lambda and cfg.get("lambda") is False:
            log.info("MCP server %s is local-only; skipped on Lambda", name)
            continue
        run = cfg["lambda"] if on_lambda and isinstance(cfg.get("lambda"), dict) else cfg
        try:
            server_env = _strings(cfg.get("env"), env)
            args = tuple(substitute(str(a), env) for a in run.get("args", ()))
            adapter_raw = cfg.get("adapter")
            adapter = None
            if adapter_raw:
                options = {k: v for k, v in adapter_raw.items() if k != "type"}
                adapter = AdapterSpec(str(adapter_raw["type"]), _strings(options, env))
        except MissingVariable as exc:
            log.warning("MCP server %s skipped: %s is not set (add it to .env)", name, exc.args[0])
            continue
        if on_lambda and run is not cfg:
            server_env.setdefault("PYTHONPATH", env["LAMBDA_TASK_ROOT"])
        command = python if run["command"] in ("python", "python3") else str(run["command"])
        allow = cfg.get("tools")
        specs.append(
            ServerSpec(
                name=name,
                command=command,
                args=args,
                env=server_env,
                allow=frozenset(allow) if allow else None,
                instructions=str(cfg.get("instructions", "")).strip(),
                adapter=adapter,
            )
        )
    return specs


def read_specs(path: Path, env: Mapping[str, str] | None = None) -> list[ServerSpec]:
    if not path.exists():
        return []
    return parse_specs(json.loads(path.read_text()), os.environ if env is None else env)
