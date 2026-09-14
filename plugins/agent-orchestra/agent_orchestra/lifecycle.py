"""Task lifecycle: what each owner of an assigned task has said about it.

Delivery answers whether the assignment arrived. Lifecycle answers whether the
work was accepted, started, blocked, or finished, and only an owner's own typed
events move it. A conductor's reminder, an observer's report, or the owner's
later `tell` leaves it where it was. `tasks` used to show the newest message of
any act, so a `tell` after `done` made finished work look open and a conductor's
follow-up hid the block before it.
"""

from __future__ import annotations

import os
from typing import Any, Iterable


OWNER_STATES = ("accepted", "started")
ASSIGNER_STATES = ("reopened", "cancelled")
STATE_VALUES = OWNER_STATES + ASSIGNER_STATES
TERMINAL = frozenset({"done", "cancelled"})

DEFAULT_RESPONSE_WITHIN_SECONDS = 15 * 60
DEFAULT_STALE_AFTER_SECONDS = 60 * 60
RESPONSE_WITHIN_ENV = "AGENT_ORCHESTRA_RESPONSE_WITHIN"
STALE_AFTER_ENV = "AGENT_ORCHESTRA_STALE_AFTER"

_ATTENTION_ORDER = {"blocked": 0, "no-response": 1, "stale": 2}
_ATTENTION_LABEL = {"blocked": "blocked", "no-response": "no response", "stale": "no report"}


def derive_owners(
    task: dict[str, Any],
    messages: Iterable[dict[str, Any]],
    reached: dict[str, Iterable[str]],
    deliveries: dict[str, dict[str, Any]],
    *,
    history_complete: bool = True,
) -> list[dict[str, Any]]:
    """Each owner's lifecycle, replayed from the task's messages in send order.

    `messages` excludes the assign itself. `reached` maps a message id to the
    members it was delivered to; only reopen and cancel read it, because they
    apply to the owners they reached. `deliveries` maps an owner to its row for
    the assign message, which stays separate from lifecycle: `delivered` means
    the mail arrived and says nothing about the work.

    `history_complete` is False when the assign row itself is gone: a hub
    before this version pruned task messages after seven days. Replaying what
    is left from `pending` would call a finished task unanswered, so those
    owners start from `unknown`.
    """
    owners: dict[str, dict[str, Any]] = {}
    for owner in task["recipients"]:
        delivery = deliveries.get(owner) or {}
        owners[owner] = {
            "id": owner,
            "state": "pending" if history_complete else "unknown",
            "state_at": task["created_at"],
            "state_message_id": task["message_id"],
            "last_report_at": None,
            "delivery": delivery.get("state") or "unknown",
            "delivered_at": delivery.get("delivered_at"),
            "handled_at": delivery.get("handled_at"),
            "history": "complete" if history_complete else "pruned",
        }
    for message in messages:
        typed = message.get("lifecycle")
        if typed in ASSIGNER_STATES:
            # The hub stores these only from the assigner or the conductor.
            for owner in reached.get(message["id"], ()):
                row = owners.get(owner)
                if row is None:
                    continue
                if typed == "reopened":
                    _move(row, "pending", message)
                elif row["state"] != "done":
                    _move(row, "cancelled", message)
            continue
        row = owners.get(message["sender"])
        if row is None:
            continue
        row["last_report_at"] = message["sent_at"]
        if row["state"] in TERMINAL:
            continue
        act = message.get("act")
        if act == "done":
            _move(row, "done", message)
        elif act == "block":
            _move(row, "blocked", message)
        elif act == "status" and typed in OWNER_STATES:
            _move(row, str(typed), message)
        elif row["state"] == "pending":
            # The owner answered without a lifecycle state: a question, or a
            # message from a version that had no STATE header.
            _move(row, "unknown", message)
    return list(owners.values())


def _move(row: dict[str, Any], state: str, message: dict[str, Any]) -> None:
    row["state"] = state
    row["state_at"] = message["sent_at"]
    row["state_message_id"] = message["id"]


def aggregate(owners: list[dict[str, Any]]) -> str:
    """One state for the whole task. No single owner speaks for the others."""
    states = {str(row.get("state")) for row in owners}
    if not states:
        return "unknown"
    if "blocked" in states:
        return "blocked"
    if states <= TERMINAL:
        return "cancelled" if states == {"cancelled"} else "done"
    for state in ("pending", "unknown", "accepted"):
        if state in states:
            return state
    return "started"


def thresholds(
    response_within: float | None = None, stale_after: float | None = None
) -> tuple[float, float]:
    """Seconds an owner has to answer an assignment, and between reports.

    Two clocks on purpose: a five-hour pass answers within minutes and then
    reports hourly. Neither is a completion deadline.
    """
    return (
        _seconds(response_within, RESPONSE_WITHIN_ENV, DEFAULT_RESPONSE_WITHIN_SECONDS),
        _seconds(stale_after, STALE_AFTER_ENV, DEFAULT_STALE_AFTER_SECONDS),
    )


def _seconds(value: Any, env: str, default: float) -> float:
    for candidate in (value, os.environ.get(env)):
        if candidate is None or candidate == "":
            continue
        try:
            seconds = float(candidate)
        except (TypeError, ValueError):
            continue
        if seconds > 0 and seconds != float("inf"):
            return seconds
    return float(default)


def attention(
    tasks: Iterable[dict[str, Any]],
    *,
    member_id: str,
    conductor_id: str | None,
    at: float,
    response_within: float,
    stale_after: float,
) -> list[dict[str, Any]]:
    """Dispatches this member answers for that need a look now.

    A member answers for the tasks it assigned, and the conductor for every
    task. Nothing here re-runs anything. An overdue owner gets a status request
    on the same task: a delivered assignment says nothing about whether a
    store-mutating command already ran.
    """
    items: list[dict[str, Any]] = []
    for task in tasks:
        owners = task.get("owners")
        if not isinstance(owners, list):
            continue
        if task.get("sender") != member_id and member_id != conductor_id:
            continue
        for owner in owners:
            item = _attention_item(task, owner, at, response_within, stale_after)
            if item is not None:
                items.append(item)
    items.sort(key=lambda item: (_ATTENTION_ORDER[item["kind"]], item["since"]))
    return items


def _attention_item(
    task: dict[str, Any],
    owner: dict[str, Any],
    at: float,
    response_within: float,
    stale_after: float,
) -> dict[str, Any] | None:
    state = owner.get("state")
    if owner.get("history") == "pruned" and state != "blocked":
        # Nothing on record says what happened to it, so nothing here can say
        # it is late.
        return None
    if state == "blocked":
        kind, since = "blocked", owner.get("state_at")
    elif state == "pending":
        kind, since = "no-response", owner.get("state_at")
    elif state in {"accepted", "started", "unknown"}:
        kind, since = "stale", owner.get("last_report_at") or owner.get("state_at")
    else:
        return None
    try:
        since = float(since)
    except (TypeError, ValueError):
        since = at
    age = max(0.0, at - since)
    if kind == "no-response" and age < response_within:
        return None
    if kind == "stale" and age < stale_after:
        return None
    if kind == "blocked":
        next_step = "read the block; answer it or roll it up to the human"
    else:
        next_step = (
            f"ask for status: ACT ask, TASK {task.get('task')}, RE {task.get('message_id')}, "
            "NEED status; do not re-assign or re-run"
        )
    return {
        "kind": kind,
        "task": task.get("task"),
        "owner": owner.get("id"),
        "state": state,
        "since": since,
        "age_seconds": round(age, 1),
        "delivery": owner.get("delivery"),
        "delivered_at": owner.get("delivered_at"),
        "assign_message_id": task.get("message_id"),
        "next": next_step,
    }


def one_line(item: dict[str, Any]) -> str:
    """`t_harvest mb_4c1e: no response 38m, delivered`, short enough for Codex."""
    minutes = int(float(item.get("age_seconds") or 0) // 60)
    age = f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"
    line = f"{item.get('task')} {item.get('owner')}: {_ATTENTION_LABEL[item['kind']]} {age}"
    if item["kind"] == "no-response":
        line += f", {item.get('delivery') or 'unknown'}"
    return line
