"""Start scripts/dev.sh (backend + Cloudflare quick tunnel) fully detached.

Double-forks into a new session so the server keeps running after the shell that
launched it exits. caffeinate -i stops the Mac idle-sleeping while it runs.

    python3 scripts/run_detached.py start    # prints the endpoint URL
    python3 scripts/run_detached.py reload   # restart the backend only; URL unchanged
    python3 scripts/run_detached.py stop
    python3 scripts/run_detached.py status
"""

from __future__ import annotations

import os
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PIDFILE = DATA / "dev.pid"
SERVER_PIDFILE = DATA / "server.pid"  # written by dev.sh for the backend it supervises
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
LOG = DATA / "dev.log"
TUNNEL_LOG = DATA / "cloudflared.log"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _running_pgid() -> int | None:
    """Process-group id of a live detached run, or None."""
    try:
        pgid = int(PIDFILE.read_text().strip())
        os.killpg(pgid, 0)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return None
    return pgid


def start() -> int:
    if (pgid := _running_pgid()) is not None:
        print(f"already running (process group {pgid}); run 'stop' first")
        return status()
    DATA.mkdir(exist_ok=True)
    if os.fork() > 0:  # parent: wait for the tunnel URL, then report
        return _wait_for_url()
    os.setsid()  # new session: no longer in the launching shell's process group
    if os.fork() > 0:
        os._exit(0)
    PIDFILE.write_text(str(os.getpgrp()))
    os.chdir(ROOT)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.execvp("caffeinate", ["caffeinate", "-i", "scripts/dev.sh"])
    return 1  # unreachable


def _wait_for_url() -> int:
    for _ in range(80):
        time.sleep(0.5)
        text = LOG.read_text() if LOG.exists() else ""
        if "Skill endpoint" in text:
            break
        if "exit" in text.lower() and "error" in text.lower():
            break
    return status()


def reload() -> int:
    """Restart only the backend (new code and .env); the tunnel and its URL stay."""
    if _running_pgid() is None:
        print("not running; use 'start'")
        return 1
    try:
        pid = int(SERVER_PIDFILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (FileNotFoundError, ValueError, ProcessLookupError) as exc:
        print(f"no backend process to restart ({type(exc).__name__})")
        return 1
    for _ in range(60):
        time.sleep(0.5)
        try:
            new = int(SERVER_PIDFILE.read_text().strip())
        except (FileNotFoundError, ValueError):
            continue
        if new != pid and _healthy():
            print(f"backend restarted (pid {pid} -> {new})")
            return status()
    print("backend did not come back within 30 s; see data/dev.log")
    return 1


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def stop() -> int:
    pgid = _running_pgid()
    if pgid is None:
        print("not running")
        return 0
    os.killpg(pgid, signal.SIGTERM)
    PIDFILE.unlink(missing_ok=True)
    print(f"stopped process group {pgid}")
    return 0


def status() -> int:
    pgid = _running_pgid()
    text = TUNNEL_LOG.read_text() if TUNNEL_LOG.exists() else ""
    match = URL_RE.search(text)
    if pgid is None:
        print("not running")
        return 1
    url = f"{match.group(0)}/alexa" if match else "(tunnel URL not ready yet)"
    print(f"running (process group {pgid})\nendpoint: {url}")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    handlers = {"start": start, "stop": stop, "status": status, "reload": reload}
    if action not in handlers:
        sys.exit(f"usage: {sys.argv[0]} start|stop|status|reload")
    sys.exit(handlers[action]())
