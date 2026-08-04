from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .identity import cwd_matches_project
from .jsonl import read_window
from .providers import normalize_records

if TYPE_CHECKING:
    from .service import Observer


PACKET_VERSION = "observer-interactive-review-v2"
MAX_MESSAGES = 40
MAX_ITEMS = 3
MAX_PACKET_BYTES = 256 * 1024
TAIL_BYTES = 4 * 1024 * 1024
REVIEW_TTL_SECONDS = 60 * 60
MAX_WAIT_SECONDS = 55.0
ACTIVITY_DEBOUNCE_SECONDS = 10 * 60
MIN_SUBSTANTIAL_ASSISTANT_MESSAGES = 2
MIN_USER_MESSAGES = 2
MIN_SUBSTANTIAL_MESSAGE_CHARS = 200
MIN_ASSISTANT_CHARS = 1_200
MAX_EVIDENCE_BLOCK_CHARS = 800
ALLOWED_TYPES = {
    "question",
    "decision",
    "requested_user_action",
    "recommendation",
    "agent_action",
    "informational",
}
ALLOWED_ASSESSMENTS = {
    "no_later_handling_observed",
    "partially_handled",
    "later_handling_found",
    "reported_complete",
    "superseded",
    "declined_in_conversation",
    "deferred_by_commitment",
    "indeterminate",
    "not_actionable",
}
ALLOWED_INTENDED_PARTIES = {"user", "agent", "unknown"}


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _write_private_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(encoded, encoding="utf-8")
    os.chmod(path, 0o600)


def _source_messages(
    source: dict[str, Any], project: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    path = Path(str(source["path"]))
    try:
        size = min(path.stat().st_size, int(source.get("committed_offset") or 0))
        start = max(0, size - TAIL_BYTES)
        window = read_window(path, start, size)
    except OSError:
        return [], [f"{source['provider']}:{source['session_id']} source unavailable"]

    records = [record for record in window.records if record.value is not None]
    batches, _mode = normalize_records(
        str(source["provider"]),
        records,
        session_id=str(source["session_id"]),
        generation=int(source["generation"]),
        message_mode=str(source.get("message_mode") or "unknown"),
    )
    current_matches = cwd_matches_project(source.get("current_cwd"), project)
    messages: list[dict[str, Any]] = []
    for batch in batches:
        if batch.cwd:
            current_matches = cwd_matches_project(batch.cwd, project)
        if not current_matches:
            continue
        for event in batch.events:
            if event.kind not in {"user_message", "assistant_message"}:
                continue
            text = str(event.payload.get("excerpt") or "")
            message_ref = str(event.payload.get("message_ref") or "")
            if not text or not message_ref:
                continue
            messages.append(
                {
                    "message_ref": message_ref,
                    "provider": source["provider"],
                    "session_id": source["session_id"],
                    "role": "user" if event.kind == "user_message" else "assistant",
                    "phase": event.payload.get("phase"),
                    "timestamp": event.source_at,
                    "text": text,
                    "evidence_blocks": _evidence_blocks(message_ref, text),
                    "byte_start": batch.record.start,
                    "byte_end": batch.record.end,
                }
            )
    gaps: list[str] = []
    if window.skipped_prefix:
        gaps.append(
            f"{source['provider']}:{source['session_id']} begins inside a bounded tail"
        )
    if start and not messages:
        gaps.append(
            f"{source['provider']}:{source['session_id']} had no safely bound visible "
            "messages in its bounded tail"
        )
    return messages[-MAX_MESSAGES:], gaps


def _evidence_blocks(message_ref: str, text: str) -> list[dict[str, str]]:
    blocks: list[str] = []
    for line in text.splitlines() or [text]:
        remaining = line.strip()
        while remaining:
            if len(remaining) <= MAX_EVIDENCE_BLOCK_CHARS:
                blocks.append(remaining)
                break
            boundary = remaining.rfind(" ", 0, MAX_EVIDENCE_BLOCK_CHARS + 1)
            if boundary < MAX_EVIDENCE_BLOCK_CHARS // 2:
                boundary = MAX_EVIDENCE_BLOCK_CHARS
            blocks.append(remaining[:boundary])
            remaining = remaining[boundary:].lstrip()
    return [
        {"evidence_ref": f"{message_ref}:e{index}", "text": block}
        for index, block in enumerate(blocks)
    ]


def _source_recency(source: dict[str, Any]) -> tuple[float, str, str]:
    observed = float(source.get("last_observation_at") or 0)
    try:
        modified = Path(str(source["path"])).stat().st_mtime
    except OSError:
        modified = 0
    return max(observed, modified), str(source["provider"]), str(source["session_id"])


def _semantic_input_hash(
    messages: list[dict[str, Any]], gaps: list[str]
) -> str:
    value = {
        "messages": [
            {
                key: message.get(key)
                for key in ("message_ref", "role", "phase", "timestamp", "text")
            }
            for message in messages
        ],
        "gaps": gaps,
    }
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _messages_after_cursor(
    messages: list[dict[str, Any]], cursor: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not cursor or not cursor.get("last_message_ref"):
        return messages
    last_ref = str(cursor["last_message_ref"])
    for index in range(len(messages) - 1, -1, -1):
        if str(messages[index].get("message_ref")) == last_ref:
            return messages[index + 1 :]
    # The accepted boundary may have fallen outside the bounded tail. Keeping the
    # visible tail is safer than silently discarding new activity; packet coverage
    # still discloses that the source began inside a bounded window.
    return messages


def _substantial_activity(
    messages: list[dict[str, Any]], cursor: dict[str, Any] | None
) -> dict[str, int | bool]:
    pending = _messages_after_cursor(messages, cursor)
    assistant = [
        message
        for message in pending
        if message.get("role") == "assistant"
        and len(str(message.get("text") or "").strip())
        >= MIN_SUBSTANTIAL_MESSAGE_CHARS
    ]
    user_count = sum(1 for message in pending if message.get("role") == "user")
    assistant_chars = sum(len(str(message.get("text") or "").strip()) for message in assistant)
    return {
        "eligible": (
            len(assistant) >= MIN_SUBSTANTIAL_ASSISTANT_MESSAGES
            and user_count >= MIN_USER_MESSAGES
            and assistant_chars >= MIN_ASSISTANT_CHARS
        ),
        "pending_messages": len(pending),
        "substantial_assistant_messages": len(assistant),
        "user_messages": user_count,
        "assistant_chars": assistant_chars,
    }


def _expire_review_artifacts(observer: Observer, now: float) -> None:
    cutoff = now - REVIEW_TTL_SECONDS
    observer.db.expire_prepared_reviews(cutoff)
    jobs = observer.config.state_dir / "review-jobs"
    if not jobs.is_dir():
        return
    for path in jobs.glob("review_*.*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def prepare_review(
    observer: Observer,
    project_value: str,
    *,
    analyzer_provider: str,
    analyzer_model: str | None = None,
    exclude_session_id: str | None = None,
    target_session_id: str | None = None,
    target_provider: str | None = None,
    _source_override: dict[str, Any] | None = None,
    _job_id_override: str | None = None,
    _lease_epoch: int | None = None,
    _input_hash_override: str | None = None,
) -> dict[str, Any]:
    if analyzer_provider not in {"claude", "codex"}:
        raise ValueError("analyzer provider must be claude or codex")
    if target_provider not in {None, "claude", "codex"}:
        raise ValueError("target provider must be claude or codex")
    if target_provider and not target_session_id:
        raise ValueError("target provider requires a target session ID")
    project = observer._resolve_project(project_value)
    status = observer.status()
    projected = next(
        item
        for item in status["projects"]
        if item["project_id"] == project["project_id"]
    )
    created_at = time.time()
    _expire_review_artifacts(observer, created_at)
    coverage_gaps: list[str] = []
    source_checkpoints: list[dict[str, Any]] = []
    sources = (
        [_source_override]
        if _source_override is not None
        else [
            source
            for source in observer.db.sources()
            if source.get("current_project_id") == project["project_id"]
            and not (
                source["provider"] == analyzer_provider
                and str(source["session_id"]) == exclude_session_id
            )
        ]
    )
    if target_session_id:
        sources = [
            source
            for source in sources
            if str(source["session_id"]) == target_session_id
            and (target_provider is None or source["provider"] == target_provider)
        ]
        if not sources:
            raise ValueError(
                f"target session is not available for this project: {target_session_id}"
            )
        providers = {str(candidate["provider"]) for candidate in sources}
        if len(providers) > 1:
            raise ValueError(
                "target session ID exists for multiple providers; specify target provider"
            )
    source = max(sources, key=_source_recency) if sources else None
    messages: list[dict[str, Any]] = []
    if source:
        messages, coverage_gaps = _source_messages(source, project)
        source_checkpoints.append(
            {
                "source_id": source["source_id"],
                "generation": source["generation"],
                "committed_offset": source["committed_offset"],
            }
        )
    elif exclude_session_id:
        coverage_gaps.append(
            f"{analyzer_provider}:{exclude_session_id} is the invoking analyzer "
            "session and was excluded; no other session was available"
        )
    else:
        coverage_gaps.append("no visible worker session was available for this project")

    packet = {
        "schema_version": PACKET_VERSION,
        "purpose": (
            "Identify evidence-backed possible loose ends in visible conversation. "
            "Do not infer project truth or worker intent."
        ),
        "project": {
            "project_id": project["project_id"],
            "display_path": project["display_path"],
            "resolved_path": project["resolved_path"],
            "current_branch": project.get("current_branch"),
        },
        "analyzer": {
            "provider": analyzer_provider,
            "model": analyzer_model,
            "mode": "interactive-session",
            "isolation": "current Claude/Codex session, not an isolated analyzer",
            "excluded_session_id": exclude_session_id,
            "lease_epoch": _lease_epoch,
        },
        "target_session": (
            {
                "provider": source["provider"],
                "session_id": source["session_id"],
                "source_id": source["source_id"],
            }
            if source
            else None
        ),
        "factual_findings": [
            {
                "finding_id": finding["finding_id"],
                "provider": finding["provider"],
                "session_id": finding["session_id"],
                "kind": finding["kind"],
                "summary": finding["summary"],
                "details": finding["details"],
            }
            for finding in projected["findings"][:20]
            if source is not None
            and finding["provider"] == source["provider"]
            and finding["session_id"] == source["session_id"]
        ],
        "messages": messages,
        "coverage": {
            "message_count": len(messages),
            "message_limit": MAX_MESSAGES,
            "tail_bytes_for_target_source": TAIL_BYTES,
            "gaps": coverage_gaps,
            "negative_assessment_blocked": any(
                "begins inside a bounded tail" not in gap
                for gap in coverage_gaps
            ),
            "source_checkpoints": source_checkpoints,
        },
        "response_schema": {
            "schema_version": PACKET_VERSION,
            "summary": "string",
            "items": [
                {
                    "type": "one allowed item type",
                    "assessment": "one allowed assessment",
                    "intended_party": "user, agent, or unknown",
                    "title": "short model wording",
                    "detail": "bounded explanation",
                    "session_id": "cited session",
                    "message_ref": "cited origin message_ref",
                    "evidence_ref": "one supplied evidence_blocks[].evidence_ref",
                }
            ],
            "limitations": ["string"],
        },
    }
    while messages and len(json.dumps(packet).encode("utf-8")) > MAX_PACKET_BYTES:
        messages.pop(0)
    packet["coverage"]["message_count"] = len(messages)

    input_hash = _input_hash_override or _semantic_input_hash(
        messages, coverage_gaps
    )

    job_id = _job_id_override or "review_" + uuid.uuid4().hex
    jobs = observer.config.state_dir / "review-jobs"
    _private_directory(jobs)
    packet_path = jobs / f"{job_id}.packet.json"
    draft_path = jobs / f"{job_id}.review.json"
    _write_private_json(packet_path, packet)
    refs = [str(message["message_ref"]) for message in messages]
    packet_meta = {
        "schema_version": PACKET_VERSION,
        "message_count": len(messages),
        "message_refs": refs,
        "packet_sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
        "target_session": packet["target_session"],
        "coverage": packet["coverage"],
        "input_hash": input_hash,
        "lease_epoch": _lease_epoch,
    }
    observer.db.create_review_job(
        job_id=job_id,
        project_id=str(project["project_id"]),
        analyzer_provider=analyzer_provider,
        analyzer_model=analyzer_model,
        created_at=created_at,
        packet_meta=packet_meta,
        source_id=str(source["source_id"]) if source and _lease_epoch is not None else None,
        source_generation=int(source["generation"])
        if source and _lease_epoch is not None
        else None,
        source_offset=int(source["committed_offset"])
        if source and _lease_epoch is not None
        else None,
        input_hash=input_hash if source and _lease_epoch is not None else None,
        lease_epoch=_lease_epoch,
    )
    return {
        "job_id": job_id,
        "packet_path": str(packet_path),
        "draft_path": str(draft_path),
        "packet": packet,
        "input_hash": input_hash,
    }


def _prepared_job_result(observer: Observer, job_id: str) -> dict[str, Any]:
    job = observer.db.review_job(job_id)
    jobs = observer.config.state_dir / "review-jobs"
    packet_path = jobs / f"{job_id}.packet.json"
    draft_path = jobs / f"{job_id}.review.json"
    try:
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError(f"prepared review packet is unavailable: {exc}") from exc
    return {
        "job_id": job_id,
        "packet_path": str(packet_path),
        "draft_path": str(draft_path),
        "packet": packet,
        "input_hash": json.loads(str(job["packet_meta_json"])).get("input_hash"),
    }


def _candidate_order(
    observer: Observer, source: dict[str, Any]
) -> tuple[int, float, float, str]:
    cursor = observer.db.analysis_cursor(str(source["source_id"]))
    if cursor is None:
        return (0, 0.0, -_source_recency(source)[0], str(source["source_id"]))
    return (
        1,
        float(cursor.get("accepted_at") or 0),
        -_source_recency(source)[0],
        str(source["source_id"]),
    )


def _prepare_next_once(
    observer: Observer, lease_token: str
) -> dict[str, Any] | None:
    lease = observer.db.supervisor_lease(lease_token)
    current_job_id = lease.get("current_job_id")
    if current_job_id:
        job = observer.db.review_job(str(current_job_id))
        if (
            job.get("status") == "prepared"
            and int(job.get("lease_epoch") or -1) == int(lease["epoch"])
        ):
            observer.db.heartbeat_supervisor(
                lease_token, state="analyzing", current_job_id=str(current_job_id)
            )
            return _prepared_job_result(observer, str(current_job_id))

    sources = [
        source
        for source in observer.db.sources(monitoring_only=True)
        if source.get("current_project_id")
        and (
            bool(lease.get("allow_cross_provider"))
            or source.get("provider") == lease.get("provider")
        )
    ]
    sources.sort(key=lambda source: _candidate_order(observer, source))
    now = time.time()
    for source in sources:
        source_id = str(source["source_id"])
        cursor = observer.db.analysis_cursor(source_id)
        generation = int(source["generation"])
        committed_offset = int(source["committed_offset"])
        if cursor and (
            int(cursor["generation"]), int(cursor["committed_offset"])
        ) == (generation, committed_offset):
            continue
        try:
            modified = Path(str(source["path"])).stat().st_mtime
        except OSError:
            continue
        if now - modified < ACTIVITY_DEBOUNCE_SECONDS:
            continue
        project = observer.db.project(str(source["current_project_id"]))
        messages, gaps = _source_messages(source, project)
        input_hash = _semantic_input_hash(messages, gaps)
        last_message_ref = (
            str(messages[-1]["message_ref"]) if messages else None
        )
        if not messages or (cursor and cursor.get("input_hash") == input_hash):
            observer.db.advance_analysis_cursor(
                source_id=source_id,
                generation=generation,
                committed_offset=committed_offset,
                input_hash=input_hash,
                last_message_ref=last_message_ref,
                job_id=cursor.get("job_id") if cursor else None,
            )
            continue
        activity = _substantial_activity(messages, cursor)
        if not activity["eligible"]:
            # Do not advance the accepted cursor: deterministic collection keeps
            # accumulating this exchange until it reaches review-worthy volume.
            continue
        identity = "\x1f".join(
            (
                source_id,
                str(generation),
                str(committed_offset),
                input_hash,
                str(lease["provider"]),
                str(lease.get("model") or ""),
                PACKET_VERSION,
            )
        )
        job_id = "review_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        prepared = prepare_review(
            observer,
            str(project["project_id"]),
            analyzer_provider=str(lease["provider"]),
            analyzer_model=lease.get("model"),
            exclude_session_id=lease.get("session_id"),
            _source_override=source,
            _job_id_override=job_id,
            _lease_epoch=int(lease["epoch"]),
            _input_hash_override=input_hash,
        )
        prepared["packet"]["coverage"]["activity_gate"] = activity
        _write_private_json(Path(prepared["packet_path"]), prepared["packet"])
        packet_meta = json.loads(
            str(observer.db.review_job(job_id)["packet_meta_json"])
        )
        packet_meta["coverage"] = prepared["packet"]["coverage"]
        packet_meta["packet_sha256"] = hashlib.sha256(
            Path(prepared["packet_path"]).read_bytes()
        ).hexdigest()
        observer.db.update_review_packet_meta(job_id, packet_meta)
        observer.db.heartbeat_supervisor(
            lease_token, state="analyzing", current_job_id=job_id
        )
        return prepared
    return None


def next_review(
    observer: Observer,
    lease_token: str,
    *,
    wait_seconds: float = 0,
) -> dict[str, Any]:
    if wait_seconds < 0 or wait_seconds > MAX_WAIT_SECONDS:
        raise ValueError(f"--wait must be between 0 and {int(MAX_WAIT_SECONDS)} seconds")
    deadline = time.monotonic() + wait_seconds
    while True:
        prepared = _prepare_next_once(observer, lease_token)
        if prepared is not None:
            return {
                "state": "work",
                "review": prepared,
                "supervisor": observer.db.supervisor_status(),
            }
        observer.db.heartbeat_supervisor(lease_token, state="waiting")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "state": "waiting",
                "review": None,
                "supervisor": observer.db.supervisor_status(),
            }
        time.sleep(min(0.5, remaining))


def _bounded_string(value: Any, field: str, limit: int, *, required: bool) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{field} must be a string")
        return ""
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} must not be empty")
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return text


def submit_review(
    observer: Observer,
    job_id: str,
    draft_path: str | Path,
    *,
    lease_token: str | None = None,
) -> dict[str, Any]:
    job = observer.db.review_job(job_id)
    if job["status"] != "prepared":
        raise ValueError("review job is not awaiting a submission")
    path = Path(draft_path).expanduser().resolve()
    expected_dir = (observer.config.state_dir / "review-jobs").resolve()
    if path.parent != expected_dir or path.name != f"{job_id}.review.json":
        raise ValueError("review draft path is not the prepared observer-owned path")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"review draft is unavailable: {exc}") from exc
    if len(raw) > MAX_PACKET_BYTES:
        raise ValueError("review draft exceeds 256 KiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("review draft is not valid JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != PACKET_VERSION:
        raise ValueError(f"review draft must use {PACKET_VERSION}")

    packet_path = expected_dir / f"{job_id}.packet.json"
    try:
        packet_bytes = packet_path.read_bytes()
        packet = json.loads(packet_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("prepared review packet is unavailable") from exc
    packet_meta = json.loads(str(job["packet_meta_json"]))
    actual_digest = hashlib.sha256(packet_bytes).hexdigest()
    if actual_digest != packet_meta.get("packet_sha256"):
        raise ValueError("prepared review packet failed its integrity check")
    messages = {
        str(message["message_ref"]): message for message in packet.get("messages", [])
    }
    evidence_blocks = {
        str(block["evidence_ref"]): (message, str(block["text"]))
        for message in packet.get("messages", [])
        for block in message.get("evidence_blocks", [])
        if isinstance(block, dict)
        and isinstance(block.get("evidence_ref"), str)
        and isinstance(block.get("text"), str)
    }
    summary = _bounded_string(value.get("summary"), "summary", 4096, required=True)
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise ValueError(f"items must be a list containing at most {MAX_ITEMS} entries")
    coverage = packet.get("coverage", {})
    incomplete = bool(
        coverage.get(
            "negative_assessment_blocked", bool(coverage.get("gaps"))
        )
    )
    items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be an object")
        item_type = str(item.get("type") or "")
        assessment = str(item.get("assessment") or "")
        if item_type not in ALLOWED_TYPES:
            raise ValueError(f"items[{index}].type is not allowed")
        if assessment not in ALLOWED_ASSESSMENTS:
            raise ValueError(f"items[{index}].assessment is not allowed")
        intended_party = str(
            item.get("intended_party")
            or (
                "user"
                if item_type in {"question", "decision", "requested_user_action"}
                else "agent"
                if item_type == "agent_action"
                else "unknown"
            )
        )
        if intended_party not in ALLOWED_INTENDED_PARTIES:
            raise ValueError(f"items[{index}].intended_party is not allowed")
        if incomplete and assessment in {
            "no_later_handling_observed",
            "partially_handled",
            "deferred_by_commitment",
        }:
            raise ValueError(
                f"items[{index}] must be indeterminate when packet coverage has gaps"
            )
        message_ref = _bounded_string(
            item.get("message_ref"), f"items[{index}].message_ref", 1024, required=True
        )
        cited = messages.get(message_ref)
        if not cited:
            raise ValueError(f"items[{index}] cites a message outside the packet")
        session_id = _bounded_string(
            item.get("session_id"), f"items[{index}].session_id", 256, required=True
        )
        if session_id != str(cited["session_id"]):
            raise ValueError(f"items[{index}] session does not match its citation")
        evidence_ref = _bounded_string(
            item.get("evidence_ref"),
            f"items[{index}].evidence_ref",
            2048,
            required=False,
        )
        if evidence_ref:
            evidence_entry = evidence_blocks.get(evidence_ref)
            if not evidence_entry or evidence_entry[0] is not cited:
                raise ValueError(
                    f"items[{index}] cites an evidence block outside its message"
                )
            evidence = evidence_entry[1]
        else:
            # Manual v2 drafts may still supply a literal exact excerpt. Automatic
            # analyzers use evidence_ref so paraphrased quotations cannot enter.
            evidence = _bounded_string(
                item.get("evidence_excerpt"),
                f"items[{index}].evidence_excerpt",
                2048,
                required=True,
            )
            if evidence not in str(cited["text"]):
                raise ValueError(
                    f"items[{index}] evidence is absent from its cited message"
                )
        items.append(
            {
                "type": item_type,
                "assessment": assessment,
                "intended_party": intended_party,
                "title": _bounded_string(
                    item.get("title"), f"items[{index}].title", 300, required=True
                ),
                "detail": _bounded_string(
                    item.get("detail"), f"items[{index}].detail", 2000, required=True
                ),
                "provider": cited["provider"],
                "session_id": session_id,
                "message_ref": message_ref,
                "evidence_ref": evidence_ref or None,
                "evidence_excerpt": evidence,
                "timestamp": cited.get("timestamp"),
            }
        )
    raw_limitations = value.get("limitations", [])
    if not isinstance(raw_limitations, list) or len(raw_limitations) > 20:
        raise ValueError("limitations must be a list containing at most 20 entries")
    limitations = [
        _bounded_string(item, f"limitations[{index}]", 1000, required=True)
        for index, item in enumerate(raw_limitations)
    ]
    submitted_at = time.time()
    if job.get("lease_epoch") is not None:
        if not lease_token:
            raise ValueError("supervised review submission requires its lease token")
        last_message_ref = (
            str(packet["messages"][-1]["message_ref"])
            if packet.get("messages")
            else None
        )
        observer.db.submit_supervised_review_job(
            job_id,
            lease_token=lease_token,
            submitted_at=submitted_at,
            summary=summary,
            items=items,
            limitations=limitations,
            last_message_ref=last_message_ref,
        )
    else:
        observer.db.submit_review_job(
            job_id,
            submitted_at=submitted_at,
            summary=summary,
            items=items,
            limitations=limitations,
        )
    for temporary in (path, packet_path):
        try:
            temporary.unlink()
        except OSError:
            pass
    return {"job_id": job_id, "status": "current", "items": len(items)}
