"""What Agent Orchestra knows about the agent in a pane, read from its files.

A member records the pid of the agent session that holds its seat
(`owner_pid`), which is the process this tool finds in the pane. That link
gives the nudger two facts no screen shows: unread mail waiting in the
member's local inbox, and the tasks it owns. Read-only, and optional: with no
orchestra state on the machine, every pane reads as not a member.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CLOSED_STATES = {"done", "cancelled"}


@dataclass
class Seat:
    member_id: str
    name: str
    role: str
    unread: int = 0
    oldest_unread_at: float | None = None
    unread_times: list[float] = field(default_factory=list)
    open_tasks: list[tuple[str, str]] = field(default_factory=list)
    # Set by agent-orchestra 0.2.9+ right before the conductor types into this
    # session. Nothing may reach the session until then, a nudge included.
    quiet_until: float = 0.0


def _root() -> Path:
    if os.environ.get("AGENT_ORCHESTRA_HOME"):
        return Path(os.environ["AGENT_ORCHESTRA_HOME"])
    return Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "agent-orchestra"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def seats_by_pid() -> dict[int, Seat]:
    seats: dict[int, Seat] = {}
    root = _root()
    for path in (root / "members").glob("*/member.json"):
        member = _read(path)
        if not member or member.get("closed_at"):
            continue
        try:
            pid = int(member.get("owner_pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        member_id = str(member.get("member_id") or path.parent.name)
        seat = Seat(member_id=member_id, name=str(member.get("name") or member_id),
                    role=str(member.get("role") or "player"))
        for item in (path.parent / "pending").glob("*.json"):
            row = _read(item)
            seat.unread += 1
            stamp = row.get("received_at") or row.get("sent_at")
            if isinstance(stamp, (int, float)):
                seat.oldest_unread_at = min(seat.oldest_unread_at or stamp, stamp)
                seat.unread_times.append(float(stamp))
        try:
            seat.quiet_until = float(_read(root / "runtime" / f"{member_id}.quiet.json").get("until") or 0)
        except (TypeError, ValueError):
            seat.quiet_until = 0.0
        snapshot = _read(root / "runtime" / f"{member_id}.tasks.json")
        for task in snapshot.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for owner in task.get("owners") or []:
                if isinstance(owner, dict) and owner.get("id") == member_id \
                        and owner.get("state") not in CLOSED_STATES:
                    seat.open_tasks.append((str(task.get("task")), str(owner.get("state"))))
        seats[pid] = seat
    return seats
