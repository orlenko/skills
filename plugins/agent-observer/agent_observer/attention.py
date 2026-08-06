from __future__ import annotations

import hashlib
import json
from typing import Any


ACTIONABLE_FINDING_KINDS = {
    "decision_requested",
    "input_requested",
    "no_completion_observed",
}
# A direct ask outranks an inference drawn from silence, even though both are
# observed facts. Ordering keeps the weaker claim from burying the stronger one.
OBSERVED_KIND_ORDER = {
    "decision_requested": 0,
    "input_requested": 1,
    "no_completion_observed": 5,
}
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


def _digest(identity: tuple[Any, ...]) -> str:
    return hashlib.sha256(
        json.dumps(identity, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def finding_identity(finding: dict[str, Any]) -> tuple[Any, ...]:
    return (
        "finding",
        str(finding.get("finding_id") or ""),
        float(finding.get("updated_at") or 0),
    )


def review_item_identity(
    review: dict[str, Any], item: dict[str, Any]
) -> tuple[Any, ...]:
    return (
        "review",
        str(review.get("job_id") or ""),
        str(item.get("message_ref") or ""),
        str(item.get("type") or ""),
        str(item.get("assessment") or ""),
    )


def review_item_fingerprint(review: dict[str, Any], item: dict[str, Any]) -> str:
    """Content-address one reviewed loose end.

    Dismissal stores this hash rather than a row keyed by position, so an item
    resurfaces exactly when its substance changes and never merely because a
    later review renumbered it.
    """
    return _digest(review_item_identity(review, item))


def attention_identities(project: dict[str, Any]) -> list[tuple[Any, ...]]:
    identities = [finding_identity(item) for item in actionable_findings(project)]
    review = project.get("review")
    if isinstance(review, dict):
        identities.extend(
            review_item_identity(review, item)
            for item in actionable_review_items(review)
        )
    return identities


def attention_fingerprint(project: dict[str, Any]) -> str:
    """Identify the current human-attention set for home-side remote dismissal."""
    current = attention_identities(project)
    if not current:
        return ""
    current.sort()
    return _digest(tuple(current))
