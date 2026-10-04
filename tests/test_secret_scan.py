"""The secret scanner itself. Fake secrets are assembled at runtime so this file holds none."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location("secret_scan", Path(__file__).parent.parent / "scripts" / "secret_scan.py")
assert _SPEC is not None and _SPEC.loader is not None
scan = importlib.util.module_from_spec(_SPEC)
sys.modules["secret_scan"] = scan  # dataclasses look their module up here
_SPEC.loader.exec_module(scan)


def kinds(text: str, literals: tuple[str, ...] = ()) -> set[str]:
    return {f.kind for f in scan.scan_text("x.md", text, literals)}


def test_token_patterns_are_caught() -> None:
    assert "OpenRouter key" in kinds("key=" + "sk-or-v1-" + "ab12" * 16)
    assert "AWS access key id" in kinds("AKIA" + "Q" * 16)
    assert "GitHub token" in kinds("ghp_" + "a1" * 18)
    assert "private key" in kinds("-----BEGIN RSA " + "PRIVATE KEY-----")
    assert "Cloudflare tunnel URL" in kinds("https://flush-some-words-" + "here.trycloudflare.com/alexa")


def test_real_alexa_ids_are_caught_but_placeholders_pass() -> None:
    real = "amzn1.ask.skill." + "1a2b3c4d-1111-2222-3333-444455556666"
    assert "Alexa skill id" in kinds(real)
    assert kinds("amzn1.ask.skill.00000000-0000-0000-0000-000000000000") == set()


def test_local_values_are_matched_literally() -> None:
    private = "my-own-" + "value-1234"
    assert kinds(f"note: {private}", (private,)) == {"a value from your local .env / mcp_servers.json"}


def test_forbidden_paths() -> None:
    for path in (".env", ".env.local", "mcp_servers.json", "data/bridge.sqlite3", "x/auth.json", "id.pem"):
        assert scan.path_finding(path) is not None, path
    for path in (".env.example", "mcp_servers.example.json", "bridge/llm.py"):
        assert scan.path_finding(path) is None, path
