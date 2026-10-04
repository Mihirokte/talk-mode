"""The task-note adapter must never lose or change a line of the user's note."""

from __future__ import annotations

import json
from typing import Any

from bridge.mcp.adapters import simplenote_tasks as st

NOTE = "\n".join(
    [
        "Tasks",
        "*Immediate*",
        "- renew passport",
        "- call the bank",
        "",
        "*Later* ",
        "- fix the bike",
        "- ",
    ]
)


def test_sections_and_spoken_list() -> None:
    assert st.section_names(NOTE) == ("Immediate", "Later")
    spoken = st.spoken_list(NOTE)
    assert spoken.startswith("3 task(s).")
    assert "Immediate: renew passport; call the bank." in spoken
    assert "*" not in spoken and "- " not in spoken


def test_add_goes_to_last_section_before_the_trailing_empty_bullet() -> None:
    new, used = st.with_task(NOTE, "buy milk")
    assert used == "Later"
    assert new.split("\n")[-3:] == ["- fix the bike", "- buy milk", "- "]
    assert st.only_inserted(NOTE, new, "- buy milk")


def test_add_to_a_named_section_keeps_the_blank_line() -> None:
    new, used = st.with_task(NOTE, "pay rent", "immediate")
    assert used == "Immediate"
    assert new.split("\n")[1:6] == ["*Immediate*", "- renew passport", "- call the bank", "- pay rent", ""]
    assert st.only_inserted(NOTE, new, "- pay rent")


def test_note_without_headings() -> None:
    plain = "Tasks\n- one\n- two"
    new, _ = st.with_task(plain, "three")
    assert new == "Tasks\n- one\n- two\n- three"
    assert st.spoken_list("Tasks") == "The task list is empty."


def test_only_inserted_rejects_any_other_change() -> None:
    assert not st.only_inserted(NOTE, NOTE.replace("call the bank", "call bank") + "\n- x", "- x")
    assert not st.only_inserted(NOTE, NOTE, "- x")


def test_clean_task() -> None:
    assert st.clean_task("  - buy   milk. ") == "buy milk"
    assert len(st.clean_task("x" * 500)) == st.MAX_TASK_CHARS


class FakeSimplenote:
    """Behaves like simplenote-mcp's get_note/update_note, including its 60 s read cache."""

    def __init__(self, content: str, stale_reads: bool = False) -> None:
        self.content = content
        self.cached = content
        self.stale_reads = stale_reads
        self.writes = 0

    def __call__(self, tool: str, args: dict[str, Any], timeout: float) -> str:
        assert args["id"] == "note-1", "the adapter must only ever touch the pinned note"
        if tool == "get_note":
            shown = self.cached if self.stale_reads else self.content
            return json.dumps({"id": "note-1", "content": shown})
        if tool == "update_note":
            assert set(args) == {"id", "content"}
            self.content = args["content"]
            self.writes += 1
            return json.dumps({"success": True, "id": "note-1", "version": self.writes})
        raise AssertionError(f"unexpected tool {tool}")


def test_two_quick_adds_survive_a_stale_cache() -> None:
    server = FakeSimplenote(NOTE, stale_reads=True)
    note = st.build({"note_id": "note-1"}, server)
    note.prepare(timeout=1)
    assert "Added 'buy milk'" in note.call(st.ADD_TOOL, {"task": "buy milk"}, 2)
    assert "Added 'book tickets'" in note.call(st.ADD_TOOL, {"task": "book tickets"}, 2)
    assert "- buy milk" in server.content and "- book tickets" in server.content
    assert server.content.count("\n") == NOTE.count("\n") + 2
    assert "buy milk" in note.call(st.READ_TOOL, {}, 2)


def test_bad_input_never_writes() -> None:
    server = FakeSimplenote(NOTE)
    note = st.build({"note_id": "note-1"}, server)
    assert note.call(st.ADD_TOOL, {"task": "   "}, 2).startswith("Tool error")
    assert note.call(st.ADD_TOOL, {}, 2).startswith("Tool error")
    assert server.writes == 0 and server.content == NOTE


def test_definitions_expose_no_note_id() -> None:
    names = {d["function"]["name"] for d in st.definitions(("Immediate", "Later"))}
    assert names == {st.READ_TOOL, st.ADD_TOOL}
    for d in st.definitions(("Immediate", "Later")):
        assert "id" not in d["function"]["parameters"]["properties"]
