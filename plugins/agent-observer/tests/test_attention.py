import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_observer.attention import attention_fingerprint
from agent_observer.presentation import dashboard_projection


class AttentionProjectionTests(unittest.TestCase):
    def project(self):
        lifecycle = [
            {
                "finding_id": f"turn:{index}",
                "provider": "codex",
                "session_id": "worker-session",
                "kind": "turn_completed" if index else "turn_aborted",
                "state": "open",
                "seen": 0,
                "updated_at": 900 + index,
                "summary": '{"changedFiles":["internal-output"]}',
                "details": {},
            }
            for index in range(19)
        ]
        return {
            "project_id": "project-1",
            "display_path": "~/code/ops2",
            "resolved_path": "/home/vlad/code/ops2",
            "current_branch": "docs/cursor-bugbot-gap-report",
            "sessions": [
                {
                    "activity_age_seconds": 5,
                    "last_activity_at": 995,
                }
            ],
            "sources": [
                {
                    "source_id": "source-1",
                    "health": "healthy",
                    "generation": 1,
                    "committed_offset": 100,
                }
            ],
            "findings": lifecycle,
            "changes": [],
            "review": None,
        }

    @staticmethod
    def review():
        return {
            "job_id": "review-human-input",
            "analyzer_provider": "codex",
            "status": "current",
            "submitted_at": 990,
            "summary": "Two bounded loose ends remain.",
            "items": [
                {
                    "type": "question",
                    "assessment": "no_later_handling_observed",
                    "intended_party": "user",
                    "title": "Clarify whether PR #2328 still needs its reviewer fleet",
                    "detail": "The intended PR is still ambiguous.",
                    "provider": "codex",
                    "session_id": "worker-session",
                    "message_ref": "message:question",
                    "evidence_excerpt": "The #2328 reviewer fleet was not run.",
                    "timestamp": "2026-08-04T12:00:00Z",
                },
                {
                    "type": "decision",
                    "assessment": "no_later_handling_observed",
                    "intended_party": "user",
                    "title": "Decide whether to rerun overflowed validators",
                    "detail": "Forty-four validators overflowed the queue.",
                    "provider": "codex",
                    "session_id": "worker-session",
                    "message_ref": "message:decision",
                    "evidence_excerpt": "44 validators overflowed the queue.",
                    "timestamp": "2026-08-04T12:01:00Z",
                },
                {
                    "type": "agent_action",
                    "assessment": "no_later_handling_observed",
                    "intended_party": "agent",
                    "title": "Run the remaining validator fleet",
                    "detail": "This belongs to the worker, not the operator.",
                    "provider": "codex",
                    "session_id": "worker-session",
                    "message_ref": "message:agent-action",
                    "evidence_excerpt": "I can run the rest.",
                    "timestamp": "2026-08-04T12:02:00Z",
                },
            ],
            "limitations": [],
            "packet_meta": {"coverage": {}, "target_session": None},
        }

    def test_lifecycle_noise_never_becomes_human_attention(self):
        project = self.project()
        projected = dashboard_projection({"generated_at": 1000, "projects": [project]})
        result = projected["projects"][0]
        self.assertNotIn("needs_a_look", result["views"])
        self.assertIsNone(result["primary_attention"])
        self.assertEqual(result["facets"]["attention"], "none")
        self.assertEqual(len(result["findings"]), 19)

    def test_human_review_outranks_lifecycle_and_agent_work(self):
        project = self.project()
        project["review"] = self.review()
        projected = dashboard_projection({"generated_at": 1000, "projects": [project]})
        result = projected["projects"][0]
        self.assertIn("needs_a_look", result["views"])
        self.assertEqual(len(result["attention_items"]), 2)
        self.assertEqual(result["primary_attention"]["type"], "decision")
        self.assertEqual(
            result["primary_attention"]["title"],
            "Decide whether to rerun overflowed validators",
        )
        self.assertIsNone(result["primary_finding"])

    def test_dismissed_review_stays_clear_when_only_lifecycle_changes(self):
        project = self.project()
        project["review"] = self.review()
        before = attention_fingerprint(project)
        project["findings"].append(
            {
                **project["findings"][-1],
                "finding_id": "turn:newer",
                "updated_at": 999,
            }
        )
        self.assertEqual(attention_fingerprint(project), before)
        project["review"]["dismissed_at"] = 1000
        projected = dashboard_projection({"generated_at": 1001, "projects": [project]})
        self.assertNotIn("needs_a_look", projected["projects"][0]["views"])


if __name__ == "__main__":
    unittest.main()
