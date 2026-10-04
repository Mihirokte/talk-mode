from __future__ import annotations

from bridge.mcp.config import AdapterSpec, parse_specs

CONFIG = {
    "mcpServers": {
        "notes": {
            "command": "npx",
            "args": ["-y", "notes-mcp@1.0.0"],
            "env": {"TOKEN": "${NOTES_TOKEN}"},
            "adapter": {"type": "simplenote_tasks", "note_id": "${NOTE_ID}"},
            "lambda": False,
        },
        "time": {
            "command": "uvx",
            "args": ["mcp-server-time", "--local-timezone=${TZ_NAME}"],
            "tools": ["convert_time"],
            "lambda": {"packages": ["mcp-server-time"], "command": "python", "args": ["-m", "mcp_server_time"]},
        },
        "off": {"disabled": True, "command": "x"},
    }
}


def test_variables_are_substituted_from_the_environment() -> None:
    specs = parse_specs(CONFIG, {"NOTES_TOKEN": "t", "NOTE_ID": "n1", "TZ_NAME": "UTC"}, python="py")
    assert [s.name for s in specs] == ["notes", "time"]
    notes, time = specs
    assert notes.env == {"TOKEN": "t"}
    assert notes.adapter == AdapterSpec("simplenote_tasks", {"note_id": "n1"})
    assert time.args == ("mcp-server-time", "--local-timezone=UTC")
    assert time.allow == frozenset({"convert_time"})


def test_a_server_with_an_unset_variable_is_skipped_not_fatal() -> None:
    specs = parse_specs(CONFIG, {"TZ_NAME": "UTC"}, python="py")
    assert [s.name for s in specs] == ["time"]


def test_lambda_uses_the_bundled_command_and_skips_local_only_servers() -> None:
    env = {"LAMBDA_TASK_ROOT": "/var/task", "NOTES_TOKEN": "t", "NOTE_ID": "n1", "TZ_NAME": "UTC"}
    specs = parse_specs(CONFIG, env, python="/var/lang/bin/python")
    assert [s.name for s in specs] == ["time"]
    assert specs[0].command == "/var/lang/bin/python"
    assert specs[0].args == ("-m", "mcp_server_time")
    assert specs[0].env["PYTHONPATH"] == "/var/task"
