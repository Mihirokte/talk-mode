"""Alexa progressive responses: speak a short filler while the answer is computed.

POST {apiEndpoint}/v1/directives with the request's apiAccessToken. Speech
played this way still counts against Alexa's ~8 s limit for the real answer.
"""

from __future__ import annotations

import logging
from html import escape

import httpx

from .alexa import AlexaContext

log = logging.getLogger("bridge.progressive")


def send_progressive(http: httpx.Client, ctx: AlexaContext, text: str) -> None:
    if not ctx.api_endpoint or not ctx.api_access_token or not ctx.request_id:
        return
    body = {
        "header": {"requestId": ctx.request_id},
        "directive": {"type": "VoicePlayer.Speak", "speech": f"<speak>{escape(text)}</speak>"},
    }
    try:
        resp = http.post(
            f"{ctx.api_endpoint.rstrip('/')}/v1/directives",
            json=body,
            headers={"Authorization": f"Bearer {ctx.api_access_token}"},
            timeout=1.5,
        )
        if resp.status_code >= 300:
            log.warning("progressive response HTTP %s: %s", resp.status_code, resp.text[:200])
        else:
            log.info("said %r while the answer is being worked out", text)
    except httpx.HTTPError as exc:
        log.warning("progressive response failed: %s", type(exc).__name__)
