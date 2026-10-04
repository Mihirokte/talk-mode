"""Adapter "simplenote_tasks": one Simplenote note as a voice task list.

    "adapter": {"type": "simplenote_tasks", "note_id": "${SIMPLENOTE_TASK_NOTE_ID}"}

The model is offered exactly two tools, read_tasks and add_task. It never sees a
note id or the raw Simplenote tools and never rewrites the note itself: the bridge
fetches the note, inserts one bullet line and writes the whole note back, after
checking that the only difference is that one line. No other note can be reached
and no existing line can be lost or changed.

The note's layout is a title line, then optional sections under *Heading* lines,
each a list of "- " bullets. A new task goes at the end of the named section
(default: the last one), before any trailing empty "- " bullet the editor left.

Needs simplenote-mcp in API mode with write mode on (its one-time `setup`).
Everything above TaskNote is pure; TaskNote does its I/O through an injected call.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("bridge.mcp.simplenote_tasks")

READ_TOOL = "read_tasks"
ADD_TOOL = "add_task"
# The two Simplenote MCP tools the adapter calls; the model never sees them.
NEEDS = frozenset({"get_note", "update_note"})
MAX_TASK_CHARS = 200
# simplenote-mcp caches Simperium responses for 60 s (its README), so a read
# right after our own write can return the note as it was before the write.
CACHE_SECONDS = 60.0

_HEADING = re.compile(r"^\*(?P<name>[^*]+)\*\s*$")
_BULLET = re.compile(r"^-(?: (?P<text>.*))?$")


class TaskNoteError(RuntimeError):
    pass


@dataclass(frozen=True)
class Section:
    name: str  # "" for bullets before the first heading
    heading_line: int  # index of the *Heading* line, -1 when there is none
    end_line: int  # one past the section's last line
    tasks: tuple[str, ...]


def _bullet_text(line: str) -> str | None:
    m = _BULLET.match(line.strip())
    return None if m is None else (m.group("text") or "").strip()


def sections(content: str) -> tuple[Section, ...]:
    lines = content.split("\n")
    starts = [(i, m.group("name").strip()) for i, line in enumerate(lines) if (m := _HEADING.match(line.strip()))]
    spans = [(-1, "", 1)] if not starts or starts[0][0] > 1 else []
    spans += [(i, name, i + 1) for i, name in starts]
    out = []
    for n, (heading, name, first) in enumerate(spans):
        end = spans[n + 1][0] if n + 1 < len(spans) else len(lines)
        tasks = tuple(t for line in lines[first:end] if (t := _bullet_text(line)))
        if heading >= 0 or tasks:
            out.append(Section(name, heading, end, tasks))
    return tuple(out)


def section_names(content: str) -> tuple[str, ...]:
    return tuple(s.name for s in sections(content) if s.name)


def spoken_list(content: str) -> str:
    """The tasks as plain text for the model: no markdown, grouped by section."""
    parts = sections(content)
    count = sum(len(s.tasks) for s in parts)
    if count == 0:
        return "The task list is empty."
    lines = [f"{count} task(s)."]
    for s in parts:
        if s.tasks:
            label = s.name or "Tasks"
            lines.append(f"{label}: " + "; ".join(s.tasks) + ".")
    return "\n".join(lines)


def clean_task(raw: object) -> str:
    if not isinstance(raw, str):
        raise TaskNoteError("task must be text")
    text = " ".join(raw.split()).lstrip("-*• ").rstrip(" .")
    if not text:
        raise TaskNoteError("task is empty")
    return text[:MAX_TASK_CHARS]


def _pick(parts: Sequence[Section], wanted: str | None) -> Section:
    if not parts:
        raise TaskNoteError("the note has no task section")
    if wanted:
        key = wanted.strip().lower()
        for s in parts:
            if s.name.lower() == key:
                return s
    return parts[-1]


def with_task(content: str, task: str, section: str | None = None) -> tuple[str, str]:
    """(new content, section used) with one "- task" line inserted. Pure."""
    lines = content.split("\n")
    target = _pick(sections(content), section)
    first = target.heading_line + 1 if target.heading_line >= 0 else 1
    # After the section's last non-empty bullet; before trailing empty bullets
    # and blank lines, which stay exactly where they were.
    at = first
    for i in range(first, target.end_line):
        if _bullet_text(lines[i]):
            at = i + 1
    new_lines = [*lines[:at], f"- {task}", *lines[at:]]
    return "\n".join(new_lines), target.name or "your tasks"


def only_inserted(before: str, after: str, line: str) -> bool:
    """True when `after` is `before` with exactly `line` added and nothing else changed."""
    old, new = before.split("\n"), after.split("\n")
    if len(new) != len(old) + 1:
        return False
    for i, candidate in enumerate(new):
        if candidate == line and new[:i] + new[i + 1 :] == old:
            return True
    return False


def note_content(get_note_result: str) -> str | None:
    """`content` from a get_note result (JSON text), or None if it is not one."""
    try:
        raw = json.loads(get_note_result)
    except json.JSONDecodeError:
        return None
    content = raw.get("content") if isinstance(raw, dict) else None
    return content if isinstance(content, str) else None


def write_ok(update_result: str) -> bool:
    try:
        raw = json.loads(update_result)
    except json.JSONDecodeError:
        return False
    return isinstance(raw, dict) and raw.get("success") is True


def freshest(fetched: str, superseded: frozenset[str], written: str | None) -> str:
    """Undo a stale cache read: if the server returns any version of the note
    that one of our own recent writes replaced, our latest written copy is current."""
    if written is not None and fetched in superseded:
        return written
    return fetched


def definitions(section_list: Sequence[str]) -> list[dict[str, Any]]:
    add_props: dict[str, Any] = {
        "task": {
            "type": "string",
            "description": "One task as a short phrase in the user's words, without 'add' or 'to my tasks'.",
        }
    }
    if section_list:
        add_props["section"] = {
            "type": "string",
            "enum": list(section_list),
            "description": f"Only when the user names a section. Default: {section_list[-1]}.",
        }
    return [
        {
            "type": "function",
            "function": {
                "name": READ_TOOL,
                "description": "Read the user's task list, grouped by section.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": ADD_TOOL,
                "description": "Add one task to the user's task list. Call once per task.",
                "parameters": {"type": "object", "properties": add_props, "required": ["task"]},
            },
        },
    ]


# (Simplenote MCP tool name, arguments, timeout seconds) -> result text
RawCall = Callable[[str, dict[str, Any], float], str]

INSTRUCTIONS = (
    "{read} and {add} are the user's task list, one Simplenote note. When they ask to add "
    "something to their tasks or to-do list, call {add} once per task with the task in their "
    "own words, then confirm in a few words. When they ask what their tasks are, call {read} "
    "and say them naturally, section by section, without numbering or symbols. You can only "
    "read and add tasks; to remove or change one, tell them to use the Simplenote app."
)


def build(options: Mapping[str, str], raw: RawCall) -> TaskNote:
    note_id = options.get("note_id", "").strip()
    if not note_id:
        raise ValueError("simplenote_tasks needs a note_id")
    return TaskNote(note_id, raw)


class TaskNote:
    """The imperative shell: get_note / update_note on one pinned note id."""

    def __init__(self, note_id: str, raw: RawCall) -> None:
        self._id = note_id
        self._raw = raw
        self._lock = threading.Lock()
        self._superseded: frozenset[str] = frozenset()
        self._written: str | None = None
        self._written_at = 0.0
        self.section_list: tuple[str, ...] = ()

    def prepare(self, timeout: float) -> None:
        """Read the section headings once, so add_task can offer them by name."""
        try:
            self.section_list = section_names(self._fetch(timeout))
            log.info("task note ready, sections: %s", ", ".join(self.section_list) or "(none)")
        except TaskNoteError as exc:
            log.warning("task note not readable at startup (%s); adding still works", exc)

    def definitions(self) -> list[dict[str, Any]]:
        return definitions(self.section_list)

    def instructions(self, public_names: Mapping[str, str]) -> str:
        return INSTRUCTIONS.format(read=public_names.get(READ_TOOL, READ_TOOL), add=public_names.get(ADD_TOOL, ADD_TOOL))

    def call(self, name: str, arguments: Mapping[str, Any], timeout: float) -> str:
        try:
            if name == READ_TOOL:
                return spoken_list(self._fetch(timeout))
            if name == ADD_TOOL:
                return self._add(clean_task(arguments.get("task")), arguments.get("section"), timeout)
        except TaskNoteError as exc:
            return f"Tool error: {exc}"
        return f"Unknown tool {name}."

    def _fetch(self, timeout: float) -> str:
        result = self._raw("get_note", {"id": self._id}, timeout)
        content = note_content(result)
        if content is None:
            raise TaskNoteError(f"could not read the task note ({result[:160]})")
        if time.monotonic() - self._written_at < CACHE_SECONDS:
            content = freshest(content, self._superseded, self._written)
        else:
            self._superseded = frozenset()
        return content

    def _add(self, task: str, section: object, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        with self._lock:
            current = self._fetch(timeout)
            wanted = section if isinstance(section, str) else None
            new, used = with_task(current, task, wanted)
            if not only_inserted(current, new, f"- {task}"):
                raise TaskNoteError("refused: the edit would change more than one line")
            remaining = max(0.5, deadline - time.monotonic())
            if not write_ok(self._raw("update_note", {"id": self._id, "content": new}, remaining)):
                raise TaskNoteError("Simplenote did not confirm the save; the task may not be added")
            self._superseded = self._superseded | {current}
            self._written, self._written_at = new, time.monotonic()
        total = sum(len(s.tasks) for s in sections(new))
        return f"Added '{task}' under {used}. The list now has {total} tasks."
