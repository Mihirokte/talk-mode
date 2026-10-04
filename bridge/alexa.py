"""Alexa request parsing and response building. Pure functions only.

The official ASK SDKs have not been released since 2022-2023, so the JSON is
read and written directly. Shapes follow Amazon's request and response
reference for custom skills.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NewType

UserId = NewType("UserId", str)

ASK_INTENT = "AskIntent"
GO_ON_INTENT = "GoOnIntent"
QUERY_SLOT = "query"

STOP_INTENTS = frozenset({"AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"})


@dataclass(frozen=True)
class AlexaContext:
    request_id: str
    session_id: str
    user_id: UserId
    application_id: str
    locale: str
    timestamp: str
    api_endpoint: str | None
    api_access_token: str | None


@dataclass(frozen=True)
class Launch:
    pass


@dataclass(frozen=True)
class Intent:
    name: str
    slots: Mapping[str, str | None]
    dialog_state: str | None


@dataclass(frozen=True)
class SessionEnded:
    reason: str
    error: Mapping[str, Any] | None


@dataclass(frozen=True)
class Unsupported:
    request_type: str


AlexaRequest = Launch | Intent | SessionEnded | Unsupported


class MalformedRequest(ValueError):
    pass


def parse(envelope: Mapping[str, Any]) -> tuple[AlexaContext, AlexaRequest]:
    try:
        request = envelope["request"]
        system = envelope["context"]["System"]
    except (KeyError, TypeError) as exc:
        raise MalformedRequest(f"missing field: {exc}") from exc

    session = envelope.get("session") or {}
    application_id = (
        system.get("application", {}).get("applicationId")
        or session.get("application", {}).get("applicationId")
        or ""
    )
    user_id = system.get("user", {}).get("userId") or session.get("user", {}).get("userId") or ""
    ctx = AlexaContext(
        request_id=request.get("requestId", ""),
        session_id=session.get("sessionId", ""),
        user_id=UserId(user_id),
        application_id=application_id,
        locale=request.get("locale", ""),
        timestamp=request.get("timestamp", ""),
        api_endpoint=system.get("apiEndpoint"),
        api_access_token=system.get("apiAccessToken"),
    )

    kind = request.get("type", "")
    if kind == "LaunchRequest":
        return ctx, Launch()
    if kind == "IntentRequest":
        intent = request.get("intent") or {}
        slots = {
            name: (slot or {}).get("value")
            for name, slot in (intent.get("slots") or {}).items()
        }
        return ctx, Intent(
            name=intent.get("name", ""),
            slots=slots,
            dialog_state=request.get("dialogState"),
        )
    if kind == "SessionEndedRequest":
        return ctx, SessionEnded(reason=request.get("reason", ""), error=request.get("error"))
    return ctx, Unsupported(request_type=kind)


def _speech(text: str) -> dict[str, str]:
    # PlainText avoids SSML escaping problems with model output.
    return {"type": "PlainText", "text": text}


def _envelope(response: dict[str, Any]) -> dict[str, Any]:
    return {"version": "1.0", "sessionAttributes": {}, "response": response}


def listen(text: str, reprompt: str) -> dict[str, Any]:
    """Speak `text`, then keep the mic open for the next free-form question.

    Uses Dialog.ElicitSlot on AskIntent.query, so whatever the user says next
    fills the slot with no carrier phrase. Valid from LaunchRequest and any
    intent through intent chaining, because AskIntent has a dialog model.
    """
    return _envelope(
        {
            "outputSpeech": _speech(text),
            "reprompt": {"outputSpeech": _speech(reprompt)},
            "directives": [
                {
                    "type": "Dialog.ElicitSlot",
                    "slotToElicit": QUERY_SLOT,
                    "updatedIntent": {
                        "name": ASK_INTENT,
                        "confirmationStatus": "NONE",
                        "slots": {QUERY_SLOT: {"name": QUERY_SLOT, "confirmationStatus": "NONE"}},
                    },
                }
            ],
            "shouldEndSession": False,
        }
    )


def end(text: str) -> dict[str, Any]:
    return _envelope({"outputSpeech": _speech(text), "shouldEndSession": True})


def empty() -> dict[str, Any]:
    """Response to SessionEndedRequest; Alexa ignores any speech in it."""
    return _envelope({})
