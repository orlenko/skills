from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.attention import (  # noqa: E402
    attention_fingerprint,
    review_item_fingerprint,
)
from agent_observer.presentation import dashboard_projection  # noqa: E402
from agent_observer.service import Observer, ObserverConfig  # noqa: E402
from agent_observer.signals import append_signal, build_signal  # noqa: E402


def iso(moment: float) -> str:
    return (
        datetime.fromtimestamp(moment, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def review():
    return {
        "job_id": "review-1",
        "analyzer_provider": "codex",
        "status": "current",
        "submitted_at": 990,
        "summary": "Two loose ends remain.",
        "items": [
            {
                "type": "question",
                "assessment": "no_later_handling_observed",
                "intended_party": "user",
                "title": "Clarify the reviewer fleet",
                "detail": "Ambiguous PR target.",
                "message_ref": "message:question",
                "evidence_excerpt": "The fleet was not run.",
                "timestamp": "2026-08-04T12:00:00Z",
            },
            {
                "type": "decision",
                "assessment": "no_later_handling_observed",
                "intended_party": "user",
                "title": "Decide whether to rerun validators",
                "detail": "Forty-four overflowed.",
                "message_ref": "message:decision",
                "evidence_excerpt": "44 validators overflowed.",
                "timestamp": "2026-08-04T12:01:00Z",
            },
        ],
        "limitations": [],
        "packet_meta": {"coverage": {}, "target_session": None},
    }


def project(**overrides):
    base = {
        "project_id": "project-1",
        "display_path": "~/code/ops2",
        "resolved_path": "/home/vlad/code/ops2",
        "sessions": [{"activity_age_seconds": 5, "last_activity_at": 995}],
        "sources": [
            {
                "source_id": "source-1",
                "health": "healthy",
                "generation": 1,
                "committed_offset": 100,
            }
        ],
        "findings": [
            {
                "finding_id": "decision:codex:worker:request-1",
                "provider": "codex",
                "session_id": "worker",
                "kind": "decision_requested",
                "state": "open",
                "seen": 0,
                "created_at": 700,
                "updated_at": 999,
                "summary": "Choose the deployment window",
                "details": {"items": {}},
            }
        ],
        "changes": [],
        "review": review(),
        "dismissed_attention": [],
    }
    base.update(overrides)
    return base


def projected(value):
    return dashboard_projection({"generated_at": 1000, "projects": [value]})[
        "projects"
    ][0]


class PerItemDismissalTests(unittest.TestCase):
    def test_every_item_carries_the_reference_needed_to_dismiss_only_itself(self):
        items = projected(project())["attention_items"]
        self.assertEqual(len(items), 3)
        observed = [item for item in items if item["truth"] == "observed"]
        model = [item for item in items if item["truth"] == "model"]
        self.assertEqual(observed[0]["dismiss_kind"], "finding")
        self.assertEqual(observed[0]["dismiss_ref"], "decision:codex:worker:request-1")
        self.assertTrue(all(item["dismiss_kind"] == "review" for item in model))
        self.assertEqual(len({item["dismiss_ref"] for item in model}), 2)

    def test_dismissing_one_reviewed_item_leaves_its_siblings_standing(self):
        current = review()
        target = review_item_fingerprint(current, current["items"][0])
        result = projected(project(dismissed_attention=[target]))
        titles = [item["title"] for item in result["attention_items"]]
        self.assertNotIn("Clarify the reviewer fleet", titles)
        self.assertIn("Decide whether to rerun validators", titles)
        self.assertIn("needs_a_look", result["views"])
        self.assertEqual(result["facets"]["prominent_review_items"], 1)

    def test_a_dismissed_item_stays_dismissed_while_its_substance_holds(self):
        current = review()
        target = review_item_fingerprint(current, current["items"][0])
        moved = review()
        moved["items"] = list(reversed(moved["items"]))
        self.assertEqual(review_item_fingerprint(moved, moved["items"][1]), target)

    def test_a_changed_assessment_resurfaces_the_item(self):
        current = review()
        target = review_item_fingerprint(current, current["items"][0])
        escalated = review()
        escalated["items"][0]["assessment"] = "partially_handled"
        self.assertNotEqual(
            review_item_fingerprint(escalated, escalated["items"][0]), target
        )
        titles = [
            item["title"]
            for item in projected(
                project(review=escalated, dismissed_attention=[target])
            )["attention_items"]
        ]
        self.assertIn("Clarify the reviewer fleet", titles)

    def test_dismissing_the_last_item_clears_the_project_from_the_view(self):
        current = review()
        dismissed = [
            review_item_fingerprint(current, item) for item in current["items"]
        ]
        result = projected(
            project(findings=[], dismissed_attention=dismissed)
        )
        self.assertEqual(result["attention_items"], [])
        self.assertNotIn("needs_a_look", result["views"])
        self.assertEqual(result["facets"]["attention"], "none")

    def test_suggested_review_respects_per_item_dismissal(self):
        current = review()
        dismissed = [
            review_item_fingerprint(current, item) for item in current["items"]
        ]
        result = projected(project(findings=[], dismissed_attention=dismissed))
        self.assertEqual(result["facets"]["suggested_review_items"], 0)
        self.assertNotIn("review_suggested", result["views"])

    def test_project_fingerprint_is_unchanged_by_the_refactor(self):
        """Remote dismissals recorded by an earlier version must still apply."""
        self.assertEqual(
            attention_fingerprint(project()),
            "d4fa5d31f0d48dfc8e9bb61fd7a4ef7948df89a6aaab6b856d62d3cc05629542",
        )


class ObserverDismissalTests(unittest.TestCase):
    """The service-level path the dashboard button actually calls."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.spool = root / "signals.jsonl"
        self.codex_root = root / "codex"
        self.config = ObserverConfig(
            root / "state",
            (root / "claude",),
            (self.codex_root,),
            None,
            self.spool,
        )
        self.session_id = "abababab-cdcd-efef-0101-232323232323"
        path = self.codex_root / f"rollout-{self.session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        moment = time.time() - 60
        with path.open("a", encoding="utf-8") as handle:
            for record in (
                {
                    "type": "session_meta",
                    "timestamp": iso(moment),
                    "payload": {"id": self.session_id, "cwd": str(self.project)},
                },
                {
                    "type": "event_msg",
                    "timestamp": iso(moment),
                    "payload": {"type": "user_message", "message": "Do the thing"},
                },
            ):
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_dismissing_one_request_leaves_the_other_session_untouched(self):
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            for session, detail in (
                (self.session_id, "Approve the shell command"),
                ("other-session", "Approve the network call"),
            ):
                append_signal(
                    build_signal(
                        "codex",
                        session_id=session,
                        cwd=str(self.project),
                        detail=detail,
                    ),
                    path=self.spool,
                )
            observer.scan()
            project_id = observer.status()["projects"][0]["project_id"]
            items = dashboard_projection(observer.status())["projects"][0][
                "attention_items"
            ]
            self.assertEqual(len(items), 2)

            target = items[0]
            self.assertTrue(
                observer.dismiss_attention_item(
                    project_id, target["dismiss_kind"], target["dismiss_ref"]
                )
            )
            remaining = dashboard_projection(observer.status())["projects"][0][
                "attention_items"
            ]
            self.assertEqual(len(remaining), 1)
            self.assertNotEqual(remaining[0]["dismiss_ref"], target["dismiss_ref"])

    def test_an_unknown_attention_kind_is_refused(self):
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            project_id = observer.status()["projects"][0]["project_id"]
            with self.assertRaises(ValueError):
                observer.dismiss_attention_item(project_id, "arbitrary", "x")

    def test_remote_projects_keep_whole_project_dismissal(self):
        with Observer(self.config) as observer:
            self.assertFalse(
                observer.dismiss_attention_item(
                    "remote:node_abc:project_1", "finding", "x"
                )
            )


if __name__ == "__main__":
    unittest.main()
