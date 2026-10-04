# Design notes

## Constraints

- **Alexa's time limit.** Alexa waits about 8 s for a response. A progressive response ("One moment") doesn't extend that. Anything slower is finished in the background and delivered on "go on".
- **Free-form speech.** A custom skill has no true catch-all slot. The skill elicits an `AMAZON.SearchQuery` slot after "What's your question?", so whatever you say next arrives as text. Alexa does the speech recognition, so the prompt tells the model the words may be misheard.
- **Cost.** Every request resends the system prompt, the day's conversation and every tool definition. Short answers, a small tool list and prompt caching keep a question to about $0.0001. A web search adds about $0.001.

## Choices

- **One wallet: OpenRouter.** It uses the OpenAI-compatible chat API, with built-in web search, and every response reports its exact cost in `usage.cost`. That cost feeds the spend cap and the logs, so there's no polling of balances.
- **Model: `deepseek/deepseek-v4.1-flash` on DeepInfra.** It was chosen for speed with tools: it answered in a median of 2.35 s in testing, against 2.60 s for GPT-6 Luna and 3.20 s for GLM 5.3 Flash, all at about $0.0001 a question. Any OpenRouter model works through `BRIDGE_MODEL`. Pin a provider with `BRIDGE_PROVIDER_ORDER`, or the cheapest host may serve a reduced-precision version.
- **Deadline as one wall-clock limit.** OpenRouter answers immediately and then sends whitespace keep-alives until the model is done, so per-read timeouts never fire. The body is read against a single deadline instead.
- **Spend cap in the backend.** The cap is checked before every call, and each call is charged with its reported cost. A call abandoned at the deadline is charged a high estimate, so the total errs high.
- **Hosting: your own Lambda, not an Alexa-hosted skill.** Alexa-hosted skills pin Python 3.8 and have no secrets store, and their function permissions are Amazon's. The background "go on" path needs the function to invoke itself.

## MCP

- **The backend is the MCP client.** Each stdio server starts once and stays open, and its tools are offered as `<server>__<tool>`. Servers come from config, so adding one needs no code.
- **Adapters for voice.** Raw tools designed for chat clients can be too broad for a model listening through a microphone. Simplenote's `update_note` replaces a whole note, so a misheard or partial rewrite would erase it. An adapter offers a few narrow tools and does the risky part in code: `simplenote_tasks` fetches the note, inserts one line and refuses any write that changes anything else.
- **Stale reads.** simplenote-mcp caches reads for 60 s. The adapter remembers what it last wrote and ignores a cached copy that predates its own write, so two quick adds both survive.

## Cost at 15 questions a day (list prices, 30 days)

| Model | No tools | Half use a tool | Every question uses a tool |
|---|---|---|---|
| deepseek-v4.1-flash | $0.24 | $0.67 | $1.05 |
| gpt-6-luna | $0.17 | $0.54 | $0.87 |

These are from `scripts/cost_model.py` at the list prices it holds; edit its assumptions at the top and rerun it for your own use. AWS adds about $0 on the free tiers. OpenRouter adds 5.5% (minimum $0.80) when you buy credit by card.
