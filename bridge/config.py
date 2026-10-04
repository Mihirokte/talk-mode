"""Settings, parsed once from the environment into an immutable value.

`load_settings` is pure: it takes a mapping (normally `os.environ`) and returns
`Settings`, so the same parsing serves the local server and the Lambda.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class StoreBackend(StrEnum):
    SQLITE = "sqlite"
    DYNAMODB = "dynamodb"


@dataclass(frozen=True)
class Reasoning:
    """How much hidden thinking the model may do before answering.

    `spec` is one of: "off", "minimal", "low", "medium", "high", or a token
    budget such as "256". Thinking costs output tokens and seconds, and Alexa
    allows about 8 s in total, so keep it small.
    """

    spec: str = "low"

    def request_field(self) -> dict[str, Any]:
        value = self.spec.strip().lower()
        if value in ("", "default"):
            return {}
        if value == "off":
            return {"reasoning": {"enabled": False}}
        if value.isdigit():
            return {"reasoning": {"max_tokens": int(value), "exclude": True}}
        return {"reasoning": {"effort": value, "exclude": True}}


@dataclass(frozen=True)
class WebSearch:
    """OpenRouter's `openrouter:web_search` server tool settings."""

    enabled: bool = True
    engine: str = "parallel"
    # Parallel "turbo" is ~200 ms and $1 per 1,000 searches (English only);
    # the default "basic" mode is $5 per 1,000.
    mode: str = "turbo"
    max_results: int = 3
    # Searches allowed per question. Each extra search costs another model pass
    # (about 1.5 to 2 s), which is what pushed answers past the deadline.
    max_uses: int = 1
    # Characters of page text per result; 0 keeps the engine default (Parallel: 1,500).
    max_characters: int = 0

    def tool(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "engine": self.engine,
            "max_results": self.max_results,
            "max_uses": max(1, self.max_uses),
            "max_total_results": self.max_results * max(1, self.max_uses),
        }
        if self.mode:
            params["mode"] = self.mode
        if self.max_characters > 0:
            params["max_characters"] = self.max_characters
        return {"type": "openrouter:web_search", "parameters": params}


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    model: str
    provider_order: tuple[str, ...]
    allow_provider_fallbacks: bool
    reasoning: Reasoning
    max_output_tokens: int
    web_search: WebSearch
    web_fetch: bool
    max_server_tool_calls: int
    # Seconds from receiving the Alexa request to giving up and saying
    # "say go on". Alexa's hard limit is about 8 s end to end.
    deadline_seconds: float
    # Speak "One moment." through the progressive response API if the answer
    # is not ready after this many seconds. 0 disables it.
    filler_after_seconds: float
    # Upper bound for one answer when it runs in the background after the
    # deadline (local thread or async Lambda worker).
    background_timeout_seconds: float
    history_turns: int
    timezone: str
    # Country or city the model is told the user is in; "" says nothing.
    user_location: str
    skill_id: str
    verify_signatures: bool
    store_backend: StoreBackend
    data_dir: Path
    dynamodb_table: str
    mcp_config_path: Path
    max_tool_result_chars: int
    # Stop paid model calls once recorded OpenRouter spend reaches this many
    # USD (see budget.py). 0 disables the cap; spend is recorded either way.
    spend_cap_usd: float
    app_title: str = "Talk Mode"

    def provider_field(self) -> dict[str, Any]:
        if not self.provider_order:
            return {}
        return {
            "provider": {
                "order": list(self.provider_order),
                "allow_fallbacks": self.allow_provider_fallbacks,
                "require_parameters": True,
            }
        }


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _csv(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_settings(env: Mapping[str, str], base_dir: Path | None = None) -> Settings:
    base = base_dir or Path.cwd()
    get = env.get
    return Settings(
        openrouter_api_key=get("OPENROUTER_API_KEY", "").strip(),
        model=get("BRIDGE_MODEL", "deepseek/deepseek-v4.1-flash"),
        provider_order=_csv(get("BRIDGE_PROVIDER_ORDER"), ("deepinfra",)),
        allow_provider_fallbacks=_bool(get("BRIDGE_PROVIDER_FALLBACKS"), False),
        reasoning=Reasoning(get("BRIDGE_REASONING", "low")),
        max_output_tokens=int(get("BRIDGE_MAX_OUTPUT_TOKENS", "700")),
        web_search=WebSearch(
            enabled=_bool(get("BRIDGE_WEB_SEARCH"), True),
            engine=get("BRIDGE_WEB_SEARCH_ENGINE", "parallel"),
            mode=get("BRIDGE_WEB_SEARCH_MODE", "turbo"),
            max_results=int(get("BRIDGE_WEB_SEARCH_MAX_RESULTS", "3")),
            max_uses=int(get("BRIDGE_WEB_SEARCH_MAX_USES", "1")),
            max_characters=int(get("BRIDGE_WEB_SEARCH_MAX_CHARACTERS", "0") or "0"),
        ),
        web_fetch=_bool(get("BRIDGE_WEB_FETCH"), False),
        max_server_tool_calls=int(get("BRIDGE_MAX_SERVER_TOOL_CALLS", "1")),
        deadline_seconds=float(get("BRIDGE_DEADLINE_SECONDS", "7.0")),
        filler_after_seconds=float(get("BRIDGE_FILLER_AFTER_SECONDS", "2.5")),
        background_timeout_seconds=float(get("BRIDGE_BACKGROUND_TIMEOUT_SECONDS", "25")),
        history_turns=int(get("BRIDGE_HISTORY_TURNS", "15")),
        timezone=get("BRIDGE_TIMEZONE", "UTC"),
        user_location=get("BRIDGE_USER_LOCATION", "").strip(),
        skill_id=get("BRIDGE_SKILL_ID", "").strip(),
        verify_signatures=_bool(get("BRIDGE_VERIFY_SIGNATURES"), True),
        store_backend=StoreBackend(get("BRIDGE_STORE", "sqlite")),
        data_dir=Path(get("BRIDGE_DATA_DIR", str(base / "data"))),
        dynamodb_table=get("BRIDGE_DYNAMODB_TABLE", ""),
        mcp_config_path=Path(get("BRIDGE_MCP_CONFIG", str(base / "mcp_servers.json"))),
        max_tool_result_chars=int(get("BRIDGE_MAX_TOOL_RESULT_CHARS", "6000")),
        spend_cap_usd=float(get("BRIDGE_SPEND_CAP_USD", "0") or "0"),
        app_title=get("BRIDGE_APP_TITLE", "Talk Mode"),
    )
