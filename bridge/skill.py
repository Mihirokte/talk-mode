"""Skill orchestration: turns one Alexa request into one Alexa response.

The decision logic is plain functions over the parsed request; the I/O it
needs (store, model, progressive speech, background work) comes in through
`Deps`, so the same code runs in the local FastAPI server and in Lambda.

Deadline handling: the answer is computed on a worker thread. If it is not
ready `deadline_seconds` after the request arrived, the skill says "say go
on", records a pending job, and hands the work to the `Deferrer`, which
finishes it in the background. "Go on" then waits for the pending answer.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol

from . import alexa
from .alexa import AlexaContext, Intent, Launch, SessionEnded, UserId
from .budget import BudgetExceeded
from .config import Settings
from .conversation import HistoryKey, Turn, build_messages, history_key, system_prompt
from .llm import Answer
from .requestlog import NoRequestLog, RequestLog
from .speech import Command, classify, to_speech
from .store import Pending, PendingStatus, Store

log = logging.getLogger("bridge.skill")

GREETING = "Hi, what's your question?"
ASK_AGAIN = "What's your question?"
ANYTHING_ELSE = "Anything else?"
HELP = "Ask me anything, for example, how far away is the moon. Say stop when you're done. What's your question?"
GOODBYE = "Goodbye."
NOT_CAUGHT = "Sorry, I didn't catch that. What's your question?"
STILL_THINKING = "That one needs a little longer. Say go on to hear it."
STILL_THINKING_REPROMPT = "Say go on to hear my answer."
STILL_WORKING = "Still working on it. Say go on again in a moment."
NOTHING_PENDING = "I don't have anything waiting for you. What's your question?"
FAILED = "Sorry, I couldn't get an answer just now. Try asking again."
FILLER = "One moment."
BUDGET_SPENT = "My spending limit is used up, so I can't answer right now. Goodbye."

POLL_SECONDS = 0.3


class Answerer(Protocol):
    def answer(self, messages: list[dict[str, Any]], timeout: float) -> Answer: ...


@dataclass(frozen=True)
class Job:
    job_id: str
    user_id: UserId
    history_key: HistoryKey
    question: str
    asked_at: str
    messages: list[dict[str, Any]]

    def to_event(self) -> dict[str, Any]:
        return {"bridge_job": asdict(self)}

    @staticmethod
    def from_event(event: Mapping[str, Any]) -> Job:
        raw = event["bridge_job"]
        return Job(
            job_id=raw["job_id"],
            user_id=UserId(raw["user_id"]),
            history_key=HistoryKey(raw["history_key"]),
            question=raw["question"],
            asked_at=raw["asked_at"],
            messages=list(raw["messages"]),
        )


class Deferrer(Protocol):
    def defer(self, job: Job, running: Future[Answer]) -> None: ...


def finish_job(
    job: Job, outcome: Answer | BaseException, store: Store, settings: Settings, request_log: RequestLog | None = None
) -> None:
    """Record a background answer so "go on" can deliver it."""
    record = request_log or NoRequestLog()
    base = {"kind": "turn", "job_id": job.job_id, "question": job.question}
    if isinstance(outcome, Answer):
        speech = to_speech(outcome.text)
        priced = {**base, "seconds": round(outcome.elapsed, 3), "answer": speech, **outcome.usage.to_record()}
        if not store.complete_pending(job.user_id, job.job_id, PendingStatus.READY, speech):
            # A newer question superseded this one and the user never heard it,
            # so it must not enter the conversation history either.
            log.info(
                "turn %s discarded after %.2fs (a newer question replaced it); %s",
                job.job_id[:8], outcome.elapsed, outcome.usage.describe(),
            )
            record.write({**priced, "outcome": "discarded"})
            return
        store.save_turn(job.history_key, Turn(job.question, speech, job.asked_at), settings.history_turns)
        log.info(
            "turn %s ready in the background after %.2fs; %s | A: %r",
            job.job_id[:8], outcome.elapsed, outcome.usage.describe(), speech[:160],
        )
        record.write({**priced, "outcome": "deferred-ready"})
    else:
        store.complete_pending(job.user_id, job.job_id, PendingStatus.FAILED, None)
        log.warning("turn %s failed in the background: %s", job.job_id[:8], outcome)
        record.write({**base, "outcome": "deferred-failed", "error": str(outcome)[:300]})


class ThreadDeferrer:
    """Local server: the worker thread simply keeps running and records its result."""

    def __init__(self, store: Store, settings: Settings, request_log: RequestLog | None = None) -> None:
        self._store = store
        self._settings = settings
        self._log = request_log

    def defer(self, job: Job, running: Future[Answer]) -> None:
        def done(f: Future[Answer]) -> None:
            exc = f.exception()
            finish_job(job, exc if exc is not None else f.result(), self._store, self._settings, self._log)

        running.add_done_callback(done)


class SkillRejected(Exception):
    """The request is not for this skill."""


@dataclass(frozen=True)
class Deps:
    settings: Settings
    store: Store
    answerer: Answerer
    deferrer: Deferrer
    executor: Executor
    now: Callable[[], datetime]
    progressive: Callable[[AlexaContext, str], None] | None = None
    request_log: RequestLog = field(default_factory=NoRequestLog)
    # Voice guidance for the MCP tools that actually started (ToolBox.instructions).
    tool_instructions: str = ""

def handle(envelope: Mapping[str, Any], deps: Deps, received_at: float) -> dict[str, Any]:
    ctx, request = alexa.parse(envelope)
    skill_id = deps.settings.skill_id
    if skill_id and ctx.application_id != skill_id:
        raise SkillRejected(f"application id {ctx.application_id!r} does not match")
    deadline = received_at + deps.settings.deadline_seconds

    match request:
        case Launch():
            return alexa.listen(GREETING, ASK_AGAIN)
        case SessionEnded(reason=reason, error=error):
            log.info("session ended: %s %s", reason, error or "")
            return alexa.empty()
        case Intent(name=alexa.ASK_INTENT, slots=slots):
            query = (slots.get(alexa.QUERY_SLOT) or "").strip()
            return _on_utterance(ctx, query, deps, deadline) if query else alexa.listen(ASK_AGAIN, ASK_AGAIN)
        case Intent(name=alexa.GO_ON_INTENT):
            return _serve_pending(ctx.user_id, deps, deadline)
        case Intent(name="AMAZON.HelpIntent"):
            return alexa.listen(HELP, ASK_AGAIN)
        case Intent(name=name) if name in alexa.STOP_INTENTS:
            return alexa.end(GOODBYE)
        case Intent(name="AMAZON.FallbackIntent"):
            return alexa.listen(NOT_CAUGHT, ASK_AGAIN)
        case Intent(name=name):
            log.warning("unhandled intent %s", name)
            return alexa.listen(NOT_CAUGHT, ASK_AGAIN)
        case _:
            return alexa.empty()


def _on_utterance(ctx: AlexaContext, query: str, deps: Deps, deadline: float) -> dict[str, Any]:
    command = classify(query)
    if command is Command.STOP:
        return alexa.end(GOODBYE)
    if command is Command.GO_ON and deps.store.get_pending(ctx.user_id) is not None:
        return _serve_pending(ctx.user_id, deps, deadline)
    return _answer(ctx, query, deps, deadline)


def _answer(ctx: AlexaContext, question: str, deps: Deps, deadline: float) -> dict[str, Any]:
    s = deps.settings
    now = deps.now()
    key = history_key(ctx.user_id, now)
    deps.store.clear_pending(ctx.user_id)  # a new question supersedes any unheard answer
    system = system_prompt(now, ctx.locale, s.user_location, deps.tool_instructions)
    messages = build_messages(system, deps.store.load_history(key), question, s.history_turns)

    running = deps.executor.submit(deps.answerer.answer, messages, s.background_timeout_seconds)
    filler = _start_filler(ctx, deps, deadline)
    received = deadline - s.deadline_seconds
    base = {"kind": "turn", "question": question}
    try:
        answer = running.result(timeout=max(0.0, deadline - time.monotonic()))
    except FutureTimeout:
        job = Job(uuid.uuid4().hex, ctx.user_id, key, question, now.isoformat(), messages)
        deps.store.put_pending(ctx.user_id, Pending(job.job_id, question, PendingStatus.RUNNING, None, time.time()))
        deps.deferrer.defer(job, running)
        waited = time.monotonic() - received
        log.info(
            "turn %s deferred at %.2fs (deadline %.1fs); still running in the background | Q: %r",
            job.job_id[:8], waited, s.deadline_seconds, question[:120],
        )
        deps.request_log.write({**base, "outcome": "deferred", "job_id": job.job_id, "seconds": round(waited, 3)})
        return alexa.listen(STILL_THINKING, STILL_THINKING_REPROMPT)
    except BudgetExceeded as exc:
        log.warning("turn refused, no request sent: %s | Q: %r", exc, question[:120])
        deps.request_log.write({**base, "outcome": "refused", "error": str(exc)})
        return alexa.end(BUDGET_SPENT)
    except Exception as exc:
        log.exception("turn failed | Q: %r", question[:120])
        deps.request_log.write({**base, "outcome": "failed", "error": str(exc)[:300]})
        return alexa.listen(FAILED, ASK_AGAIN)
    finally:
        if filler is not None:
            filler.cancel()

    speech = to_speech(answer.text)
    deps.store.save_turn(key, Turn(question, speech, now.isoformat()), s.history_turns)
    total = time.monotonic() - received
    log.info(
        "turn answered in %.2fs (model %.2fs); %s | Q: %r | A: %r",
        total, answer.elapsed, answer.usage.describe(), question[:120], speech[:160],
    )
    deps.request_log.write(
        {**base, "outcome": "answered", "seconds": round(total, 3), "answer": speech, **answer.usage.to_record()}
    )
    return alexa.listen(speech or FAILED, ANYTHING_ELSE)


def _start_filler(ctx: AlexaContext, deps: Deps, deadline: float) -> threading.Timer | None:
    after = deps.settings.filler_after_seconds
    if deps.progressive is None or after <= 0:
        return None
    started = deadline - deps.settings.deadline_seconds
    delay = max(0.0, started + after - time.monotonic())
    timer = threading.Timer(delay, deps.progressive, args=(ctx, FILLER))
    timer.daemon = True
    timer.start()
    return timer


def _serve_pending(user: UserId, deps: Deps, deadline: float) -> dict[str, Any]:
    pending = deps.store.get_pending(user)
    if pending is None:
        return alexa.listen(NOTHING_PENDING, ASK_AGAIN)
    while pending is not None and pending.status is PendingStatus.RUNNING and time.monotonic() + POLL_SECONDS < deadline:
        time.sleep(POLL_SECONDS)
        pending = deps.store.get_pending(user)
    if pending is None:
        return alexa.listen(NOTHING_PENDING, ASK_AGAIN)
    if pending.status is PendingStatus.RUNNING:
        log.info("turn go-on: %s still running; asked the user to say go on again", pending.job_id[:8])
        return alexa.listen(STILL_WORKING, STILL_THINKING_REPROMPT)
    deps.store.clear_pending(user)
    if pending.status is PendingStatus.FAILED or not pending.answer:
        log.info("turn go-on: %s had failed; said sorry", pending.job_id[:8])
        return alexa.listen(FAILED, ASK_AGAIN)
    log.info("turn go-on: delivered %s (its cost is on its 'ready' line)", pending.job_id[:8])
    deps.request_log.write({"kind": "turn", "outcome": "go-on", "job_id": pending.job_id, "question": pending.question})
    return alexa.listen(pending.answer, ANYTHING_ELSE)
