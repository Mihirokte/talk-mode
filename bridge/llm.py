"""OpenRouter chat-completions client with a deadline-aware tool loop.

OpenRouter server tools (`openrouter:web_search`, `openrouter:web_fetch`) run
inside OpenRouter, so they cost one request here. MCP tools are function tools:
the model asks, this client runs them and calls the model again.

Every request is logged on one line with its generation id, time, tokens,
searches and the USD cost OpenRouter reports for it (see usage.py).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .budget import UNKNOWN_CALL_CHARGE, SpendMeter
from .config import Settings
from .mcp import ToolBox
from .usage import CallUsage, UsageTotal, call_usage

log = logging.getLogger("bridge.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_FUNCTION_ROUNDS = 3
CONNECT_TIMEOUT = 5.0
# Longest silence tolerated between body chunks. OpenRouter's keep-alives come
# about every 3 s, so this only bounds the overshoot past the wall-clock deadline.
READ_GAP_TIMEOUT = 8.0


class AnswerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answer:
    text: str
    model: str
    provider: str | None
    elapsed: float
    rounds: int
    function_calls: int
    usage: UsageTotal = field(default_factory=UsageTotal)


def build_request(settings: Settings, messages: list[dict[str, Any]], function_tools: list[dict[str, Any]]) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    if settings.web_search.enabled:
        tools.append(settings.web_search.tool())
    if settings.web_fetch:
        tools.append({"type": "openrouter:web_fetch"})
    tools.extend(function_tools)
    body: dict[str, Any] = {
        "model": settings.model,
        "messages": messages,
        "max_tokens": settings.max_output_tokens,
        **settings.reasoning.request_field(),
        **settings.provider_field(),
    }
    if tools:
        body["tools"] = tools
        body["max_tool_calls"] = max(1, settings.max_server_tool_calls)
    return body


class OpenRouterAnswerer:
    def __init__(self, settings: Settings, http: httpx.Client, tools: ToolBox, meter: SpendMeter) -> None:
        self._s = settings
        self._http = http
        self._tools = tools
        self._meter = meter

    def answer(self, messages: list[dict[str, Any]], timeout: float) -> Answer:
        if not self._s.openrouter_api_key:
            raise AnswerError("OPENROUTER_API_KEY is not set")
        started = time.monotonic()
        deadline = started + timeout
        convo = list(messages)
        function_calls = 0
        usage = UsageTotal()
        for round_no in range(1, MAX_FUNCTION_ROUNDS + 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                raise AnswerError("ran out of time in the tool loop")
            # The last round offers no function tools, forcing a spoken answer.
            offer = self._tools.definitions() if round_no <= MAX_FUNCTION_ROUNDS else []
            data, call = self._post(build_request(self._s, convo, offer), remaining)
            usage = usage.plus(call)
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls:
                text = (message.get("content") or "").strip()
                if not text:
                    raise AnswerError(f"empty answer (finish_reason={choice.get('finish_reason')})")
                return Answer(
                    text=text,
                    model=data.get("model", self._s.model),
                    provider=data.get("provider"),
                    elapsed=time.monotonic() - started,
                    rounds=round_no,
                    function_calls=function_calls,
                    usage=usage,
                )
            convo.append(_assistant_echo(message))
            for tool_call in calls:
                function_calls += 1
                fn = tool_call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                budget = max(0.5, min(4.0, deadline - time.monotonic() - 1.0))
                result = self._tools.call(fn.get("name", ""), args, budget)
                log.info("tool %s(%s) -> %d chars", fn.get("name"), json.dumps(args)[:200], len(result))
                convo.append({"role": "tool", "tool_call_id": tool_call.get("id", ""), "content": result})
        raise AnswerError("tool loop did not finish")

    def _post(self, body: dict[str, Any], timeout: float) -> tuple[dict[str, Any], CallUsage]:
        """POST and return the JSON body, giving up `timeout` seconds after the start.

        OpenRouter answers non-streaming requests with 200 headers at once and
        then sends whitespace keep-alives every few seconds until the model is
        done. httpx timeouts bound each read, so those keep-alives would keep a
        request alive indefinitely (measured: 199 s on a 25 s timeout). The body
        is therefore read in chunks against a wall-clock deadline.
        """
        headers = {
            "Authorization": f"Bearer {self._s.openrouter_api_key}",
            "X-Title": self._s.app_title,
        }
        self._meter.check()  # raises BudgetExceeded before anything is sent
        started = time.monotonic()
        deadline = started + timeout
        limits = httpx.Timeout(
            connect=min(timeout, CONNECT_TIMEOUT),
            read=min(timeout, READ_GAP_TIMEOUT),
            write=min(timeout, CONNECT_TIMEOUT),
            pool=min(timeout, CONNECT_TIMEOUT),
        )
        # True once OpenRouter accepted the request (HTTP 200). From then on it
        # bills the work even if we stop reading, so an abandoned call is charged
        # an estimate rather than recorded as free.
        accepted = False
        # Sent in the x-generation-id header before the body, so even an abandoned
        # request can be found on openrouter.ai/activity.
        generation_id = ""
        try:
            with self._http.stream("POST", OPENROUTER_URL, json=body, headers=headers, timeout=limits) as resp:
                status = resp.status_code
                accepted = status == 200
                generation_id = resp.headers.get("x-generation-id", "")
                raw = bytearray()
                for chunk in resp.iter_bytes():
                    raw.extend(chunk)
                    if time.monotonic() > deadline:
                        raise AnswerError(f"OpenRouter gave no answer within {timeout:.1f}s")
        except AnswerError:
            self._abandon(generation_id, started, accepted)
            raise
        except httpx.TimeoutException as exc:
            self._abandon(generation_id, started, accepted)
            raise AnswerError(f"OpenRouter timed out ({type(exc).__name__}) within {timeout:.1f}s") from exc
        except httpx.HTTPError as exc:
            self._abandon(generation_id, started, accepted)
            raise AnswerError(f"OpenRouter request failed: {type(exc).__name__}") from exc
        text = raw.decode("utf-8", errors="replace")
        if status != 200:
            raise AnswerError(f"OpenRouter HTTP {status}: {text.strip()[:300]}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            self._abandon(generation_id, started, accepted)
            raise AnswerError(f"OpenRouter sent unreadable JSON: {text.strip()[:120]!r}") from exc
        call = call_usage(data, time.monotonic() - started)
        if not call.generation_id:
            call = dataclasses.replace(call, generation_id=generation_id)
        call = self._settle(call)
        if "error" in data:
            raise AnswerError(f"OpenRouter error: {json.dumps(data['error'])[:300]}")
        return data, call

    def _settle(self, call: CallUsage) -> CallUsage:
        """Charge a finished request to the spend meter and log its price."""
        if call.cost is None:
            call = dataclasses.replace(call, estimate=UNKNOWN_CALL_CHARGE)
        total = self._meter.charge(call.cost if call.cost is not None else UNKNOWN_CALL_CHARGE)
        log.info("openrouter %s | spend now $%.4f of cap $%.4f", call.describe(), total, self._meter.cap)
        return call

    def _abandon(self, generation_id: str, started: float, accepted: bool) -> None:
        if not accepted:
            return  # rejected before any work (4xx/5xx, connect failure): not billed
        call = CallUsage(generation_id=generation_id, seconds=time.monotonic() - started, estimate=UNKNOWN_CALL_CHARGE)
        total = self._meter.charge(UNKNOWN_CALL_CHARGE)
        log.warning("openrouter %s | spend now $%.4f of cap $%.4f", call.describe(), total, self._meter.cap)


def _assistant_echo(message: dict[str, Any]) -> dict[str, Any]:
    """Return the assistant tool-call turn as the next request expects it.

    Reasoning models need their reasoning_details passed back unchanged
    across tool calls, so keep them.
    """
    echo: dict[str, Any] = {
        "role": "assistant",
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls"),
    }
    if message.get("reasoning_details"):
        echo["reasoning_details"] = message["reasoning_details"]
    return echo
