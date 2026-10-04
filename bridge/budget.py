"""Spend cap for paid model calls.

Every OpenRouter response reports what it was billed (`usage.cost`). The meter
adds that to a running total kept in the Store (SQLite on the Mac, DynamoDB on
AWS), so the total survives restarts and is shared by the server and by the
scripts that use the same data directory. Once the total reaches the cap, new
calls are refused before anything is sent.

The cap is checked before a call and the charge is only known after it, so
calls already in flight can carry the total past the cap. Set the cap below
the real limit by at least one worst-case call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NewType, Protocol

Usd = NewType("Usd", float)

# Charged when a request reached OpenRouter but its cost could not be read:
# the answer was abandoned at the deadline (OpenRouter still bills the work)
# or the response carried no usage block. Deliberately above one typical call
# (about $0.0003 to $0.002 on the shortlisted models).
UNKNOWN_CALL_CHARGE = Usd(0.003)


class SpendLedger(Protocol):
    def spent_usd(self) -> float: ...
    def add_spend(self, usd: float) -> float: ...


class BudgetExceeded(RuntimeError):
    """The spend cap is reached, so no paid call was made."""


def billed_cost(usage: Mapping[str, Any]) -> Usd | None:
    """The amount OpenRouter charged for one response, or None if it is missing."""
    cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return Usd(max(0.0, float(cost)))


@dataclass(frozen=True)
class SpendMeter:
    ledger: SpendLedger
    cap: Usd  # 0 or less disables the cap; spend is still recorded

    def check(self) -> None:
        if self.cap <= 0:
            return
        spent = self.ledger.spent_usd()
        if spent >= self.cap:
            raise BudgetExceeded(f"spend cap reached: ${spent:.4f} of ${self.cap:.4f}")

    def charge(self, usd: Usd) -> float:
        """Add `usd` to the running total and return the new total."""
        if usd <= 0:
            return self.ledger.spent_usd()
        return self.ledger.add_spend(usd)
