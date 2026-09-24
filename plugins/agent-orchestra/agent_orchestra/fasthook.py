"""The no-membership answer for hooks, before the plugin is imported.

Every Claude and Codex turn in every directory runs these hooks, and almost
none of those directories hold a membership. The full path imports the HTTP,
TLS and process-walking code first, which costs most of a hook's time on a
loaded machine. This reads only member.json files. Anything unexpected falls
through to the full path, so it can only ever skip work, never lose mail.

`instance_key` and `state_root` mirror core.py; tests hold them equal.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

HOOKS = ("hook-context", "hook-stop", "hook-wait")
_ALIASES = {"claude-code": "claude", "anthropic": "claude", "openai": "codex"}


def state_root() -> Path:
    explicit = os.environ.get("AGENT_ORCHESTRA_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (base / "agent-orchestra").resolve()


def instance_key(provider: str, cwd: str) -> str:
    value = provider.strip().lower()
    value = _ALIASES.get(value, value)
    canonical = str(Path(cwd).expanduser().resolve())
    return hashlib.sha256(f"{value}\0{canonical}".encode("utf-8")).hexdigest()[:32]


def _provider(argv: list[str]) -> str:
    if "--provider" in argv:
        index = argv.index("--provider")
        if index + 1 < len(argv):
            return argv[index + 1]
    return os.environ.get("AGENT_ORCHESTRA_PROVIDER", "cli")


def _member_here(key: str) -> bool:
    for path in (state_root() / "members").glob("*/member.json"):
        try:
            member = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Unreadable is not proof of absence: let the full path decide.
            return True
        if isinstance(member, dict) and member.get("instance_key") == key \
                and not member.get("closed_at"):
            return True
    return False


def nothing_to_do(argv: list[str]) -> bool:
    """True, after printing the empty answer, when no membership is here."""
    try:
        raw = sys.stdin.read()
        # The full path reads the payload again.
        sys.stdin = io.StringIO(raw)
        payload = json.loads(raw) if raw.strip() else {}
        cwd = str(payload.get("cwd") or os.getcwd()) if isinstance(payload, dict) else os.getcwd()
        if _member_here(instance_key(_provider(argv), cwd)):
            return False
    except Exception:  # noqa: BLE001 - any doubt goes to the full path
        return False
    if argv[1] == "hook-stop":
        print("{}")
    return True
