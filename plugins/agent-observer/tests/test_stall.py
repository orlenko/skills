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

from agent_observer.presentation import dashboard_projection  # noqa: E402
from agent_observer.service import (  # noqa: E402
    STALL_DEBOUNCE_SECONDS,
    STALL_WINDOW_SECONDS,
    Observer,
    ObserverConfig,
)


def iso(moment: float) -> str:
    return (
        datetime.fromtimestamp(moment, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class StalledSessionTests(unittest.TestCase):
    """A session that owes a completion and went quiet is an observed fact."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.claude_root = root / "claude"
        self.codex_root = root / "codex"
        self.config = ObserverConfig(
            root / "state", (self.claude_root,), (self.codex_root,)
        )
        self.session_id = "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
        self.path = self.codex_root / f"rollout-{self.session_id}.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *records: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def unanswered_prompt(self, quiet_seconds: float) -> None:
        moment = time.time() - quiet_seconds
        self.write(
            {
                "type": "session_meta",
                "timestamp": iso(moment),
                "payload": {"id": self.session_id, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": iso(moment),
                "payload": {"type": "user_message", "message": "Ship the migration"},
            },
        )

    def findings(self, observer: Observer) -> list[dict]:
        return [
            finding
            for finding in observer.status()["projects"][0]["findings"]
            if finding["kind"] == "no_completion_observed"
        ]

    def test_quiet_unanswered_session_becomes_observed_attention(self):
        self.unanswered_prompt(STALL_DEBOUNCE_SECONDS + 120)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            findings = self.findings(observer)
            self.assertEqual(len(findings), 1)
            self.assertEqual(
                findings[0]["summary"],
                "No agent response observed since your last message",
            )
            projected = dashboard_projection(observer.status())["projects"][0]
            self.assertIn("needs_a_look", projected["views"])
            attention = projected["primary_attention"]
            self.assertEqual(attention["type"], "no_completion_observed")
            self.assertEqual(attention["truth"], "observed")

    def test_recent_silence_is_ordinary_work(self):
        self.unanswered_prompt(STALL_DEBOUNCE_SECONDS / 2)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            self.assertEqual(self.findings(observer), [])

    def test_abandoned_session_never_enters_the_attention_ledger(self):
        self.unanswered_prompt(STALL_WINDOW_SECONDS + 3600)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            self.assertEqual(self.findings(observer), [])

    def test_stall_claim_retires_once_it_outlives_the_window(self):
        self.unanswered_prompt(STALL_WINDOW_SECONDS - 60)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            self.assertEqual(len(self.findings(observer)), 1)
            # The same session, re-examined once the claim has aged out.
            observer.db.reconcile_stalled_sessions(
                time.time() + 120,
                debounce_seconds=STALL_DEBOUNCE_SECONDS,
                window_seconds=STALL_WINDOW_SECONDS,
            )
            self.assertEqual(self.findings(observer), [])

    def test_resumed_work_clears_the_claim_without_operator_action(self):
        self.unanswered_prompt(STALL_DEBOUNCE_SECONDS + 120)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            self.assertEqual(len(self.findings(observer)), 1)
            self.write(
                {
                    "type": "event_msg",
                    "timestamp": iso(time.time()),
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "Migration shipped",
                    },
                },
            )
            observer.scan()
            self.assertEqual(self.findings(observer), [])

    def test_direct_request_outranks_an_inference_drawn_from_silence(self):
        self.unanswered_prompt(STALL_DEBOUNCE_SECONDS + 120)
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            raw = observer.status()
            project = raw["projects"][0]
            project["findings"].append(
                {
                    "finding_id": "decision:codex:worker:request-1",
                    "provider": "codex",
                    "session_id": "worker",
                    "kind": "decision_requested",
                    "state": "open",
                    "seen": 0,
                    "created_at": time.time() - 30,
                    "updated_at": time.time() - 30,
                    "summary": "Choose the deployment window",
                    "details": {"items": {}},
                }
            )
            projected = dashboard_projection(raw)["projects"][0]
            self.assertEqual(len(projected["attention_items"]), 2)
            self.assertEqual(
                projected["primary_attention"]["type"], "decision_requested"
            )


if __name__ == "__main__":
    unittest.main()
