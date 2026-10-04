"""Estimate one day's OpenRouter bill for the Alexa skill.

All token counts are assumptions, not measurements. Prices are OpenRouter
per-provider prices (USD per 1M tokens) read from
https://openrouter.ai/api/v1/models/<id>/endpoints on 2026-10-02, at the
first-party provider we would pin (not the cheapest, possibly fp4, host).
Prompt caching is NOT modelled, so input costs are an upper bound.
Edit the constants and re-run:

    python3 scripts/cost_model.py          # 150 reasoning tokens per call
    python3 scripts/cost_model.py 600      # pessimistic: verbose reasoning
"""

import sys
from dataclasses import dataclass

QUESTIONS_PER_DAY = 15
QUESTION_TOKENS = 40          # up to ~30 spoken words
ANSWER_TOKENS = 80            # about two spoken sentences
SYSTEM_TOKENS = 400           # persona + voice rules + "may be mis-heard" note
TOOL_DEF_TOKENS = 1300        # curated tool definitions, resent every call
TOOL_CALL_TOKENS = 40         # the model's tool-call arguments
TOOL_RESULT_TOKENS = 2000     # e.g. 3 capped web-search results
REASONING_TOKENS = 150        # hidden thinking per call, reasoning models only
SEARCH_FEE_USD = 0.001        # openrouter:web_search with engine=parallel


@dataclass(frozen=True)
class Model:
    name: str
    input_per_m: float
    output_per_m: float
    reasoning: bool


MODELS = (
    # Earlier shortlist, kept for reference.
    Model("gpt-oss-120b (Groq)", 0.15, 0.60, True),
    Model("gemini-3.1-flash-lite", 0.25, 1.50, True),
    # Smarter candidates (first-party provider pinned).
    Model("gpt-6-luna (OpenAI)", 0.10, 0.50, True),
    Model("qwen3.8-flash (Alibaba)", 0.15, 0.47, True),
    Model("glm-5.3-flash (Z.AI)", 0.15, 0.50, True),
    Model("deepseek-v4.1-flash (DeepSeek)", 0.15, 0.60, True),
    Model("deepseek-v4-pro-0813 (DeepSeek)", 0.66, 1.98, True),
    Model("gemini-3.8-flash", 0.75, 3.75, True),
    Model("qwen3.8-max-0902 (Alibaba)", 2.00, 6.00, True),
)


def day_cost(model: Model, tool_every: int | None,
             reasoning_tokens: int = REASONING_TOKENS) -> tuple[int, int, float]:
    """Return (input tokens, output tokens, USD) for one day.

    tool_every=None: no question uses a tool; 1: every question; 2: every other one.
    """
    think = reasoning_tokens if model.reasoning else 0
    tokens_in = tokens_out = 0
    fees = 0.0
    for turn in range(QUESTIONS_PER_DAY):
        history = turn * (QUESTION_TOKENS + ANSWER_TOKENS)  # whole session resent
        prompt = SYSTEM_TOKENS + TOOL_DEF_TOKENS + history + QUESTION_TOKENS
        if tool_every and turn % tool_every == 0:
            # Call 1 decides on the tool; call 2 reads the result and answers.
            tokens_in += prompt + (prompt + TOOL_CALL_TOKENS + TOOL_RESULT_TOKENS)
            tokens_out += (TOOL_CALL_TOKENS + think) + (ANSWER_TOKENS + think)
            fees += SEARCH_FEE_USD
        else:
            tokens_in += prompt
            tokens_out += ANSWER_TOKENS + think
    usd = tokens_in * model.input_per_m / 1e6 + tokens_out * model.output_per_m / 1e6 + fees
    return tokens_in, tokens_out, usd


def main() -> None:
    reasoning_tokens = int(sys.argv[1]) if len(sys.argv) > 1 else REASONING_TOKENS
    print(f"reasoning tokens per call: {reasoning_tokens}")
    for label, every in (("no tools", None), ("half use a tool", 2), ("every question uses a tool", 1)):
        print(f"== {label}")
        for model in MODELS:
            tokens_in, tokens_out, usd = day_cost(model, every, reasoning_tokens)
            print(f"  {model.name:32s} in {tokens_in:6d} out {tokens_out:5d}  "
                  f"${usd:.4f}/day  ${usd * 30:.2f}/month")


if __name__ == "__main__":
    main()
