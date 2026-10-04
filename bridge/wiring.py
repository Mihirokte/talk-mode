"""Builds the runtime dependencies shared by the local server and the Lambda."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .budget import SpendMeter, Usd
from .config import Settings, StoreBackend
from .llm import OpenRouterAnswerer
from .mcp import ToolBox
from .progressive import send_progressive
from .requestlog import JsonlRequestLog, NoRequestLog, RequestLog
from .skill import Deferrer, Deps
from .store import DynamoStore, SqliteStore, Store


def build_http() -> httpx.Client:
    # One pooled client keeps the TLS connection to OpenRouter warm between turns.
    return httpx.Client(limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120))


def build_store(settings: Settings) -> Store:
    if settings.store_backend is StoreBackend.DYNAMODB:
        if not settings.dynamodb_table:
            raise ValueError("BRIDGE_DYNAMODB_TABLE is required when BRIDGE_STORE=dynamodb")
        return DynamoStore(settings.dynamodb_table)
    return SqliteStore(settings.data_dir / "bridge.sqlite3")


def build_request_log(settings: Settings) -> RequestLog:
    """data/requests.jsonl next to the SQLite store; on Lambda the log lines carry the figures."""
    if settings.store_backend is StoreBackend.SQLITE:
        return JsonlRequestLog(settings.data_dir / "requests.jsonl")
    return NoRequestLog()


def build_deps(
    settings: Settings, http: httpx.Client, store: Store, tools: ToolBox, deferrer: Deferrer, request_log: RequestLog
) -> Deps:
    zone = ZoneInfo(settings.timezone)
    meter = SpendMeter(store, Usd(settings.spend_cap_usd))
    return Deps(
        settings=settings,
        store=store,
        answerer=OpenRouterAnswerer(settings, http, tools, meter),
        deferrer=deferrer,
        executor=ThreadPoolExecutor(max_workers=8, thread_name_prefix="answer"),
        now=lambda: datetime.now(zone),
        progressive=lambda ctx, text: send_progressive(http, ctx, text),
        request_log=request_log,
        tool_instructions=tools.instructions(),
    )
