"""TypeSafe Jev: the client, the switch, and the one send-time check that earned a place.

Off unless AGENT_ORCHESTRA_JEV=1 (or the older AGENT_ORCHESTRA_JEV_SHADOW=1)
and TYPESAFE_API_KEY are set. Turning it on sends message bodies and
transcript tails to TypeSafe, a third party.

The send check came out of a benchmark on 1,221 real orchestra messages
(aiq/bench/jev/orchestra/mail). Of the five questions tried, only one beat
the sender's own headers. When a body asks for a reply but `NEED` is `none`,
Jev at p >= 0.9 was right 7 of 8 times. It stays a warning, because the
message has already gone out and eight labels cannot justify changing
routing. Jev was worse than the headers on ACT, blocked, and done-evidence,
so it is not asked about those.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from typing import Any

from .core import now, runtime_dir


FLAG_ENV = "AGENT_ORCHESTRA_JEV"
LEGACY_FLAG_ENV = "AGENT_ORCHESTRA_JEV_SHADOW"
KEY_ENV = "TYPESAFE_API_KEY"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
SEND_TIMEOUT_SECONDS = 3.0
NEEDS_REPLY_THRESHOLD = 0.9
MIN_BODY_CHARS = 20
LOG_CAP_BYTES = 20 * 1024 * 1024

NEEDS_REPLY_QUESTION = {
    "needs_reply": {
        "type": "noul",
        "instructions": "The state is the body of a message one coding agent sends to other "
                        "agents in a team called an orchestra; its header lines are removed. "
                        "Does the sender expect the recipient to answer or do something in "
                        "response?",
        "criteria": {
            "true": "It asks a question, requests a decision, evidence, or an action, assigns "
                    "work, or asks for an acknowledgement.",
            "false": "It informs, reports, or closes something, and nothing is asked of the "
                     "recipient.",
        },
    },
}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def enabled() -> bool:
    flag = os.environ.get(FLAG_ENV) or os.environ.get(LEGACY_FLAG_ENV)
    return _truthy(flag) and bool(os.environ.get(KEY_ENV, "").strip())


def ask(state: str, questions: dict[str, Any], *, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """One call. Returns (answers, meta). Raises on any transport or HTTP error."""
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ.get(KEY_ENV, '').strip()}",
                 "Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    meta = {
        "input_tokens": (data.get("usage") or {}).get("input_tokens"),
        "model": data.get("model"),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }
    return data["answers"], meta


def message_body(text: str) -> str:
    """The body under the header block, which ends at the first blank line."""
    parts = text.split("\n\n", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def send_check(member_id: str, message_id: str, act: str, need: str, text: str) -> str | None:
    """A warning for the sender, or None. Never raises and never blocks past its timeout.

    Runs only for `NEED none`: the benchmark found no use for it in the other
    direction. Every call is logged to `<member>.jev-send.jsonl`, with the
    body, for a later look at false alarms.
    """
    if not enabled() or str(need or "none").strip().lower() != "none":
        return None
    body = message_body(text)
    if len(body) < MIN_BODY_CHARS:
        return None
    row: dict[str, Any] = {
        "ts": now(), "member_id": member_id, "message_id": message_id, "act": act,
        "sha": hashlib.sha256(body.encode("utf-8")).hexdigest(), "body": body,
    }
    warning = None
    try:
        answers, meta = ask(body, NEEDS_REPLY_QUESTION, timeout=SEND_TIMEOUT_SECONDS)
        p = float(answers["needs_reply"]["noul"])
        row.update(meta, needs_reply_p=p)
        if p >= NEEDS_REPLY_THRESHOLD:
            warning = (
                f"NEED is none, but the body reads as asking for a reply (Jev p={p:.2f}). "
                "Only a NEED other than none makes the recipient treat it as owed. If you "
                f"need an answer, send one short follow-up with RE {message_id} and NEED "
                "set to the answer's shape; otherwise ignore this."
            )
    except Exception as exc:  # noqa: BLE001 - a warning is never worth a failed send
        row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    row["warned"] = warning is not None
    _append(runtime_dir() / f"{member_id}.jev-send.jsonl", row)
    return warning


def _append(path, row: dict[str, Any]) -> None:
    try:
        if path.exists() and path.stat().st_size > LOG_CAP_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
