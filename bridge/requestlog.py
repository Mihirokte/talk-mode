"""Per-request cost records, one JSON object per line (data/requests.jsonl locally).

One {"kind": "turn", ...} record per spoken turn: outcome, seconds, question,
answer, and the OpenRouter requests behind it, each with the cost OpenRouter
reported in its response (`usage.cost`, split into model and search).

`scripts/costs.py` turns the file into a per-request price table. On Lambda
there is no file; the same figures are in the CloudWatch log lines.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("bridge.requestlog")


class RequestLog(Protocol):
    def write(self, record: Mapping[str, Any]) -> None: ...


class NoRequestLog:
    def write(self, record: Mapping[str, Any]) -> None:
        return None


class JsonlRequestLog:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps({"ts": round(time.time(), 3), **record}, ensure_ascii=False)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            # A full disk must not cost the user their answer; the log line still has the figures.
            log.warning("could not write %s: %s", self.path, exc)
