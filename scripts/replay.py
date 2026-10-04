"""Replay a scripted Alexa conversation against the local server or the deployed Lambda.

Each step is `launch`, `help`, `go-on`, `stop`, `end`, or any other text,
which is sent as the free-form answer to "What's your question?" (AskIntent).

    # local server started with BRIDGE_VERIFY_SIGNATURES=false
    uv run python scripts/replay.py launch "why is the sky blue" stop
    # deployed Lambda (needs AWS credentials)
    uv run --extra deploy python scripts/replay.py --lambda talk-mode launch "what's the news today" stop

Prints what Alexa would say and the latency of each turn. Requests are
unsigned, so a server with signature verification on rejects them (by design).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from typing import Any

DEFAULT_SKILL = "amzn1.ask.skill.00000000-0000-0000-0000-000000000000"


def envelope(request: dict[str, Any], *, new: bool, skill_id: str, user_id: str, session_id: str) -> dict[str, Any]:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    app = {"applicationId": skill_id}
    user = {"userId": user_id}
    return {
        "version": "1.0",
        "session": {"new": new, "sessionId": session_id, "application": app, "user": user, "attributes": {}},
        "context": {
            "System": {
                "application": app,
                "user": user,
                "device": {"deviceId": "replay-device", "supportedInterfaces": {}},
                "apiEndpoint": "https://api.eu.amazonalexa.com",
            }
        },
        "request": {
            **request,
            "requestId": f"amzn1.echo-api.request.{uuid.uuid4()}",
            "timestamp": stamp,
            "locale": "en-US",
        },
    }


def intent(name: str, slots: dict[str, str] | None = None, dialog_state: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "IntentRequest",
        "intent": {
            "name": name,
            "confirmationStatus": "NONE",
            "slots": {k: {"name": k, "value": v, "confirmationStatus": "NONE"} for k, v in (slots or {}).items()},
        },
    }
    if dialog_state:
        body["dialogState"] = dialog_state
    return body


def request_for(step: str) -> dict[str, Any]:
    match step.lower():
        case "launch":
            return {"type": "LaunchRequest"}
        case "help":
            return intent("AMAZON.HelpIntent")
        case "go-on":
            return intent("GoOnIntent")
        case "stop":
            return intent("AMAZON.StopIntent")
        case "end":
            return {"type": "SessionEndedRequest", "reason": "USER_INITIATED"}
        case _:
            return intent("AskIntent", {"query": step}, dialog_state="IN_PROGRESS")


def send_http(url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:300]}


def send_lambda(function: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    import boto3

    resp = boto3.client("lambda").invoke(FunctionName=function, Payload=json.dumps(payload).encode())
    body = json.loads(resp["Payload"].read() or b"{}")
    return (500 if resp.get("FunctionError") else 200), body


def describe(body: dict[str, Any]) -> str:
    response = body.get("response")
    if response is None:
        return json.dumps(body)[:300]
    speech = (response.get("outputSpeech") or {}).get("text", "")
    directives = [d.get("type") for d in response.get("directives", [])]
    ends = response.get("shouldEndSession")
    return f"{speech!r}  [ends={ends} directives={directives}]"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("steps", nargs="+")
    parser.add_argument("--url", default="http://127.0.0.1:8787/alexa")
    parser.add_argument("--lambda", dest="function", help="invoke this Lambda function instead of --url")
    parser.add_argument("--skill-id", default=os.environ.get("BRIDGE_SKILL_ID") or DEFAULT_SKILL)
    parser.add_argument("--user-id", default="amzn1.ask.account.replay-user")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to wait between steps")
    args = parser.parse_args()

    session_id = f"amzn1.echo-api.session.{uuid.uuid4()}"
    failures = 0
    for index, step in enumerate(args.steps):
        payload = envelope(
            request_for(step), new=index == 0, skill_id=args.skill_id, user_id=args.user_id, session_id=session_id
        )
        started = time.monotonic()
        status, body = send_lambda(args.function, payload) if args.function else send_http(args.url, payload)
        elapsed = time.monotonic() - started
        flag = "OK " if status == 200 and elapsed < 8 else "BAD"
        failures += flag == "BAD"
        print(f"{flag} {elapsed:5.2f}s  you: {step!r}\n            alexa: {describe(body)}")
        if args.pause:
            time.sleep(args.pause)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
