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
from agent_observer.service import Observer, ObserverConfig  # noqa: E402
from agent_observer.signals import (  # noqa: E402
    append_signal,
    build_signal,
    drain_signals,
    normalize_hook_payload,
    valid_signal,
)


def iso(moment: float) -> str:
    return (
        datetime.fromtimestamp(moment, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class SignalSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.spool = Path(self.temp.name) / "signals.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def test_draining_clears_the_spool_for_the_next_writer(self):
        append_signal(
            build_signal("claude", session_id="s1", cwd="/tmp", detail="one"),
            path=self.spool,
        )
        self.assertEqual(len(drain_signals(self.spool)), 1)
        self.assertEqual(drain_signals(self.spool), [])
        self.assertEqual(self.spool.stat().st_size, 0)

    def test_missing_spool_is_not_an_error(self):
        self.assertEqual(drain_signals(Path(self.temp.name) / "absent.jsonl"), [])

    def test_untrusted_lines_are_rejected_field_by_field(self):
        self.spool.write_text(
            "\n".join(
                [
                    "not json at all",
                    json.dumps({"v": 99, "provider": "claude"}),
                    json.dumps(build_signal("hal", session_id="s", cwd="/tmp")),
                    json.dumps(
                        build_signal("claude", session_id="", cwd="/tmp")
                    ),
                    json.dumps(
                        {**build_signal("claude", session_id="s", cwd="/tmp"), "at": 0}
                    ),
                    json.dumps(
                        {
                            **build_signal("claude", session_id="s", cwd="/tmp"),
                            "kind": "turn_completed",
                        }
                    ),
                    json.dumps(build_signal("codex", session_id="ok", cwd="/tmp")),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        drained = drain_signals(self.spool)
        self.assertEqual([item["session_id"] for item in drained], ["ok"])

    def test_detail_is_bounded_and_stripped_of_control_characters(self):
        record = valid_signal(
            build_signal(
                "claude",
                session_id="s",
                cwd="/tmp",
                detail="a\x00b" + "x" * 900,
            )
        )
        self.assertIsNotNone(record)
        self.assertLessEqual(len(record["detail"]), 500)
        self.assertNotIn("\x00", record["detail"])

    def test_turn_completion_notices_are_not_attention(self):
        self.assertIsNone(
            normalize_hook_payload(
                "codex",
                {"type": "agent-turn-complete", "session_id": "s", "cwd": "/tmp"},
            )
        )
        self.assertIsNotNone(
            normalize_hook_payload(
                "codex",
                {"type": "approval-requested", "session_id": "s", "cwd": "/tmp"},
            )
        )

    def test_a_signal_without_a_session_is_dropped_rather_than_guessed(self):
        self.assertIsNone(
            normalize_hook_payload("claude", {"cwd": "/tmp", "message": "blocked"})
        )


class SignalIngestionTests(unittest.TestCase):
    """A blocked session becomes observed attention without touching the worker."""

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
        self.session_id = "12121212-3434-5656-7878-909090909090"
        self.path = self.codex_root / f"rollout-{self.session_id}.jsonl"
        self.write(
            {
                "type": "session_meta",
                "timestamp": iso(time.time() - 60),
                "payload": {"id": self.session_id, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": iso(time.time() - 60),
                "payload": {"type": "user_message", "message": "Start the migration"},
            },
        )

    def tearDown(self):
        self.temp.cleanup()

    def write(self, *records: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

    def signal(self, *, at: float, detail: str = "Approve the shell command") -> None:
        append_signal(
            build_signal(
                "codex",
                session_id=self.session_id,
                cwd=str(self.project),
                detail=detail,
                origin="approval-requested",
                at=at,
            ),
            path=self.spool,
        )

    def requests(self, observer: Observer) -> list[dict]:
        return [
            finding
            for finding in observer.status()["projects"][0]["findings"]
            if finding["kind"] == "input_requested"
        ]

    def test_blocked_session_surfaces_on_the_next_scan(self):
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self.signal(at=time.time())
            observer.scan()
            requests = self.requests(observer)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["summary"], "Approve the shell command")
            projected = dashboard_projection(observer.status())["projects"][0]
            self.assertEqual(projected["primary_attention"]["type"], "input_requested")
            self.assertEqual(projected["primary_attention"]["truth"], "observed")

    def test_a_precise_request_absorbs_the_silence_inference(self):
        """One session must not occupy two rows saying the same thing."""
        stalled = self.codex_root / "rollout-stalled.jsonl"
        quiet = time.time() - 20 * 60
        with stalled.open("w", encoding="utf-8") as handle:
            for record in (
                {
                    "type": "session_meta",
                    "timestamp": iso(quiet),
                    "payload": {"id": "stalled-session", "cwd": str(self.project)},
                },
                {
                    "type": "event_msg",
                    "timestamp": iso(quiet),
                    "payload": {"type": "user_message", "message": "Other work"},
                },
            ):
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")

        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observer.scan()
            kinds = [
                finding["kind"]
                for finding in observer.status()["projects"][0]["findings"]
            ]
            # Only the 20-minute-quiet session qualifies; setUp's is 60s old.
            self.assertEqual(kinds.count("no_completion_observed"), 1)

            append_signal(
                build_signal(
                    "codex",
                    session_id="stalled-session",
                    cwd=str(self.project),
                    detail="Approve the shell command",
                ),
                path=self.spool,
            )
            observer.scan()
            findings = observer.status()["projects"][0]["findings"]
            by_session: dict[str, set[str]] = {}
            for finding in findings:
                by_session.setdefault(finding["session_id"], set()).add(finding["kind"])
            self.assertEqual(by_session["stalled-session"], {"input_requested"})

    def test_signals_from_unwatched_directories_are_ignored(self):
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            append_signal(
                build_signal(
                    "codex",
                    session_id="elsewhere",
                    cwd=str(Path(self.temp.name) / "other"),
                ),
                path=self.spool,
            )
            observer.scan()
            self.assertEqual(self.requests(observer), [])

    def test_repeated_notices_keep_the_original_request_time(self):
        first = time.time() - 300
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self.signal(at=first)
            observer.scan()
            self.signal(at=time.time(), detail="Still waiting")
            observer.scan()
            requests = self.requests(observer)
            self.assertEqual(len(requests), 1)
            self.assertAlmostEqual(requests[0]["created_at"], first, places=3)
            self.assertGreater(requests[0]["updated_at"], first)

    def test_a_late_arriving_older_record_does_not_clear_the_request(self):
        moment = time.time()
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self.signal(at=moment)
            observer.scan()
            self.assertEqual(len(self.requests(observer)), 1)

            # Collection lags the harness; this record predates the request.
            self.write(
                {
                    "type": "event_msg",
                    "timestamp": iso(moment - 30),
                    "payload": {"type": "user_message", "message": "earlier context"},
                }
            )
            observer.scan()
            self.assertEqual(len(self.requests(observer)), 1)

            self.write(
                {
                    "type": "event_msg",
                    "timestamp": iso(moment + 30),
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "Permission granted, continuing",
                    },
                }
            )
            observer.scan()
            self.assertEqual(self.requests(observer), [])

    def test_a_new_request_after_resolution_resurfaces_a_dismissed_session(self):
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self.signal(at=time.time() - 120)
            observer.scan()
            project_id = observer.status()["projects"][0]["project_id"]
            observer.db.dismiss_project_findings(project_id)
            self.assertTrue(all(item["seen"] for item in self.requests(observer)))

            self.write(
                {
                    "type": "event_msg",
                    "timestamp": iso(time.time() - 60),
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "Continuing",
                    },
                }
            )
            observer.scan()
            self.signal(at=time.time(), detail="Approve the next command")
            observer.scan()
            requests = self.requests(observer)
            self.assertEqual(len(requests), 1)
            self.assertFalse(requests[0]["seen"])


if __name__ == "__main__":
    unittest.main()
