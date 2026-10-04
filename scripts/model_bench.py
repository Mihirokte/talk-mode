"""Time candidate models on the same spoken-style questions, inside a spend cap.

Each question goes through the bridge's own client (same system prompt,
reasoning setting and cost tracking as a real Alexa turn), single-turn and
without web search. Spend is charged to the same ledger as the server
(data/bridge.sqlite3), so it counts against BRIDGE_SPEND_CAP_USD, and the run
also stops once it has spent --max-spend itself.

    uv run --env-file .env python scripts/model_bench.py --max-spend 0.01
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.budget import BudgetExceeded, SpendMeter, Usd
from bridge.config import WebSearch, load_settings
from bridge.conversation import build_messages, system_prompt
from bridge.llm import AnswerError, OpenRouterAnswerer
from bridge.mcp import NoTools
from bridge.store import SqliteStore
from bridge.wiring import build_http

# (OpenRouter model id, pinned provider slugs). DeepSeek's own API is filtered
# out by the account's "no training on paid prompts" setting, so DeepInfra.
MODELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek/deepseek-v4.1-flash", ("deepinfra",)),
    ("openai/gpt-6-luna", ("openai",)),
    ("z-ai/glm-5.3-flash", ("z-ai",)),
)

# Phrased the way speech recognition hands them over: lower case, no punctuation.
QUESTIONS: tuple[str, ...] = (
    "why do i get more jet lag flying east than west",
    "how much water should i drink in a day in pune summer",
    "who invented the tele phone and was it really him",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-spend", type=float, default=0.01, help="USD this run may spend (default 0.01)")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds allowed per answer")
    args = parser.parse_args()

    base = load_settings(os.environ, ROOT)
    store = SqliteStore(base.data_dir / "bridge.sqlite3")
    start = store.spent_usd()
    run_cap = start + args.max_spend
    cap = Usd(min(base.spend_cap_usd, run_cap) if base.spend_cap_usd > 0 else run_cap)
    meter = SpendMeter(store, cap)
    http = build_http()
    zone = ZoneInfo(base.timezone)
    print(f"ledger ${start:.4f} before the run; this run stops at ${cap:.4f}\n")

    results: dict[str, list[tuple[float, float]]] = {}
    try:
        for model, providers in MODELS:
            settings = dataclasses.replace(
                base, model=model, provider_order=providers, web_search=WebSearch(enabled=False)
            )
            answerer = OpenRouterAnswerer(settings, http, NoTools(), meter)
            print(f"== {model} via {','.join(providers)} (reasoning={settings.reasoning.spec})")
            for question in QUESTIONS:
                messages = build_messages(system_prompt(datetime.now(zone), "en-IN"), [], question, 0)
                before = store.spent_usd()
                try:
                    answer = answerer.answer(messages, args.timeout)
                except AnswerError as exc:
                    print(f"  FAIL  {question!r}: {exc}")
                    continue
                cost = store.spent_usd() - before
                results.setdefault(model, []).append((answer.elapsed, cost))
                u = answer.usage
                print(
                    f"  {answer.elapsed:5.2f}s ${cost:.5f} in={u.prompt_tokens} out={u.completion_tokens}"
                    f" think={u.reasoning_tokens} [{answer.provider}]\n"
                    f"        Q: {question}\n        A: {answer.text[:220]!r}"
                )
    except BudgetExceeded as exc:
        print(f"\nstopped: {exc}")

    print("\nmodel                          median   max     spent")
    for model, rows in results.items():
        times = [t for t, _ in rows]
        print(f"{model:30s} {statistics.median(times):5.2f}s {max(times):5.2f}s  ${sum(c for _, c in rows):.5f}")
    print(f"\nrun spent ${store.spent_usd() - start:.5f}; ledger now ${store.spent_usd():.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
