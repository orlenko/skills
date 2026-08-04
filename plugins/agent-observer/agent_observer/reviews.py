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


PACKET_VERSION = "observer-interactive-review-v1"
MAX_MESSAGES = 40
MAX_ITEMS = 3
MAX_PACKET_BYTES = 256 * 1024
TAIL_BYTES = 4 * 1024 * 1024
REVIEW_TTL_SECONDS = 60 * 60
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
        size = path.stat().st_size
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
    current_matches = False
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


def _source_recency(source: dict[str, Any]) -> tuple[float, str, str]:
    observed = float(source.get("last_observation_at") or 0)
    try:
        modified = Path(str(source["path"])).stat().st_mtime
    except OSError:
        modified = 0
    return max(observed, modified), str(source["provider"]), str(source["session_id"])


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
    sources = [
        source
        for source in observer.db.sources()
        if source.get("current_project_id") == project["project_id"]
        and not (
            source["provider"] == analyzer_provider
            and str(source["session_id"]) == exclude_session_id
        )
    ]
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
            "source_checkpoints": source_checkpoints,
        },
        "response_schema": {
            "schema_version": PACKET_VERSION,
            "summary": "string",
            "items": [
                {
                    "type": "one allowed item type",
                    "assessment": "one allowed assessment",
                    "title": "short model wording",
                    "detail": "bounded explanation",
                    "session_id": "cited session",
                    "message_ref": "cited origin message_ref",
                    "evidence_excerpt": "exact substring from cited message",
                }
            ],
            "limitations": ["string"],
        },
    }
    while messages and len(json.dumps(packet).encode("utf-8")) > MAX_PACKET_BYTES:
        messages.pop(0)
    packet["coverage"]["message_count"] = len(messages)

    job_id = "review_" + uuid.uuid4().hex
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
    }
    observer.db.create_review_job(
        job_id=job_id,
        project_id=str(project["project_id"]),
        analyzer_provider=analyzer_provider,
        analyzer_model=analyzer_model,
        created_at=created_at,
        packet_meta=packet_meta,
    )
    return {
        "job_id": job_id,
        "packet_path": str(packet_path),
        "draft_path": str(draft_path),
        "packet": packet,
    }


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
    observer: Observer, job_id: str, draft_path: str | Path
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
    summary = _bounded_string(value.get("summary"), "summary", 4096, required=True)
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise ValueError(f"items must be a list containing at most {MAX_ITEMS} entries")
    incomplete = bool(packet.get("coverage", {}).get("gaps"))
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
                "title": _bounded_string(
                    item.get("title"), f"items[{index}].title", 300, required=True
                ),
                "detail": _bounded_string(
                    item.get("detail"), f"items[{index}].detail", 2000, required=True
                ),
                "provider": cited["provider"],
                "session_id": session_id,
                "message_ref": message_ref,
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
    observer.db.submit_review_job(
        job_id,
        submitted_at=time.time(),
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
