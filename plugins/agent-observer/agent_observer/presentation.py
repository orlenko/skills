from __future__ import annotations

from typing import Any


ATTENTION_ORDER = {
    "decision_requested": 0,
    "turn_aborted": 1,
    "turn_completed": 2,
    "no_completion_observed": 3,
}
PROMINENT_ASSESSMENTS = {
    "no_later_handling_observed",
    "partially_handled",
    "deferred_by_commitment",
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
        unseen = [finding for finding in project["findings"] if not finding["seen"]]
        attention = min(
            unseen,
            key=lambda item: (
                ATTENTION_ORDER.get(str(item["kind"]), 99),
                -float(item["updated_at"]),
            ),
            default=None,
        )
        review = _review(project)
        prominent = [
            item
            for item in (review or {}).get("items") or []
            if item.get("assessment") in PROMINENT_ASSESSMENTS
        ]
        views: list[str] = []
        if attention:
            views.append("needs_a_look")
        if prominent:
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
                    "attention": attention["kind"] if attention else "none",
                    "activity": activity,
                    "activity_age_seconds": activity_age,
                    "health": health,
                    "continuity": (review or {}).get("status", "not_analyzed"),
                    "prominent_review_items": len(prominent),
                },
                "primary_finding": attention,
                "review": review,
                "views": views,
            }
        )
    return {
        "schema_version": "agent-observer-dashboard-v1",
        "generated_at": raw["generated_at"],
        "view_counts": view_counts,
        "projects": projects,
    }
