"""Persistence for conversation history and deferred ("say go on") answers.

Two adapters behind one protocol: SQLite for the local Mac, DynamoDB on AWS.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from .alexa import UserId
from .conversation import HistoryKey, Turn, append_turn

HISTORY_TTL_SECONDS = 2 * 24 * 3600
PENDING_TTL_SECONDS = 3600


class PendingStatus(StrEnum):
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Pending:
    job_id: str
    question: str
    status: PendingStatus
    answer: str | None
    created_at: float


class Store(Protocol):
    def load_history(self, key: HistoryKey) -> list[Turn]: ...
    def save_turn(self, key: HistoryKey, turn: Turn, keep: int) -> None: ...
    def put_pending(self, user: UserId, pending: Pending) -> None: ...
    def complete_pending(self, user: UserId, job_id: str, status: PendingStatus, answer: str | None) -> bool: ...
    def get_pending(self, user: UserId) -> Pending | None: ...
    def clear_pending(self, user: UserId) -> None: ...
    # Running total of OpenRouter spend in USD (see budget.py).
    def spent_usd(self) -> float: ...
    def add_spend(self, usd: float) -> float: ...


SPEND_KEY = "total"


class SqliteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS history (key TEXT PRIMARY KEY, turns TEXT NOT NULL, updated REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending (user TEXT PRIMARY KEY, job_id TEXT NOT NULL, question TEXT NOT NULL,"
                " status TEXT NOT NULL, answer TEXT, created REAL NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS spend (name TEXT PRIMARY KEY, usd REAL NOT NULL)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=5)
        try:
            with conn:  # commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    def load_history(self, key: HistoryKey) -> list[Turn]:
        with self._connect() as db:
            row = db.execute("SELECT turns FROM history WHERE key = ?", (key,)).fetchone()
        return [Turn.from_dict(t) for t in json.loads(row[0])] if row else []

    def save_turn(self, key: HistoryKey, turn: Turn, keep: int) -> None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT turns FROM history WHERE key = ?", (key,)).fetchone()
            current = [Turn.from_dict(t) for t in json.loads(row[0])] if row else []
            turns = json.dumps([t.to_dict() for t in append_turn(current, turn, keep)])
            db.execute(
                "INSERT INTO history (key, turns, updated) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET turns = excluded.turns, updated = excluded.updated",
                (key, turns, time.time()),
            )

    def put_pending(self, user: UserId, pending: Pending) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO pending (user, job_id, question, status, answer, created) VALUES (?, ?, ?, ?, ?, ?)",
                (user, pending.job_id, pending.question, pending.status, pending.answer, pending.created_at),
            )

    def complete_pending(self, user: UserId, job_id: str, status: PendingStatus, answer: str | None) -> bool:
        # Only the job that is still current may complete; a newer question wins.
        with self._connect() as db:
            cur = db.execute(
                "UPDATE pending SET status = ?, answer = ? WHERE user = ? AND job_id = ?",
                (status, answer, user, job_id),
            )
            return cur.rowcount > 0

    def get_pending(self, user: UserId) -> Pending | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT job_id, question, status, answer, created FROM pending WHERE user = ?", (user,)
            ).fetchone()
        if not row or time.time() - row[4] > PENDING_TTL_SECONDS:
            return None
        return Pending(job_id=row[0], question=row[1], status=PendingStatus(row[2]), answer=row[3], created_at=row[4])

    def clear_pending(self, user: UserId) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM pending WHERE user = ?", (user,))

    def spent_usd(self) -> float:
        with self._connect() as db:
            row = db.execute("SELECT usd FROM spend WHERE name = ?", (SPEND_KEY,)).fetchone()
        return float(row[0]) if row else 0.0

    def add_spend(self, usd: float) -> float:
        with self._connect() as db:
            db.execute(
                "INSERT INTO spend (name, usd) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET usd = usd + excluded.usd",
                (SPEND_KEY, usd),
            )
            row = db.execute("SELECT usd FROM spend WHERE name = ?", (SPEND_KEY,)).fetchone()
        return float(row[0])


class DynamoStore:
    """Single-table layout: pk "hist#<user>#<date>" and "pending#<user>", TTL on expires_at."""

    def __init__(self, table: str, client: Any | None = None) -> None:
        if client is None:
            import boto3  # present in the Lambda runtime

            client = boto3.client("dynamodb")
        self._db = client
        self._table = table

    def load_history(self, key: HistoryKey) -> list[Turn]:
        item = self._db.get_item(TableName=self._table, Key={"pk": {"S": f"hist#{key}"}}).get("Item")
        return [Turn.from_dict(t) for t in json.loads(item["turns"]["S"])] if item else []

    def save_turn(self, key: HistoryKey, turn: Turn, keep: int) -> None:
        turns = append_turn(self.load_history(key), turn, keep)
        self._db.put_item(
            TableName=self._table,
            Item={
                "pk": {"S": f"hist#{key}"},
                "turns": {"S": json.dumps([t.to_dict() for t in turns])},
                "expires_at": {"N": str(int(time.time()) + HISTORY_TTL_SECONDS)},
            },
        )

    def put_pending(self, user: UserId, pending: Pending) -> None:
        item: dict[str, Any] = {
            "pk": {"S": f"pending#{user}"},
            "job_id": {"S": pending.job_id},
            "question": {"S": pending.question},
            "status": {"S": pending.status},
            "created_at": {"N": repr(pending.created_at)},
            "expires_at": {"N": str(int(time.time()) + PENDING_TTL_SECONDS)},
        }
        if pending.answer is not None:
            item["answer"] = {"S": pending.answer}
        self._db.put_item(TableName=self._table, Item=item)

    def complete_pending(self, user: UserId, job_id: str, status: PendingStatus, answer: str | None) -> bool:
        try:
            self._db.update_item(
                TableName=self._table,
                Key={"pk": {"S": f"pending#{user}"}},
                UpdateExpression="SET #s = :s, answer = :a",
                ConditionExpression="job_id = :j",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": {"S": status},
                    ":a": {"S": answer or ""},
                    ":j": {"S": job_id},
                },
            )
        except self._db.exceptions.ConditionalCheckFailedException:
            return False  # a newer question replaced this job; its answer is no longer wanted
        return True

    def get_pending(self, user: UserId) -> Pending | None:
        item = self._db.get_item(
            TableName=self._table, Key={"pk": {"S": f"pending#{user}"}}, ConsistentRead=True
        ).get("Item")
        if not item:
            return None
        created = float(item["created_at"]["N"])
        if time.time() - created > PENDING_TTL_SECONDS:
            return None
        answer = item.get("answer", {}).get("S")
        return Pending(
            job_id=item["job_id"]["S"],
            question=item["question"]["S"],
            status=PendingStatus(item["status"]["S"]),
            answer=answer or None,
            created_at=created,
        )

    def clear_pending(self, user: UserId) -> None:
        self._db.delete_item(TableName=self._table, Key={"pk": {"S": f"pending#{user}"}})

    def spent_usd(self) -> float:
        item = self._db.get_item(
            TableName=self._table, Key={"pk": {"S": f"spend#{SPEND_KEY}"}}, ConsistentRead=True
        ).get("Item")
        return float(item["usd"]["N"]) if item else 0.0

    def add_spend(self, usd: float) -> float:
        # ADD is atomic, so concurrent Lambda invocations cannot lose a charge.
        resp = self._db.update_item(
            TableName=self._table,
            Key={"pk": {"S": f"spend#{SPEND_KEY}"}},
            UpdateExpression="ADD usd :u",
            ExpressionAttributeValues={":u": {"N": repr(float(usd))}},
            ReturnValues="UPDATED_NEW",
        )
        return float(resp["Attributes"]["usd"]["N"])
