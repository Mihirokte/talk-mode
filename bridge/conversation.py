"""Conversation memory and prompt building. Pure functions only."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NewType

from .alexa import UserId

HistoryKey = NewType("HistoryKey", str)


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str
    at: str  # ISO-8601 with offset

    def to_dict(self) -> dict[str, str]:
        return {"q": self.question, "a": self.answer, "at": self.at}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Turn:
        return Turn(question=str(raw.get("q", "")), answer=str(raw.get("a", "")), at=str(raw.get("at", "")))


def history_key(user_id: UserId, now: datetime) -> HistoryKey:
    """One conversation per user per local calendar day."""
    return HistoryKey(f"{user_id}#{now.date().isoformat()}")


def system_prompt(now: datetime, locale: str, location: str = "", tool_instructions: str = "") -> str:
    stamp = now.strftime("%A %d %B %Y, %I:%M %p %Z").replace(" 0", " ")
    where = f" The user is in {location}." if location else ""
    extra = f"\n{tool_instructions}" if tool_instructions else ""
    return (
        "You are Talk Mode, a voice assistant answering through an Amazon Echo. "
        f"It is {stamp}.{where} Their Alexa locale is {locale or 'unknown'}.\n"
        "The user's words come from speech recognition, so they may contain misheard or "
        "missing words. Work out what they most likely meant from the sound of the words "
        "and the conversation so far; ask a short clarifying question only if you truly "
        "cannot tell.\n"
        "Your reply is spoken aloud. Answer in one to three short sentences unless the user "
        "asks for more detail. Lead with the answer. Use plain spoken English: no markdown, "
        "lists, headings, emojis, URLs or citation marks. Say numbers, units and symbols "
        "the way a person would read them.\n"
        "Use web search only when the question needs current or specific facts you are "
        "unsure of. Search once, with one specific query (for news, include today's date), "
        "and answer from what it returns. When the user asks for more on something you "
        "just answered, build on that answer and search again only for a fact you still "
        "need. Never invent facts; if you do not know, say so briefly."
        f"{extra}"
    )


def build_messages(
    system: str, history: Sequence[Turn], question: str, max_turns: int
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for turn in history[-max_turns:] if max_turns > 0 else ():
        messages.append({"role": "user", "content": turn.question})
        messages.append({"role": "assistant", "content": turn.answer})
    messages.append({"role": "user", "content": question})
    return messages


def append_turn(history: Sequence[Turn], turn: Turn, keep: int) -> list[Turn]:
    return [*history, turn][-keep:] if keep > 0 else []
