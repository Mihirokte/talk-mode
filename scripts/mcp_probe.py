"""Start the MCP servers in mcp_servers.json the way the backend does, without the model.

Free: no OpenRouter call. Shows what the model would be offered and what a tool returns.

    uv run --extra mcp --env-file .env python scripts/mcp_probe.py                  # list offered tools
    uv run --extra mcp --env-file .env python scripts/mcp_probe.py simplenote__read_tasks
    # call a server's own tool, bypassing allow-lists and adapters (e.g. to find a note id):
    uv run --extra mcp --env-file .env python scripts/mcp_probe.py --raw simplenote search_notes '{"query": "Tasks"}'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.mcp import McpHub
from bridge.mcp.config import read_specs

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tool", nargs="?", help="offered tool name, or server name with --raw")
    parser.add_argument("args", nargs="*", help="[raw tool name] then JSON arguments")
    parser.add_argument("--raw", action="store_true", help="call a server's own tool directly")
    opts = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = Path(os.environ.get("BRIDGE_MCP_CONFIG", ROOT / "mcp_servers.json"))
    specs = read_specs(config)
    if not specs:
        print(f"no MCP servers to start in {config} (copy mcp_servers.example.json?)")
        return 1
    started = time.monotonic()
    hub = McpHub(specs, max_result_chars=6000).start()
    print(f"started {', '.join(hub.servers()) or 'no servers'} in {time.monotonic() - started:.2f} s")
    defs = hub.definitions()
    for d in defs:
        fn = d["function"]
        print(f"  {fn['name']:<32} {len(json.dumps(d)):>5} chars  {fn['description'][:80]!r}")
    total = len(json.dumps(defs))
    # ~4 characters per token is the usual rule of thumb for English JSON.
    print(f"{len(defs)} tool(s), {total} chars of definitions, about {total // 4} tokens added to every request")
    if instructions := hub.instructions():
        print(f"system prompt addition ({len(instructions)} chars):\n  {instructions}")

    if opts.tool:
        t = time.monotonic()
        if opts.raw:
            if not opts.args:
                parser.error("--raw needs: <server> <tool> [json]")
            arguments = json.loads(opts.args[1]) if len(opts.args) > 1 else {}
            result = hub.call_raw(opts.tool, opts.args[0], arguments, timeout=8.0)
        else:
            arguments = json.loads(opts.args[0]) if opts.args else {}
            result = hub.call(opts.tool, arguments, timeout=8.0)
        print(f"\nanswered in {time.monotonic() - t:.2f} s, {len(result)} chars:\n{result[:3000]}")
    hub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
