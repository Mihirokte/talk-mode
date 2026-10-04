"""Local web server: POST /alexa for the skill endpoint (behind a tunnel).

    uv run --extra server --env-file .env uvicorn bridge.server:app --port 8787
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .alexa import MalformedRequest
from .config import load_settings
from .mcp import load_toolbox
from .skill import SkillRejected, ThreadDeferrer, handle
from .verify import AlexaVerifier, RequestNotVerified
from .wiring import build_deps, build_http, build_request_log, build_store

ROOT = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs one line per HTTP call; the bridge's own lines already carry what matters.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bridge.server")

settings = load_settings(os.environ, base_dir=ROOT)
http = build_http()
store = build_store(settings)
request_log = build_request_log(settings)
tools = load_toolbox(settings.mcp_config_path, settings.max_tool_result_chars)
deps = build_deps(settings, http, store, tools, ThreadDeferrer(store, settings, request_log), request_log)
verifier = AlexaVerifier(http) if settings.verify_signatures else None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    ws = settings.web_search
    log.info(
        "model=%s providers=%s reasoning=%s web_search=%s (%s/%s, %d results, %d per question) mcp_tools=%d"
        " deadline=%.1fs filler=%.1fs store=%s spend=$%.4f of cap $%.4f",
        settings.model, ",".join(settings.provider_order) or "default", settings.reasoning.spec,
        ws.enabled, ws.engine, ws.mode, ws.max_results, ws.max_uses, len(tools.definitions()),
        settings.deadline_seconds, settings.filler_after_seconds, settings.store_backend,
        store.spent_usd(), settings.spend_cap_usd,
    )
    if not settings.openrouter_api_key:
        log.warning("OPENROUTER_API_KEY is not set: questions will get the 'could not answer' reply")
    if not settings.skill_id:
        log.warning("BRIDGE_SKILL_ID is not set: requests from any skill are accepted")
    if verifier is None:
        log.warning("BRIDGE_VERIFY_SIGNATURES=false: unsigned requests accepted. Local replay only; never expose this")
    yield
    close = getattr(tools, "close", None)
    if close:
        close()
    http.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "model": settings.model, "verify_signatures": verifier is not None}


@app.post("/alexa")
async def alexa_endpoint(request: Request) -> JSONResponse:
    received_at = time.monotonic()
    body = await request.body()
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if verifier is not None:
        timestamp = str((envelope.get("request") or {}).get("timestamp", ""))
        try:
            await run_in_threadpool(verifier.verify, request.headers, body, timestamp)
        except RequestNotVerified as exc:
            log.warning("rejected unverified request: %s", exc)
            return JSONResponse({"error": "not verified"}, status_code=400)
    try:
        response = await run_in_threadpool(handle, envelope, deps, received_at)
    except (SkillRejected, MalformedRequest) as exc:
        log.warning("rejected request: %s", exc)
        return JSONResponse({"error": "rejected"}, status_code=400)
    log.info("request handled in %.2fs", time.monotonic() - received_at)
    return JSONResponse(response)
