"""AWS Lambda entry point. Alexa invokes the function directly (no API Gateway).

Two event shapes arrive here:
  * an Alexa request envelope, answered by `skill.handle`;
  * {"bridge_job": ...}, an async self-invocation that finishes an answer
    which missed the deadline, so "go on" can deliver it.

The in-flight answer thread cannot survive the handler returning (Lambda
freezes the sandbox), so a deferred question is asked again by the worker.
That doubles the token cost of the rare slow question, not of normal ones.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import boto3

from .config import load_settings
from .llm import Answer
from .mcp import load_toolbox
from .skill import Job, finish_job, handle
from .wiring import build_deps, build_http, build_request_log, build_store

logging.getLogger().setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bridge.lambda")


def _resolve_env() -> dict[str, str]:
    env = dict(os.environ)
    param = env.get("OPENROUTER_API_KEY_PARAM")
    if not env.get("OPENROUTER_API_KEY") and param:
        resp = boto3.client("ssm").get_parameter(Name=param, WithDecryption=True)
        env["OPENROUTER_API_KEY"] = resp["Parameter"]["Value"]
    return env


class LambdaDeferrer:
    def __init__(self, function_name: str) -> None:
        self._function = function_name
        self._client = boto3.client("lambda")

    def defer(self, job: Job, running: Future[Answer]) -> None:
        self._client.invoke(
            FunctionName=self._function,
            InvocationType="Event",
            Payload=json.dumps(job.to_event()).encode(),
        )


# Cold-start initialisation, reused by warm invocations.
settings = load_settings(_resolve_env(), base_dir=Path(__file__).resolve().parent.parent)
http = build_http()
store = build_store(settings)
request_log = build_request_log(settings)
tools = load_toolbox(settings.mcp_config_path, settings.max_tool_result_chars)
deps = build_deps(
    settings, http, store, tools, LambdaDeferrer(os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")), request_log
)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if "bridge_job" in event:
        job = Job.from_event(event)
        try:
            outcome: Answer | BaseException = deps.answerer.answer(job.messages, settings.background_timeout_seconds)
        except Exception as exc:
            outcome = exc
        finish_job(job, outcome, store, settings, request_log)
        return {"ok": True}

    received_at = time.monotonic()
    response = handle(event, deps, received_at)
    log.info("request handled in %.2fs", time.monotonic() - received_at)
    return response
