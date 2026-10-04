"""Report the OpenRouter key's limit/usage and the account credit balance.

Reads OPENROUTER_API_KEY from the environment (run via `uv run --env-file .env`)
and never prints it.  Usage: uv run --env-file .env python scripts/or_balance.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1"


def _get(path: str, key: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("data", {})
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"}


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2

    info = _get("/key", key)
    if "error" in info:
        print(f"key check failed: {info['error']}")
        return 1
    print(f"key valid    : yes (label {info.get('label')!r})")
    print(f"key limit    : {info.get('limit')}  remaining {info.get('limit_remaining')}")
    print(f"key usage    : ${info.get('usage', 0):.4f}")
    print(f"free tier    : {info.get('is_free_tier')}")

    credits = _get("/credits", key)
    if "error" in credits:
        print(f"credits      : unavailable ({credits['error']})")
    else:
        total = float(credits.get("total_credits", 0))
        used = float(credits.get("total_usage", 0))
        print(f"account      : ${total:.4f} bought, ${used:.4f} used, ${total - used:.4f} left")

    cap = float(os.environ.get("BRIDGE_SPEND_CAP_USD") or 0)
    print(f"local ledger : ${_ledger_total():.4f} recorded, cap ${cap:.4f} (BRIDGE_SPEND_CAP_USD)")
    return 0


def _ledger_total() -> float:
    """The bridge's own running spend total (bridge/budget.py), 0 if none yet."""
    data_dir = Path(os.environ.get("BRIDGE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    path = data_dir / "bridge.sqlite3"
    if not path.exists():
        return 0.0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT usd FROM spend WHERE name = 'total'").fetchone()
    except sqlite3.OperationalError:
        return 0.0  # created before the spend table existed
    finally:
        conn.close()
    return float(row[0]) if row else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
