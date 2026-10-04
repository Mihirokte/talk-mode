"""Text shaping for voice. Pure functions only."""

from __future__ import annotations

import re
from enum import StrEnum

# Alexa's PlainText outputSpeech limit is 8,000 characters; spoken answers
# should be far shorter, so trim long ones at a sentence boundary.
MAX_SPOKEN_CHARS = 1500

_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)[^)]+\)")
_URL = re.compile(r"\(?https?://\S+\)?")
_CITATION = re.compile(r"\[(?:\d+(?:[,\s-]+\d+)*|source[^\]]*|citation[^\]]*)\]", re.IGNORECASE)
_MD_MARKS = re.compile(r"(\*\*|__|`+|^#{1,6}\s*|^\s*[-*+]\s+|^\s*\d+\.\s+)", re.MULTILINE)
_SPACES = re.compile(r"\s+")


def to_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Strip markdown, links and citation markers, then cap the length."""
    out = _MD_LINK.sub(r"\1", text)
    out = _URL.sub("", out)
    out = _CITATION.sub("", out)
    out = _MD_MARKS.sub("", out)
    out = out.replace("*", "")
    out = _SPACES.sub(" ", out).strip()
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    if len(out) <= limit:
        return out
    cut = out[:limit]
    stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[: stop + 1] if stop > limit // 2 else cut.rstrip() + "."


class Command(StrEnum):
    STOP = "stop"
    GO_ON = "go_on"
    QUESTION = "question"


_STOP_PHRASES = frozenset(
    {
        "stop", "cancel", "exit", "quit", "goodbye", "good bye", "bye", "bye bye",
        "that's all", "that is all", "that's it", "nothing", "no", "no thanks",
        "no thank you", "never mind", "nevermind", "shut up", "i'm done", "i am done",
    }
)
_GO_ON_PHRASES = frozenset(
    {
        "go on", "continue", "go ahead", "yes", "yeah", "okay", "ok", "tell me",
        "what is it", "what did you find", "tell me the answer", "and", "carry on",
    }
)


def normalize(utterance: str) -> str:
    return _SPACES.sub(" ", re.sub(r"[^\w\s']", " ", utterance.lower())).strip()


def classify(utterance: str) -> Command:
    """Decide whether a free-form utterance is a command or a real question.

    While a slot is being elicited, Alexa may put "stop" or "go on" into the
    query slot instead of routing to the built-in intent, so check here too.
    """
    text = normalize(utterance)
    if text in _STOP_PHRASES:
        return Command.STOP
    if text in _GO_ON_PHRASES:
        return Command.GO_ON
    return Command.QUESTION
