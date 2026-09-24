from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .core import MEMBER_ID_RE, MESSAGE_ID_RE, TASK_ID_RE, OrchestraError
from .lifecycle import ASSIGNER_STATES, OWNER_STATES, STATE_VALUES


ACTS = ("ask", "tell", "done", "block", "dissent", "assign", "status", "type")
ALIASES = ("conductor", "parent", "children", "siblings", "all")
HEADER_KEYS = ("ACT", "TO", "RE", "TASK", "NEED", "REF", "STATE", "TYPE")
MAX_HEADER_LINES = 20

_HEADER_LINE = re.compile(r"^([A-Z]+)[ \t]+(.+)$")
_MESSAGE_ID = re.compile(MESSAGE_ID_RE)
_MEMBER_ID = re.compile(MEMBER_ID_RE)
_TASK_ID = re.compile(TASK_ID_RE)
# `none` followed by more words. Only an exact `none` means no reply, so
# `NEED none — just a status update` asked for one without meaning to and
# competed with real blockers for attention.
_AMBIGUOUS_NONE = re.compile(r"none\b")
_STATE_AS_ACT = {"blocked": "block", "block": "block", "done": "done"}
DEFAULT_TYPE_QUIET_SECONDS = 1800
MAX_TYPE_QUIET_SECONDS = 4 * 3600
_KEY_NAME = re.compile(r"[A-Za-z0-9+-]{1,16}")


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
    lifecycle: str | None = None
    typing: TypeOptions | None = None


@dataclass(frozen=True)
class TypeOptions:
    """How the target's monitor types an ACT type body into the member's pane.

    `TYPE` header tokens: `enter` or `no-enter`, `idle` or `anytime`,
    `quiet=SECONDS`: how long, after typing, the orchestra stays silent in that
    session unless the member reports STATE started first, and `key=NAME`
    once per named key to press after the text (then no automatic Enter).
    """

    submit: bool = True
    anytime: bool = False
    quiet: int = DEFAULT_TYPE_QUIET_SECONDS
    # Named keys pressed after the text, instead of the automatic Enter.
    keys: tuple[str, ...] = ()
    # What ends the quiet early: the member's STATE started on the task, or
    # only its done or block. A multi-PR /qc launches its later PRs long
    # after the first STATE started.
    until: str = "started"
    # Set the quiet and type nothing.
    hold_only: bool = False

    def header(self) -> str:
        return " ".join((
            "enter" if self.submit else "no-enter",
            "anytime" if self.anytime else "idle",
            f"quiet={self.quiet}",
            f"until={self.until}",
            *(("hold",) if self.hold_only else ()),
            *(f"key={name}" for name in self.keys),
        ))


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
    _check_need(need)
    lifecycle = headers.get("STATE")
    _check_lifecycle(act, task, lifecycle)
    refs = [item.strip() for item in headers.get("REF", "").split(",") if item.strip()]
    typing = None
    if act == "type":
        _check_type_recipients(recipients)
        typing = parse_type_options(headers.get("TYPE"))
        if not message_body(text).strip() and not typing.keys and not typing.hold_only:
            raise ProtocolError("ACT type needs a body, the text to type, or a key=NAME")
    elif "TYPE" in headers:
        raise ProtocolError("The TYPE header goes on ACT type")

    return Envelope(
        act=act,
        to=recipients,
        re=reference,
        task=task,
        need=need,
        refs=refs,
        text=text,
        lifecycle=lifecycle,
        typing=typing,
    )


def parse_type_options(raw: str | None) -> TypeOptions:
    submit, anytime, quiet = True, False, DEFAULT_TYPE_QUIET_SECONDS
    until, hold_only = "started", False
    names: list[str] = []
    for token in (raw or "").split():
        if token == "hold":
            hold_only = True
        elif token.startswith("until="):
            until = token[len("until="):]
            if until not in ("started", "done"):
                raise ProtocolError(f"TYPE {token!r}: until is started or done")
        elif token.startswith("key="):
            name = token[len("key="):]
            if not _KEY_NAME.fullmatch(name):
                raise ProtocolError(f"TYPE {token!r}: a key is a tmux key name such as Enter or C-c")
            names.append(name)
        elif token in ("enter", "no-enter"):
            submit = token == "enter"
        elif token in ("idle", "anytime"):
            anytime = token == "anytime"
        elif token.startswith("quiet="):
            try:
                quiet = int(token[len("quiet="):])
            except ValueError:
                raise ProtocolError(f"TYPE {token!r}: quiet takes whole seconds") from None
            if not 0 <= quiet <= MAX_TYPE_QUIET_SECONDS:
                raise ProtocolError(f"TYPE quiet must be 0 to {MAX_TYPE_QUIET_SECONDS} seconds")
        else:
            raise ProtocolError(
                f"Unknown TYPE token {token!r}; expected enter|no-enter, idle|anytime, "
                "quiet=SECONDS, until=started|done, hold, key=NAME"
            )
    return TypeOptions(submit=submit, anytime=anytime, quiet=quiet, keys=tuple(names),
                       until=until, hold_only=hold_only)


def message_body(text: str) -> str:
    """Everything after the header block's blank line, byte for byte."""
    _, separator, body = text.partition("\n\n")
    return body if separator else ""


def _check_type_recipients(to: Any) -> None:
    if len(to) != 1 or not _MEMBER_ID.fullmatch(str(to[0]).strip()):
        raise ProtocolError("ACT type goes to exactly one member id, not an alias")


def reply_required(envelope_or_row: Envelope | dict[str, Any]) -> bool:
    if isinstance(envelope_or_row, Envelope):
        need: Any = envelope_or_row.need
    elif isinstance(envelope_or_row, dict):
        need = envelope_or_row.get("need")
    else:
        raise ProtocolError("reply_required needs an Envelope or a row with a need key")
    return str(need or "none").strip().lower() != "none"


def attention_rank(row: dict[str, Any]) -> int:
    """Blocks first, then anything that needs a reply, then the rest."""
    if row.get("act") == "block":
        return 0
    return 1 if reply_required(row) else 2


def summarize(row: dict[str, Any]) -> str:
    parts = [f"act={row.get('act') or 'tell'}"]
    task = row.get("task")
    if task:
        parts.append(f"task={task}")
    lifecycle = row.get("lifecycle")
    if lifecycle:
        parts.append(f"state={lifecycle}")
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
    lifecycle: Any = None,
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
    if act == "type":
        _check_type_recipients(to)
    if not isinstance(need, str) or not need.strip():
        raise ProtocolError("NEED must be a non-empty string; use 'none' when no reply is required")
    _check_need(need)
    if not isinstance(refs, (list, tuple)):
        raise ProtocolError("REF must be a list of anchors")
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            raise ProtocolError(f"REF anchor {ref!r} is empty")
    _check_lifecycle(act, task, lifecycle)


def _check_need(need: str) -> None:
    value = need.strip()
    if value.lower() != "none" and _AMBIGUOUS_NONE.match(value.lower()):
        raise ProtocolError(
            f"NEED {value!r} is ambiguous: only an exact `NEED none` means no reply, "
            "and anything after it makes the message reply-required. Write `NEED none` "
            "and move the explanation into the body, or give the shape of the reply you need"
        )


def _check_lifecycle(act: Any, task: Any, lifecycle: Any) -> None:
    if lifecycle is None:
        return
    if not isinstance(lifecycle, str) or lifecycle not in STATE_VALUES:
        instead = _STATE_AS_ACT.get(str(lifecycle).strip().lower())
        if instead:
            raise ProtocolError(f"STATE {lifecycle!r} is not a lifecycle state; send ACT {instead}")
        raise ProtocolError(
            f"Unknown STATE {lifecycle!r}; expected one of {', '.join(STATE_VALUES)}"
        )
    if task is None:
        raise ProtocolError(f"STATE {lifecycle} needs a TASK naming the assignment it reports on")
    if lifecycle in OWNER_STATES and act != "status":
        raise ProtocolError(f"STATE {lifecycle} goes on ACT status")
    if lifecycle in ASSIGNER_STATES and act not in ("tell", "ask"):
        raise ProtocolError(f"STATE {lifecycle} goes on ACT tell or ACT ask")


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
