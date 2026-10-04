"""Print OpenRouter per-provider price, latency and throughput for candidate models.

Reads the public, unauthenticated https://openrouter.ai/api/v1/models/<id>/endpoints
API. Latency/throughput are OpenRouter's own recent measurements, not ours.

    python3 scripts/or_endpoints.py
"""

import json
import sys
import urllib.request

CANDIDATES = sys.argv[1:] or [
    "deepseek/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-pro-0813",
    "qwen/qwen3.8-flash",
    "qwen/qwen3.7-flash",
    "qwen/qwen3.7-plus",
    "qwen/qwen3.8-27b",
    "google/gemini-3.1-flash-lite",
    "google/gemini-3.8-flash",
    "openai/gpt-oss-120b",
    "openai/gpt-5.6-luna",
    "z-ai/glm-5.3-flash",
    "minimax/minimax-m3",
]


def fetch(model_id: str) -> dict:
    url = f"https://openrouter.ai/api/v1/models/{model_id}/endpoints"
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.load(resp)["data"]


def per_m(value: str | None) -> float:
    return float(value or 0) * 1e6


def main() -> None:
    for model_id in CANDIDATES:
        try:
            data = fetch(model_id)
        except Exception as exc:  # report and continue with the next model
            print(f"{model_id}: fetch failed: {exc}")
            continue
        print(f"== {model_id}")
        for ep in data.get("endpoints", []):
            price = ep.get("pricing", {})
            params = ep.get("supported_parameters", [])
            print(
                f"  {ep.get('provider_name', '?'):18s} in {per_m(price.get('prompt')):6.3f} "
                f"out {per_m(price.get('completion')):6.3f} "
                f"cache {per_m(price.get('input_cache_read')):6.3f} "
                f"lat {ep.get('latency_last_30m')} tps {ep.get('throughput_last_30m')} "
                f"up {ep.get('uptime_last_30m')} tools={'tools' in params} "
                f"reason={'reasoning' in params} quant={ep.get('quantization')}"
            )


if __name__ == "__main__":
    main()
