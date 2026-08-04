from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .model import JsonRecord, NormalizedEvent, SourceCandidate


CANONICALIZATION = {"claude": "claude-v1", "codex": "codex-v1"}
MAX_EXCERPT = 4096
_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RecordEvents:
    record: JsonRecord
    events: tuple[NormalizedEvent, ...]
    cwd: str | None
    recognized: bool


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bounded(text: str) -> str:
    return text[:MAX_EXCERPT]


def _content_blocks(content: Any, text_type: str) -> list[tuple[int, str]]:
    if isinstance(content, str):
        return [(0, content.strip())] if content.strip() else []
    if not isinstance(content, list):
        return []
    result: list[tuple[int, str]] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != text_type:
            continue
        text = _text(block.get("text"))
        if text:
            result.append((index, text))
    return result


def _claude_record_id(obj: dict[str, Any], record: JsonRecord) -> str:
    return str(obj.get("uuid") or f"bytes-{record.start}-{record.end}")


def _message_ref(
    provider: str,
    session_id: str,
    generation: int,
    record: JsonRecord,
    suffix: str,
) -> str:
    return (
        f"{CANONICALIZATION[provider]}:{session_id}:{generation}:"
        f"{record.start}-{record.end}:{suffix}"
    )


def _claude_title(obj: dict[str, Any]) -> str | None:
    if obj.get("type") not in {"custom-title", "title", "session_title"}:
        return None
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    return (
        _text(
            obj.get("customTitle")
            or obj.get("title")
            or payload.get("title")
            or payload.get("name")
        )
        or None
    )


def _codex_payload(obj: dict[str, Any]) -> dict[str, Any]:
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else {}


def record_cwd(provider: str, obj: dict[str, Any]) -> str | None:
    if provider == "claude":
        return _text(obj.get("cwd")) or None
    payload = _codex_payload(obj)
    if obj.get("type") == "session_meta":
        return _text(payload.get("cwd")) or None
    if obj.get("type") == "turn_context":
        return _text(payload.get("cwd")) or None
    return None


def detect_codex_message_mode(records: Iterable[JsonRecord]) -> str:
    saw_response = False
    for record in records:
        obj = record.value or {}
        payload = _codex_payload(obj)
        if obj.get("type") == "event_msg" and payload.get("type") in {
            "user_message",
            "agent_message",
        }:
            return "display"
        if (
            obj.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") in {"user", "assistant"}
        ):
            saw_response = True
    return "response" if saw_response else "unknown"


def source_candidate(
    provider: str,
    path: Path,
    records: Iterable[JsonRecord],
    *,
    file_size: int,
    tail_start: int,
    mtime: float,
) -> SourceCandidate | None:
    ordered = sorted(
        (record for record in records if record.value is not None),
        key=lambda record: record.start,
    )
    if not ordered:
        return None
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    parent_id: str | None = None
    tail_has_turn_context = False

    if provider == "claude":
        match = _UUID_RE.search(path.stem)
        session_id = match.group(1) if match else path.stem
        for record in ordered:
            obj = record.value or {}
            session_id = _text(obj.get("sessionId")) or session_id
            cwd = record_cwd(provider, obj) or cwd
            title = _claude_title(obj) or title
        mode = "display"
    else:
        for record in ordered:
            obj = record.value or {}
            payload = _codex_payload(obj)
            if obj.get("type") == "session_meta":
                session_id = _text(payload.get("id")) or session_id
                cwd = _text(payload.get("cwd")) or cwd
                parent_id = _text(payload.get("parent_thread_id")) or parent_id
            elif obj.get("type") == "turn_context":
                cwd = _text(payload.get("cwd")) or cwd
                if record.start >= tail_start:
                    tail_has_turn_context = True
        if tail_start > 4 * 1024 * 1024 and not tail_has_turn_context:
            cwd = None
        match = _UUID_RE.search(path.stem)
        session_id = session_id or (match.group(1) if match else path.stem)
        mode = detect_codex_message_mode(ordered)

    if not session_id or parent_id:
        return None
    return SourceCandidate(
        provider=provider,
        session_id=session_id,
        path=path,
        current_cwd=cwd,
        message_mode=mode,
        title=title,
        mtime=mtime,
    )


def _claude_answers(block: dict[str, Any]) -> dict[str, str]:
    value = block.get("answers")
    content = block.get("content")
    if value is None and isinstance(content, dict):
        value = content.get("answers")
    answers: dict[str, str] = {}
    if isinstance(value, dict):
        for key, answer in value.items():
            if isinstance(answer, (str, int, float, bool)):
                answers[str(key)] = str(answer)[:MAX_EXCERPT]
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("question_index", item.get("item_id"))
            answer = item.get("answer")
            if key is not None and isinstance(answer, (str, int, float, bool)):
                answers[str(key)] = str(answer)[:MAX_EXCERPT]
    return answers


def _claude_events(
    record: JsonRecord,
    session_id: str,
    generation: int,
) -> RecordEvents:
    obj = record.value or {}
    kind = obj.get("type")
    timestamp = _text(obj.get("timestamp")) or None
    events: list[NormalizedEvent] = []
    ordinal = 0
    record_id = _claude_record_id(obj, record)
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")

    if kind == "user" and not obj.get("isMeta"):
        if isinstance(content, str):
            text = content.strip()
            if text and not text.lstrip().startswith(
                ("<system-reminder", "<environment_context")
            ):
                events.append(
                    NormalizedEvent(
                        "user_message",
                        timestamp,
                        {
                            "excerpt": _bounded(text),
                            "length": len(text),
                            "message_ref": _message_ref(
                                "claude",
                                session_id,
                                generation,
                                record,
                                f"{record_id}:0",
                            ),
                        },
                        ordinal,
                    )
                )
                ordinal += 1
        elif isinstance(content, list):
            for index, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = _text(block.get("text"))
                    if text and not text.lstrip().startswith(
                        ("<system-reminder", "<environment_context")
                    ):
                        events.append(
                            NormalizedEvent(
                                "user_message",
                                timestamp,
                                {
                                    "excerpt": _bounded(text),
                                    "length": len(text),
                                    "message_ref": _message_ref(
                                        "claude",
                                        session_id,
                                        generation,
                                        record,
                                        f"{record_id}:{index}",
                                    ),
                                },
                                ordinal,
                            )
                        )
                        ordinal += 1
                elif block.get("type") == "tool_result":
                    call_id = _text(block.get("tool_use_id"))
                    if call_id:
                        events.append(
                            NormalizedEvent(
                                "tool_finished",
                                timestamp,
                                {"call_id": call_id},
                                ordinal,
                            )
                        )
                        ordinal += 1
                        answers = _claude_answers(block)
                        if answers:
                            events.append(
                                NormalizedEvent(
                                    "decision_response",
                                    timestamp,
                                    {"request_id": call_id, "answers": answers},
                                    ordinal,
                                )
                            )
                            ordinal += 1

    if kind == "assistant" and not obj.get("isSidechain"):
        for index, text in _content_blocks(content, "text"):
            events.append(
                NormalizedEvent(
                    "assistant_message",
                    timestamp,
                    {
                        "excerpt": _bounded(text),
                        "length": len(text),
                        "message_ref": _message_ref(
                            "claude",
                            session_id,
                            generation,
                            record,
                            f"{record_id}:{index}",
                        ),
                    },
                    ordinal,
                )
            )
            ordinal += 1
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                call_id = _text(block.get("id"))
                name = _text(block.get("name"))
                if call_id:
                    events.append(
                        NormalizedEvent(
                            "tool_started",
                            timestamp,
                            {"call_id": call_id, "name": name},
                            ordinal,
                        )
                    )
                    ordinal += 1
                if name != "AskUserQuestion" or not call_id:
                    continue
                tool_input = block.get("input")
                questions = (
                    tool_input.get("questions")
                    if isinstance(tool_input, dict)
                    else None
                )
                if not isinstance(questions, list):
                    continue
                for question_index, question in enumerate(questions):
                    if not isinstance(question, dict):
                        continue
                    prompt = _text(question.get("question"))
                    if not prompt:
                        continue
                    options: list[dict[str, str]] = []
                    for option in question.get("options") or []:
                        if not isinstance(option, dict):
                            continue
                        label = _text(option.get("label"))
                        if label:
                            options.append(
                                {
                                    "label": _bounded(label),
                                    "description": _bounded(
                                        _text(option.get("description"))
                                    ),
                                }
                            )
                    events.append(
                        NormalizedEvent(
                            "decision_requested",
                            timestamp,
                            {
                                "request_id": call_id,
                                "item_id": str(question_index),
                                "question": _bounded(prompt),
                                "options": options,
                            },
                            ordinal,
                        )
                    )
                    ordinal += 1
    if kind == "system":
        subtype = obj.get("subtype")
        if subtype in {"turn_duration", "turn_complete", "turn_completed"}:
            events.append(
                NormalizedEvent(
                    "turn_completed",
                    timestamp,
                    {"turn_id": str(obj.get("turnId") or record_id)},
                    ordinal,
                )
            )
        elif subtype in {"turn_aborted", "api_error"}:
            events.append(
                NormalizedEvent(
                    "turn_aborted",
                    timestamp,
                    {"turn_id": str(obj.get("turnId") or record_id)},
                    ordinal,
                )
            )

    title = _claude_title(obj)
    if title:
        events.append(
            NormalizedEvent(
                "session_title_changed",
                timestamp,
                {"title": _bounded(title)},
                ordinal,
            )
        )

    recognized = bool(events) or kind in {
        "user",
        "assistant",
        "system",
        "summary",
        "custom-title",
        "title",
        "file-history-snapshot",
        "queue-operation",
        "progress",
        "agent-name",
        "ai-title",
        "attachment",
        "last-prompt",
        "mode",
        "permission-mode",
    }
    return RecordEvents(record, tuple(events), record_cwd("claude", obj), recognized)


def _codex_response_text(payload: dict[str, Any], role: str) -> str:
    text_type = "output_text" if role == "assistant" else "input_text"
    return "\n\n".join(
        text for _index, text in _content_blocks(payload.get("content"), text_type)
    )


def _codex_events(
    record: JsonRecord,
    session_id: str,
    generation: int,
    message_mode: str,
) -> RecordEvents:
    obj = record.value or {}
    top_type = obj.get("type")
    payload = _codex_payload(obj)
    payload_type = payload.get("type")
    timestamp = _text(obj.get("timestamp")) or None
    events: list[NormalizedEvent] = []
    ordinal = 0

    if top_type == "event_msg":
        if message_mode == "display" and payload_type in {
            "user_message",
            "agent_message",
        }:
            text = _text(payload.get("message"))
            if text:
                role = "user" if payload_type == "user_message" else "assistant"
                events.append(
                    NormalizedEvent(
                        f"{role}_message",
                        timestamp,
                        {
                            "excerpt": _bounded(text),
                            "length": len(text),
                            "phase": _text(payload.get("phase")) or None,
                            "message_ref": _message_ref(
                                "codex",
                                session_id,
                                generation,
                                record,
                                _text(payload.get("phase")) or role,
                            ),
                        },
                        ordinal,
                    )
                )
        elif payload_type == "task_started":
            events.append(
                NormalizedEvent(
                    "turn_started",
                    timestamp,
                    {"turn_id": _text(payload.get("turn_id")) or None},
                    ordinal,
                )
            )
        elif payload_type == "task_complete":
            excerpt = _text(payload.get("last_agent_message"))
            events.append(
                NormalizedEvent(
                    "turn_completed",
                    timestamp,
                    {
                        "turn_id": _text(payload.get("turn_id")) or None,
                        "excerpt": _bounded(excerpt) if excerpt else None,
                    },
                    ordinal,
                )
            )
        elif payload_type == "turn_aborted":
            events.append(
                NormalizedEvent(
                    "turn_aborted",
                    timestamp,
                    {"turn_id": _text(payload.get("turn_id")) or None},
                    ordinal,
                )
            )

    if (
        top_type == "response_item"
        and message_mode == "response"
        and payload_type == "message"
        and payload.get("role") in {"user", "assistant"}
    ):
        role = str(payload["role"])
        text = _codex_response_text(payload, role).strip()
        if text and not text.lstrip().startswith(
            ("<environment_context", "<system-reminder")
        ):
            events.append(
                NormalizedEvent(
                    f"{role}_message",
                    timestamp,
                    {
                        "excerpt": _bounded(text),
                        "length": len(text),
                        "phase": _text(payload.get("phase")) or None,
                        "message_ref": _message_ref(
                            "codex",
                            session_id,
                            generation,
                            record,
                            f"fallback-{role}",
                        ),
                    },
                    ordinal,
                )
            )

    if top_type == "response_item":
        call_types = {
            "function_call": "tool_started",
            "custom_tool_call": "tool_started",
            "tool_search_call": "tool_started",
            "function_call_output": "tool_finished",
            "custom_tool_call_output": "tool_finished",
            "tool_search_output": "tool_finished",
        }
        event_kind = call_types.get(str(payload_type))
        if event_kind:
            call_id = _text(payload.get("call_id") or payload.get("id"))
            if call_id:
                events.append(
                    NormalizedEvent(
                        event_kind,
                        timestamp,
                        {
                            "call_id": call_id,
                            "name": _text(payload.get("name")) or None,
                        },
                        len(events),
                    )
                )

    recognized = bool(events) or top_type in {
        "session_meta",
        "turn_context",
        "event_msg",
        "response_item",
        "compacted",
        "inter_agent_communication_metadata",
        "world_state",
    }
    return RecordEvents(record, tuple(events), record_cwd("codex", obj), recognized)


def normalize_records(
    provider: str,
    records: Iterable[JsonRecord],
    *,
    session_id: str,
    generation: int,
    message_mode: str,
) -> tuple[list[RecordEvents], str]:
    records = list(records)
    if provider == "codex":
        detected = detect_codex_message_mode(records)
        if detected == "display":
            message_mode = "display"
        elif message_mode == "unknown" and detected != "unknown":
            message_mode = detected
        return (
            [
                _codex_events(record, session_id, generation, message_mode)
                for record in records
                if record.value is not None
            ],
            message_mode,
        )
    return (
        [
            _claude_events(record, session_id, generation)
            for record in records
            if record.value is not None
        ],
        "display",
    )
