from __future__ import annotations

import hashlib
import json
from typing import Any


ACTIONABLE_FINDING_KINDS = {"decision_requested"}
HUMAN_REVIEW_TYPES = {"question", "decision", "requested_user_action"}
IMMEDIATE_REVIEW_ASSESSMENTS = {
    "no_later_handling_observed",
    "partially_handled",
}
SUGGESTED_REVIEW_ASSESSMENTS = IMMEDIATE_REVIEW_ASSESSMENTS | {
    "deferred_by_commitment",
}


def _intended_for_user(item: dict[str, Any]) -> bool:
    intended_party = item.get("intended_party")
    if intended_party is not None:
        return intended_party == "user"
    # Reviews accepted before intended_party was introduced used the item type as
    # the only audience signal. Preserve those useful decisions and questions.
    return item.get("type") in HUMAN_REVIEW_TYPES


def actionable_findings(project: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        finding
        for finding in project.get("findings") or []
        if isinstance(finding, dict)
        and finding.get("kind") in ACTIONABLE_FINDING_KINDS
        and finding.get("state") == "open"
        and not finding.get("seen")
    ]


def actionable_review_items(review: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not review or review.get("dismissed_at"):
        return []
    if review.get("status") not in {"current", "stale"}:
        return []
    return [
        item
        for item in review.get("items") or []
        if isinstance(item, dict)
        and item.get("type") in HUMAN_REVIEW_TYPES
        and item.get("assessment") in IMMEDIATE_REVIEW_ASSESSMENTS
        and _intended_for_user(item)
    ]


def suggested_review_items(review: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not review or review.get("dismissed_at"):
        return []
    if review.get("status") not in {"current", "stale"}:
        return []
    return [
        item
        for item in review.get("items") or []
        if isinstance(item, dict)
        and item.get("assessment") in SUGGESTED_REVIEW_ASSESSMENTS
        and _intended_for_user(item)
    ]


def attention_fingerprint(project: dict[str, Any]) -> str:
    """Identify the current human-attention set for home-side remote dismissal."""
    current: list[tuple[Any, ...]] = [
        (
            "finding",
            str(item.get("finding_id") or ""),
            float(item.get("updated_at") or 0),
        )
        for item in actionable_findings(project)
    ]
    review = project.get("review")
    if isinstance(review, dict):
        for item in actionable_review_items(review):
            current.append(
                (
                    "review",
                    str(review.get("job_id") or ""),
                    str(item.get("message_ref") or ""),
                    str(item.get("type") or ""),
                    str(item.get("assessment") or ""),
                )
            )
    if not current:
        return ""
    current.sort()
    return hashlib.sha256(
        json.dumps(current, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
