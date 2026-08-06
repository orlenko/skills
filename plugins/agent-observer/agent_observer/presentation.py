from __future__ import annotations

from typing import Any

from .attention import (
    OBSERVED_KIND_ORDER,
    actionable_findings,
    actionable_review_items,
    review_item_fingerprint,
    suggested_review_items,
)

REVIEW_TYPE_ORDER = {"decision": 0, "question": 1, "requested_user_action": 2}
OBSERVED_FALLBACK_TITLES = {
    "decision_requested": "Decision requested",
    "input_requested": "Input requested",
    "no_completion_observed": "No completion observed",
}


def _activity(project: dict[str, Any]) -> tuple[str, float | None]:
    ages = [
        float(session["activity_age_seconds"])
        for session in project["sessions"]
        if session.get("activity_age_seconds") is not None
    ]
    if not ages:
        return "unknown", None
    age = min(ages)
    if age <= 5 * 60:
        return "recent", age
    if age <= 30 * 60:
        return "quiet", age
    return "stale", age


def _health(project: dict[str, Any]) -> str:
    node = project.get("node")
    if isinstance(node, dict) and node.get("transport_state") != "connected":
        return "unavailable"
    states = {
        str(source.get("health") or "unavailable") for source in project["sources"]
    }
    if not states or "unavailable" in states:
        return "unavailable"
    if "degraded" in states:
        return "degraded"
    return "healthy"


def _review(project: dict[str, Any]) -> dict[str, Any] | None:
    review = project.get("review")
    if not review:
        return None
    safe = {
        key: review.get(key)
        for key in (
            "job_id",
            "analyzer_provider",
            "analyzer_model",
            "status",
            "created_at",
            "submitted_at",
            "summary",
            "items",
            "limitations",
            "error",
            "dismissed_at",
        )
    }
    checkpoints = {
        str(item["source_id"]): item
        for item in review.get("packet_meta", {})
        .get("coverage", {})
        .get("source_checkpoints", [])
    }
    for source in project["sources"]:
        captured = checkpoints.get(str(source["source_id"]))
        if not captured:
            continue
        if int(captured["generation"]) != int(source["generation"]) or int(
            captured["committed_offset"]
        ) != int(source["committed_offset"]):
            safe["status"] = "stale"
            break
    safe["coverage"] = review.get("packet_meta", {}).get("coverage", {})
    safe["target_session"] = review.get("packet_meta", {}).get("target_session")
    return safe


def _observed_fallback(finding: dict[str, Any]) -> str:
    return OBSERVED_FALLBACK_TITLES.get(
        str(finding.get("kind")), "Attention requested"
    )


def _attention_items(
    project: dict[str, Any], review: dict[str, Any] | None
) -> list[dict[str, Any]]:
    observed = [
        {
            "attention_id": f"finding:{finding['finding_id']}",
            "truth": "observed",
            "type": str(finding["kind"]),
            "title": str(finding.get("summary") or _observed_fallback(finding)),
            "detail": str(finding.get("summary") or _observed_fallback(finding)),
            "provider": finding.get("provider"),
            "session_id": finding.get("session_id"),
            "requested_at": finding.get("created_at"),
            "updated_at": finding.get("updated_at"),
            "details": finding.get("details") or {},
            "finding_id": finding.get("finding_id"),
            "dismiss_kind": "finding",
            "dismiss_ref": finding.get("finding_id"),
        }
        for finding in sorted(
            actionable_findings(project),
            key=lambda item: (
                OBSERVED_KIND_ORDER.get(str(item.get("kind")), 9),
                -float(item.get("updated_at") or 0),
            ),
        )
    ]
    if project.get("remote_attention_dismissed"):
        return observed
    dismissed = set(project.get("dismissed_attention") or [])
    model: list[dict[str, Any]] = []
    for index, item in enumerate((review or {}).get("items") or []):
        if item not in actionable_review_items(review):
            continue
        fingerprint = review_item_fingerprint(review or {}, item)
        if fingerprint in dismissed:
            continue
        model.append(
            {
                "attention_id": f"review:{review.get('job_id')}:{index}",
                "truth": "model",
                "type": item.get("type"),
                "title": item.get("title"),
                "detail": item.get("detail"),
                "provider": item.get("provider") or review.get("analyzer_provider"),
                "session_id": item.get("session_id"),
                "requested_at": item.get("timestamp"),
                "updated_at": item.get("timestamp") or review.get("submitted_at"),
                "assessment": item.get("assessment"),
                "intended_party": item.get("intended_party") or "user",
                "evidence_excerpt": item.get("evidence_excerpt"),
                "message_ref": item.get("message_ref"),
                "review_item_index": index,
                "review_job_id": review.get("job_id"),
                "review_status": review.get("status"),
                "dismiss_kind": "review",
                "dismiss_ref": fingerprint,
            }
        )
    model.sort(
        key=lambda item: REVIEW_TYPE_ORDER.get(str(item.get("type")), 9)
    )
    return observed + model


def dashboard_projection(raw: dict[str, Any]) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    view_counts = {
        "needs_a_look": 0,
        "review_suggested": 0,
        "recently_active": 0,
        "quiet": 0,
        "stale": 0,
        "observer_issues": 0,
    }
    for project in raw["projects"]:
        activity, activity_age = _activity(project)
        health = _health(project)
        review = _review(project)
        attention_items = _attention_items(project, review)
        attention = attention_items[0] if attention_items else None
        dismissed = set(project.get("dismissed_attention") or [])
        suggested = (
            []
            if project.get("remote_attention_dismissed")
            else [
                item
                for item in suggested_review_items(review)
                if review_item_fingerprint(review or {}, item) not in dismissed
            ]
        )
        views: list[str] = []
        if attention:
            views.append("needs_a_look")
        if suggested:
            views.append("review_suggested")
        if activity == "recent" and not attention:
            views.append("recently_active")
        if activity == "quiet":
            views.append("quiet")
        if activity == "stale":
            views.append("stale")
        if health != "healthy":
            views.append("observer_issues")
        for view in views:
            view_counts[view] += 1
        projects.append(
            {
                **project,
                "facets": {
                    "attention": attention["type"] if attention else "none",
                    "activity": activity,
                    "activity_age_seconds": activity_age,
                    "health": health,
                    "continuity": (review or {}).get("status", "not_analyzed"),
                    "prominent_review_items": sum(
                        1 for item in attention_items if item["truth"] == "model"
                    ),
                    "suggested_review_items": len(suggested),
                },
                "primary_attention": attention,
                "attention_items": attention_items,
                "primary_finding": (
                    next(
                        (
                            finding
                            for finding in project["findings"]
                            if attention
                            and attention.get("finding_id") == finding.get("finding_id")
                        ),
                        None,
                    )
                ),
                "review": review,
                "views": views,
            }
        )
    return {
        "schema_version": "agent-observer-dashboard-v1",
        "generated_at": raw["generated_at"],
        "view_counts": view_counts,
        "projects": projects,
        "remote_nodes": raw.get("remote_nodes", []),
    }
