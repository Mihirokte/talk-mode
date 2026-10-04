"""Refuse to commit or push anything that looks like a secret or personal data.

Runs from the git hooks in .githooks/ (enabled by `make setup`) and in CI.

    python3 scripts/secret_scan.py --staged    # pre-commit: the files about to be committed
    python3 scripts/secret_scan.py --tracked   # pre-push and CI: every tracked file at HEAD

Three layers, so one miss is caught by the next:
1. Paths that must never be tracked (.env, mcp_servers.json, data/, keys, databases).
2. Token patterns: OpenRouter/OpenAI/Anthropic/AWS/GitHub/Slack keys, private
   keys, real Alexa skill, account, device and person ids, Cloudflare tunnel
   hostnames.
3. Your own values, searched for literally so a value pasted into a doc or a
   test is caught even if no pattern matches:
   - every value in your local .env, and every ${VAR}-free string in
     mcp_servers.json env/adapter blocks;
   - logins that MCP servers keep outside this repo (simplenote-mcp's
     auth.json and telemetry.json in your user config directory);
   - your git user.email;
   - anything else you list in .secrets.local (git-ignored, one value per line).

Only the location of a finding is printed, never the matched value.
Standard library only, so the hook runs before any virtualenv exists.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_PATHS = (
    ".env",
    ".env.*",
    ".secrets.local",
    "mcp_servers.json",
    "auth.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.sqlite3",
    "*.jsonl",
    "*.log",
    "data/*",
    "build/*",
)
ALLOWED_PATHS = (".env.example",)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenRouter key", re.compile(r"sk-or-v1-[0-9a-f]{20,}")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("AWS secret key assignment", re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*\S{20,}")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # Placeholder ids made of one repeated digit (amzn1.ask.skill.00000000-...) are fine.
    (
        "Alexa skill id",
        re.compile(r"amzn1\.ask\.skill\.(?!([0-9a-f])\1{7}-)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    ),
    ("Alexa account/user id", re.compile(r"amzn1\.(?:ask\.account|account)\.[A-Z0-9]{20,}")),
    ("Alexa device/person id", re.compile(r"amzn1\.ask\.(?:device|person)\.[A-Za-z0-9_-]{20,}")),
    ("Cloudflare tunnel URL", re.compile(r"\b[a-z]+(?:-[a-z]+){2,}\.trycloudflare\.com")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{24,}=*")),
)
MIN_LITERAL = 8  # shorter .env values (true, 7.0, sqlite) are settings, not secrets
LITERAL_KIND = "a private value from this machine"
# Values already written in these tracked examples are public by definition.
PUBLIC_EXAMPLES = (".env.example", "mcp_servers.example.json")
SKIP_SUFFIXES = (".lock",)  # dependency hashes would only produce noise
# Files an MCP server keeps its login in, outside this repo. Paths mirror
# simplenote-mcp's providers/paths.js (getConfigDir).
SIMPLENOTE_STORE_FILES = ("auth.json", "telemetry.json")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"  {self.path}:{self.line}  {self.kind}" if self.line else f"  {self.path}  {self.kind}"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def simplenote_config_dir(platform: str, home: Path, env: Mapping[str, str]) -> Path:
    """Where simplenote-mcp keeps auth.json on this platform (sys.platform naming)."""
    if platform == "darwin":
        return home / "Library" / "Application Support" / "simplenote-mcp"
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "simplenote-mcp"
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    return Path(xdg or home / ".config") / "simplenote-mcp"


def json_strings(value: object) -> Iterator[str]:
    """Every string anywhere in a parsed JSON document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_strings(item)


def _outside_repo_values() -> list[str]:
    """Private values kept outside the repo: MCP logins, git email, .secrets.local."""
    values: list[str] = []
    store = simplenote_config_dir(sys.platform, Path.home(), os.environ)
    for name in SIMPLENOTE_STORE_FILES:
        path = store / name
        if path.is_file():
            try:
                values.extend(json_strings(json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError):
                print(f"secret_scan: could not read {path}; its values are not checked", file=sys.stderr)
    try:
        values.append(_git("config", "user.email").strip())
    except subprocess.CalledProcessError:  # no email configured (CI)
        pass
    extra = ROOT / ".secrets.local"
    if extra.exists():
        values.extend(s for line in extra.read_text().splitlines() if (s := line.strip()) and not s.startswith("#"))
    return values


def local_values() -> list[str]:
    """Literal values that exist only on this machine and must never appear in the repo."""
    values: list[str] = []
    env = ROOT / ".env"
    if env.exists():
        for raw in env.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip("\"'")
            if len(value) >= MIN_LITERAL:
                values.append(value)
    mcp = ROOT / "mcp_servers.json"
    if mcp.exists():
        try:
            servers = (json.loads(mcp.read_text()).get("mcpServers") or {}).values()
        except (json.JSONDecodeError, AttributeError):
            servers = []
        for cfg in servers:
            for block in ("env", "adapter"):
                for key, value in (cfg.get(block) or {}).items():
                    if key == "type":  # an adapter's type names code in this repo
                        continue
                    if isinstance(value, str) and "${" not in value and len(value) >= MIN_LITERAL:
                        values.append(value)
    values.extend(v for v in _outside_repo_values() if len(v) >= MIN_LITERAL)
    # Settings such as model names are long but harmless; anything that is
    # already in a tracked example file is public by definition.
    public = "".join((ROOT / name).read_text() for name in PUBLIC_EXAMPLES if (ROOT / name).exists())
    return sorted({v for v in values if v not in public})


def path_finding(path: str) -> Finding | None:
    name = Path(path).name
    if path in ALLOWED_PATHS or name in ALLOWED_PATHS:
        return None
    for pattern in FORBIDDEN_PATHS:
        if fnmatch(path, pattern) or fnmatch(name, pattern):
            return Finding(path, 0, f"file must never be committed (matches {pattern})")
    return None


def scan_text(
    path: str, text: str, literals: Iterable[str], patterns: Iterable[tuple[str, re.Pattern[str]]] = PATTERNS
) -> list[Finding]:
    found = []
    literal_list = list(literals)
    pattern_list = list(patterns)
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in pattern_list:
            if pattern.search(line):
                found.append(Finding(path, number, kind))
        for value in literal_list:
            if value in line:
                found.append(Finding(path, number, LITERAL_KIND))
    return found


def staged_files() -> list[tuple[str, str]]:
    names = [n for n in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0") if n]
    return [(n, _git("show", f":{n}")) for n in names]


def tracked_files() -> list[tuple[str, str]]:
    names = [n for n in _git("ls-files", "-z").split("\0") if n]
    return [(n, (ROOT / n).read_text(errors="replace")) for n in names if (ROOT / n).is_file()]


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--staged"
    files = staged_files() if mode == "--staged" else tracked_files()
    literals = local_values()
    findings: list[Finding] = []
    for path, text in files:
        if (hit := path_finding(path)) is not None:
            findings.append(hit)
            continue
        if path.endswith(SKIP_SUFFIXES):
            continue
        if path == "scripts/secret_scan.py":  # holds the patterns themselves; check literals only
            findings.extend(scan_text(path, text, literals, patterns=()))
            continue
        findings.extend(scan_text(path, text, literals))
    if findings:
        print("secret_scan: refusing, possible secret or personal data:", file=sys.stderr)
        for finding in findings:
            print(finding, file=sys.stderr)
        print("Remove it (keep real values in .env or .secrets.local, both git-ignored) and try again.", file=sys.stderr)
        return 1
    print(f"secret_scan: {len(files)} file(s) clean ({len(literals)} local value(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
