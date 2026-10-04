"""Per-request price and token accounting, read from OpenRouter's own figures.

Every OpenRouter response carries `usage.cost`: the USD amount charged for that
one request, search fees included, and `cost_details.upstream_inference_cost`,
the model host's share of it. So each question's price is known exactly when it
is answered, with no need to diff the account balance (which lags by minutes).

Checked on 2026-10-05: three requests reported $0.00135, $0.00142 and $0.00005,
and the key's usage on OpenRouter rose by exactly their sum ($0.0088 -> $0.0116).
OpenRouter's per-generation record (GET /api/v1/generation) is NOT a usable
cross-check for requests that use server tools: it showed $0 and 0 tokens for
them. The response's `provider` field also read "OpenAI" on those requests
while the generation record said DeepInfra, so it is not logged.

Pure functions over response JSON only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .budget import Usd, billed_cost


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _usd(value: Any) -> Usd | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return Usd(float(value))


def money(usd: float | None) -> str:
    return "unknown" if usd is None else f"${usd:.5f}"


@dataclass(frozen=True)
class CallUsage:
    """One OpenRouter request: what it used and what it was billed."""

    generation_id: str
    seconds: float
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    web_searches: int = 0
    cost: Usd | None = None  # usage.cost: what OpenRouter charged, search fees included
    model_cost: Usd | None = None  # cost_details.upstream_inference_cost: the model host's share
    # Charged to the spend meter instead when `cost` is unknown (the request was
    # abandoned at the deadline, so its response never arrived).
    estimate: Usd | None = None

    @property
    def tool_cost(self) -> Usd | None:
        """Everything that is not the model's tokens: OpenRouter's search fees."""
        if self.cost is None or self.model_cost is None:
            return None
        return Usd(max(0.0, self.cost - self.model_cost))

    def describe(self) -> str:
        gen = self.generation_id or "no-id"
        if self.cost is None and self.estimate is not None:
            return f"{gen} abandoned after {self.seconds:.2f}s; real cost unknown, counted as {money(self.estimate)}"
        split = f" = model {money(self.model_cost)} + search {money(self.tool_cost)}" if self.tool_cost is not None else ""
        return (
            f"{gen} {self.seconds:.2f}s, tokens in {self.prompt_tokens} (cached {self.cached_tokens})"
            f" out {self.completion_tokens} (thinking {self.reasoning_tokens}), searches {self.web_searches},"
            f" cost {money(self.cost)}{split}"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "seconds": round(self.seconds, 3),
            "tokens_in": self.prompt_tokens,
            "tokens_cached": self.cached_tokens,
            "tokens_out": self.completion_tokens,
            "tokens_thinking": self.reasoning_tokens,
            "searches": self.web_searches,
            "cost": self.cost,
            "model_cost": self.model_cost,
            "search_cost": self.tool_cost,
            "estimate": self.estimate,
        }


def call_usage(data: Mapping[str, Any], seconds: float) -> CallUsage:
    usage = data.get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    # Server-tool counts arrive as server_tool_use_details (observed); the
    # server-tool docs show server_tool_use, so read either.
    server_tools = usage.get("server_tool_use_details") or usage.get("server_tool_use") or {}
    cost_details = usage.get("cost_details") or {}
    return CallUsage(
        generation_id=str(data.get("id") or ""),
        seconds=seconds,
        prompt_tokens=_int(usage.get("prompt_tokens")),
        cached_tokens=_int(prompt_details.get("cached_tokens")),
        completion_tokens=_int(usage.get("completion_tokens")),
        reasoning_tokens=_int(completion_details.get("reasoning_tokens")),
        web_searches=_int(server_tools.get("web_search_requests")),
        cost=billed_cost(usage),
        model_cost=_usd(cost_details.get("upstream_inference_cost")),
    )


@dataclass(frozen=True)
class UsageTotal:
    """All OpenRouter requests made for one answer (more than one only with MCP tools)."""

    calls: tuple[CallUsage, ...] = ()

    def plus(self, call: CallUsage) -> UsageTotal:
        return UsageTotal((*self.calls, call))

    @property
    def cost(self) -> Usd:
        return Usd(sum(c.cost or 0.0 for c in self.calls))

    @property
    def web_searches(self) -> int:
        return sum(c.web_searches for c in self.calls)

    @property
    def prompt_tokens(self) -> int:
        return sum(c.prompt_tokens for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens for c in self.calls)

    @property
    def reasoning_tokens(self) -> int:
        return sum(c.reasoning_tokens for c in self.calls)

    def describe(self) -> str:
        n, s = len(self.calls), self.web_searches
        return (
            f"cost {money(self.cost)} ({n} request{'s' if n != 1 else ''}, {s} search{'es' if s != 1 else ''},"
            f" {self.prompt_tokens} tokens in, {self.completion_tokens} out)"
        )

    def to_record(self) -> dict[str, Any]:
        return {"cost": round(self.cost, 8), "searches": self.web_searches, "calls": [c.to_record() for c in self.calls]}
