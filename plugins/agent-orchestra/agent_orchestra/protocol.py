from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .core import MEMBER_ID_RE, MESSAGE_ID_RE, TASK_ID_RE, OrchestraError


ACTS = ("ask", "tell", "done", "block", "dissent", "assign", "status")
ALIASES = ("conductor", "parent", "children", "siblings", "all")
HEADER_KEYS = ("ACT", "TO", "RE", "TASK", "NEED", "REF")
MAX_HEADER_LINES = 20

_HEADER_LINE = re.compile(r"^([A-Z]+)[ \t]+(.+)$")
_MESSAGE_ID = re.compile(MESSAGE_ID_RE)
_MEMBER_ID = re.compile(MEMBER_ID_RE)
_TASK_ID = re.compile(TASK_ID_RE)


class ProtocolError(OrchestraError):
    """A malformed message header block or envelope field."""


@dataclass
class Envelope:
    act: str
    to: list[str]
    re: str | None = None
    task: str | None = None
    need: str = "none"
    refs: list[str] = field(default_factory=list)
    text: str = ""


def parse_message(text: str, extra_to: list[str] | None = None) -> Envelope:
    if not isinstance(text, str):
        raise ProtocolError("Message text must be a string")
    headers = _parse_headers(text)
    act = headers.get("ACT")
    if act is None:
        raise ProtocolError(f"ACT header is required; expected one of {', '.join(ACTS)}")
    if act not in ACTS:
        raise ProtocolError(f"Unknown act {act!r}; expected one of {', '.join(ACTS)}")

    recipients = _recipients(headers.get("TO"), extra_to)

    reference = headers.get("RE")
    if reference is not None and not _MESSAGE_ID.fullmatch(reference):
        raise ProtocolError(f"RE {reference!r} is not a message id matching {MESSAGE_ID_RE}")

    task = headers.get("TASK")
    if task is not None and not _TASK_ID.fullmatch(task):
        raise ProtocolError(f"TASK {task!r} is not a task id matching {TASK_ID_RE}")
    if act == "assign" and task is None:
        raise ProtocolError("Act assign requires a TASK header")

    need = headers.get("NEED", "none")
    refs = [item.strip() for item in headers.get("REF", "").split(",") if item.strip()]

    return Envelope(act=act, to=recipients, re=reference, task=task, need=need, refs=refs, text=text)


def reply_required(envelope_or_row: Envelope | dict[str, Any]) -> bool:
    if isinstance(envelope_or_row, Envelope):
        need: Any = envelope_or_row.need
    elif isinstance(envelope_or_row, dict):
        need = envelope_or_row.get("need")
    else:
        raise ProtocolError("reply_required needs an Envelope or a row with a need key")
    return str(need or "none").strip().lower() != "none"


def summarize(row: dict[str, Any]) -> str:
    parts = [f"act={row.get('act') or 'tell'}"]
    task = row.get("task")
    if task:
        parts.append(f"task={task}")
    parts.append(f"need={row.get('need') or 'none'}")
    sender = row.get("from")
    if isinstance(sender, dict):
        sender = sender.get("name") or sender.get("id")
    if sender:
        parts.append(f"from={sender}")
    if row.get("id"):
        parts.append(f"id={row['id']}")
    return " ".join(parts)


def validate_fields(
    act: Any,
    to: Any,
    re: Any,
    task: Any,
    need: Any,
    refs: Any,
) -> None:
    if act not in ACTS:
        raise ProtocolError(f"Unknown act {act!r}; expected one of {', '.join(ACTS)}")
    if not isinstance(to, (list, tuple)) or not to:
        raise ProtocolError("Message needs at least one recipient")
    for entry in to:
        if not isinstance(entry, str):
            raise ProtocolError(f"Recipient {entry!r} is not a string")
        _check_recipient(entry.strip())
    if re is not None and not _MESSAGE_ID.fullmatch(str(re)):
        raise ProtocolError(f"RE {re!r} is not a message id matching {MESSAGE_ID_RE}")
    if task is not None and not _TASK_ID.fullmatch(str(task)):
        raise ProtocolError(f"TASK {task!r} is not a task id matching {TASK_ID_RE}")
    if act == "assign" and task is None:
        raise ProtocolError("Act assign requires a task")
    if not isinstance(need, str) or not need.strip():
        raise ProtocolError("NEED must be a non-empty string; use 'none' when no reply is required")
    if not isinstance(refs, (list, tuple)):
        raise ProtocolError("REF must be a list of anchors")
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ProtocolError(f"REF anchor {ref!r} is empty")


def _parse_headers(text: str) -> dict[str, str]:
    block: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            break
        block.append(line)
    if not block:
        raise ProtocolError("Message must start with a header block")
    if len(block) > MAX_HEADER_LINES:
        raise ProtocolError(
            f"Header block is longer than {MAX_HEADER_LINES} lines: {len(block)} lines"
        )

    headers: dict[str, str] = {}
    for index, line in enumerate(block, start=1):
        match = _HEADER_LINE.match(line)
        if not match:
            raise ProtocolError(
                f"Message must start with a header block; line {index} is not \"KEY value\": {line!r}"
            )
        key = match.group(1)
        value = match.group(2).strip()
        if key not in HEADER_KEYS:
            raise ProtocolError(
                f"Unknown header key {key}; expected one of {', '.join(HEADER_KEYS)}"
            )
        if key in headers:
            raise ProtocolError(f"Duplicate {key} header")
        if not value:
            raise ProtocolError(f"{key} header has an empty value")
        headers[key] = value
    return headers


def _recipients(raw: str | None, extra_to: list[str] | None) -> list[str]:
    tokens: list[str] = []
    if raw:
        tokens.extend(raw.split(","))
    if extra_to:
        tokens.extend(extra_to)
    resolved: list[str] = []
    for entry in tokens:
        value = entry.strip()
        _check_recipient(value)
        if value not in resolved:
            resolved.append(value)
    if not resolved:
        raise ProtocolError("Message needs at least one recipient: a TO header or --to")
    return resolved


def _check_recipient(value: str) -> None:
    if value in ALIASES:
        return
    if _MEMBER_ID.fullmatch(value):
        return
    raise ProtocolError(
        f"Recipient {value!r} is not a member id matching {MEMBER_ID_RE} "
        f"nor one of the aliases: {', '.join(ALIASES)}"
    )
