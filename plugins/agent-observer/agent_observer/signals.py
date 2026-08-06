"""Machine-global spool for harness signals that never reach a session log.

A worker session that is blocked on a permission prompt writes nothing to its
transcript, so silence is the only trace collection can see. Claude and Codex
both invoke a local program at that moment, and this module is the bounded
place those invocations may leave a note.

Two properties matter more than throughput. Hooks must never fail or delay a
worker session, so every writer path swallows its own errors. And the spool is
untrusted input even though it is local, so readers validate every field
instead of trusting the producer.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


SIGNAL_VERSION = 1
SIGNAL_KINDS = {"input_requested"}
PROVIDERS = {"claude", "codex"}
MAX_LINE_BYTES = 8 * 1024
MAX_SPOOL_BYTES = 1024 * 1024
MAX_DETAIL_CHARS = 500
MAX_RECORDS_PER_DRAIN = 500


def spool_path() -> Path:
    return Path(
        os.environ.get("AGENT_OBSERVER_SIGNAL_SPOOL")
        or Path.home() / ".local" / "state" / "agent-observer-signals.jsonl"
    ).expanduser()


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(
        character for character in value if character.isprintable()
    ).strip()[:limit]


def build_signal(
    provider: str,
    *,
    session_id: str,
    cwd: str,
    kind: str = "input_requested",
    detail: str = "",
    origin: str = "",
    at: float | None = None,
) -> dict[str, Any]:
    return {
        "v": SIGNAL_VERSION,
        "provider": provider,
        "session_id": session_id,
        "cwd": cwd,
        "kind": kind,
        "detail": detail,
        "origin": origin,
        "at": at if at is not None else time.time(),
    }


def valid_signal(value: Any) -> dict[str, Any] | None:
    """Return a normalized signal, or None when anything is out of contract."""
    if not isinstance(value, dict) or value.get("v") != SIGNAL_VERSION:
        return None
    provider = value.get("provider")
    kind = value.get("kind")
    if provider not in PROVIDERS or kind not in SIGNAL_KINDS:
        return None
    session_id = _clean(value.get("session_id"), 200)
    cwd = _clean(value.get("cwd"), 4096)
    if not session_id or not cwd:
        return None
    try:
        at = float(value.get("at"))
    except (TypeError, ValueError):
        return None
    if at <= 0:
        return None
    return {
        "v": SIGNAL_VERSION,
        "provider": str(provider),
        "session_id": session_id,
        "cwd": cwd,
        "kind": str(kind),
        "detail": _clean(value.get("detail"), MAX_DETAIL_CHARS),
        "origin": _clean(value.get("origin"), 64),
        "at": at,
    }


def append_signal(record: dict[str, Any], *, path: Path | None = None) -> bool:
    """Append one signal. Never raises: a hook must not break its session."""
    target = path or spool_path()
    line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
    if len(line.encode("utf-8")) > MAX_LINE_BYTES:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            os.write(handle, line.encode("utf-8"))
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)
    except OSError:
        return False
    return True


def drain_signals(path: Path | None = None) -> list[dict[str, Any]]:
    """Take every pending signal and clear the spool under one exclusive lock.

    Reading and clearing together keeps a concurrent hook from losing a line.
    A crash between this call and the caller's database write loses at most one
    real-time notice, which the collector's own silence rule recovers later.
    """
    target = path or spool_path()
    try:
        handle = os.open(target, os.O_RDWR)
    except OSError:
        return []
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = b""
        while True:
            chunk = os.read(handle, 65536)
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_SPOOL_BYTES:
                break
        os.ftruncate(handle, 0)
    except OSError:
        return []
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)

    signals: list[dict[str, Any]] = []
    for line in raw.splitlines()[:MAX_RECORDS_PER_DRAIN]:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        record = valid_signal(value)
        if record:
            signals.append(record)
    return signals


def normalize_hook_payload(
    provider: str,
    payload: dict[str, Any],
    *,
    session_id: str = "",
    cwd: str = "",
    detail: str = "",
) -> dict[str, Any] | None:
    """Map a provider hook payload onto a signal, or None when it is lifecycle.

    Turn-completion notices are deliberately dropped. A finished turn is
    activity, not an unmet request, and promoting it would rebuild exactly the
    lifecycle wall the attention ledger exists to avoid.
    """
    if provider not in PROVIDERS:
        return None
    origin = _clean(
        payload.get("hook_event_name") or payload.get("type") or "", 64
    )
    if provider == "codex":
        kinds = str(payload.get("type") or "")
        if kinds and "approval" not in kinds and "request" not in kinds:
            return None
    resolved_session = session_id or _clean(
        payload.get("session_id") or payload.get("session-id") or "", 200
    )
    resolved_cwd = cwd or _clean(payload.get("cwd") or "", 4096)
    if not resolved_session or not resolved_cwd:
        return None
    return build_signal(
        provider,
        session_id=resolved_session,
        cwd=resolved_cwd,
        detail=detail
        or _clean(
            payload.get("message") or payload.get("notification") or "",
            MAX_DETAIL_CHARS,
        ),
        origin=origin,
    )
