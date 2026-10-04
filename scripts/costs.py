"""Price of every request, from data/requests.jsonl (written by the local server).

Each paid turn shows the cost OpenRouter reported in that request's own
response (`usage.cost`), split into the model's tokens and the search fee.
The footer compares the spend ledger with OpenRouter's running total for the
API key, so any request the ledger missed would show up as a difference.

    make costs                                          # today's turns
    uv run --env-file .env python scripts/costs.py --all  # everything in the file
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "requests.jsonl"
LEDGER = ROOT / "data" / "bridge.sqlite3"
ZONE = ZoneInfo(os.environ.get("BRIDGE_TIMEZONE", "UTC"))
PAID = {"answered", "deferred-ready", "discarded"}


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def ledger_total() -> float | None:
    if not LEDGER.exists():
        return None
    with sqlite3.connect(LEDGER) as db:
        row = db.execute("SELECT usd FROM spend WHERE name = 'total'").fetchone()
    return float(row[0]) if row else 0.0


def key_usage() -> float | None:
    """OpenRouter's own running total for this API key (GET /api/v1/key, free)."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        return None
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return float(json.load(resp)["data"]["usage"])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def usd(v: float | None) -> str:
    return "" if v is None else f"${v:.5f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="every record, not just today's")
    args = parser.parse_args()

    turns = [r for r in load(LOG) if r.get("kind") == "turn"]
    if not args.all:
        today = datetime.now(tz=ZONE).date()
        turns = [r for r in turns if datetime.fromtimestamp(r["ts"], tz=ZONE).date() == today]
    if not turns:
        print(f"no turns in {LOG}" + ("" if args.all else " for today (try --all)"))
    else:
        print(f"{'time':8}  {'outcome':14} {'secs':>5} {'srch':>4} {'model':>9} {'search':>9} {'total':>9}  question")
    total = 0.0
    paid = 0
    for t in turns:
        stamp = datetime.fromtimestamp(t["ts"], tz=ZONE).strftime("%H:%M:%S")
        secs = f"{t['seconds']:.2f}" if isinstance(t.get("seconds"), (int, float)) else ""
        question = (t.get("question") or "")[:58]
        if t["outcome"] not in PAID:
            print(f"{stamp:8}  {t['outcome']:14} {secs:>5} {'':>4} {'':>9} {'':>9} {'':>9}  {question}")
            continue
        calls = t.get("calls", [])
        model = sum(c.get("model_cost") or 0.0 for c in calls)
        search = sum(c.get("search_cost") or 0.0 for c in calls)
        cost = float(t.get("cost") or 0.0)
        total += cost
        paid += 1
        row = f"{usd(model):>9} {usd(search):>9} {usd(cost):>9}"
        print(f"{stamp:8}  {t['outcome']:14} {secs:>5} {t.get('searches', 0):>4} {row}  {question}")

    if turns:
        avg = f", average {usd(total / paid)}" if paid else ""
        print(f"\n{paid} paid turn(s), {usd(total)} in total{avg}")
    ledger, account = ledger_total(), key_usage()
    if ledger is not None:
        print(f"spend ledger, all time (what the cap checks): ${ledger:.5f}")
    if account is not None:
        note = ""
        if ledger is not None:
            diff = account - ledger
            note = "; matches the ledger" if abs(diff) < 5e-6 else f"; difference ${diff:+.5f}"
        print(f"OpenRouter's own total for this key:         ${account:.5f}{note}")
        print("(OpenRouter's key total can trail by a minute or two after a request.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
