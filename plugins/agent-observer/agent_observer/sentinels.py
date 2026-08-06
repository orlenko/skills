"""Deterministic health probes for background automation the operator owns.

Scheduled jobs fail quietly. A launchd analyze step that stopped writing, or a
dispatch queue whose oldest brief has waited a month, leaves evidence on disk
that nothing reads. These probes read exactly that evidence: file timestamps,
nothing more. No subprocess, no network, no model.

Enrollment is explicit and retirement is first class. A probe carries an expiry
so that a check for a system the operator has since retired stops asserting a
failure and starts asking to be re-affirmed or removed. A watchdog nobody can
retire becomes a permanent false alarm, and permanent false alarms are how an
attention surface loses the operator's trust.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


PROBES = {"file_freshness", "queue_backlog"}
SCAN_ENTRY_LIMIT = 500


def _describe_age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 90 * 60:
        return f"{int(seconds / 60)}m"
    if seconds < 48 * 3600:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def _file_freshness(target: Path, max_age: float, now: float) -> dict[str, Any]:
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {
            "status": "failing",
            "detail": "target is missing or unreadable",
            "age_seconds": None,
        }
    age = max(0.0, now - mtime)
    if age > max_age:
        return {
            "status": "failing",
            "detail": (
                f"last changed {_describe_age(age)} ago, "
                f"beyond the {_describe_age(max_age)} threshold"
            ),
            "age_seconds": age,
        }
    return {
        "status": "healthy",
        "detail": f"last changed {_describe_age(age)} ago",
        "age_seconds": age,
    }


def _queue_backlog(target: Path, max_age: float, now: float) -> dict[str, Any]:
    oldest: float | None = None
    counted = 0
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                if counted >= SCAN_ENTRY_LIMIT:
                    break
                if entry.name.startswith(".") or not entry.is_file():
                    continue
                counted += 1
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                oldest = mtime if oldest is None else min(oldest, mtime)
    except OSError:
        return {
            "status": "failing",
            "detail": "queue directory is missing or unreadable",
            "age_seconds": None,
        }
    if oldest is None:
        return {"status": "healthy", "detail": "queue is empty", "age_seconds": None}
    age = max(0.0, now - oldest)
    if age > max_age:
        return {
            "status": "failing",
            "detail": (
                f"{counted} item(s) waiting, oldest for {_describe_age(age)}, "
                f"beyond the {_describe_age(max_age)} threshold"
            ),
            "age_seconds": age,
        }
    return {
        "status": "healthy",
        "detail": f"{counted} item(s) waiting, oldest for {_describe_age(age)}",
        "age_seconds": age,
    }


def evaluate(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    moment = now if now is not None else time.time()
    expires_at = row.get("expires_at")
    result = {
        "sentinel_id": row["sentinel_id"],
        "label": row["label"],
        "probe": row["probe"],
        "target": row["target"],
        "max_age_seconds": float(row["max_age_seconds"]),
        "expires_at": expires_at,
        "note": row.get("note"),
    }
    if expires_at is not None and moment > float(expires_at):
        return {
            **result,
            "status": "expired",
            "detail": (
                "this check has not been re-affirmed; confirm the system still "
                "exists or remove the check"
            ),
            "age_seconds": None,
        }
    target = Path(str(row["target"])).expanduser()
    max_age = float(row["max_age_seconds"])
    if row["probe"] == "file_freshness":
        outcome = _file_freshness(target, max_age, moment)
    elif row["probe"] == "queue_backlog":
        outcome = _queue_backlog(target, max_age, moment)
    else:
        outcome = {
            "status": "expired",
            "detail": f"unsupported probe: {row['probe']}",
            "age_seconds": None,
        }
    return {**result, **outcome}
