from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.db import ObserverDB  # noqa: E402
from agent_observer.sentinels import evaluate  # noqa: E402


def touch(path: Path, age_seconds: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    moment = time.time() - age_seconds
    os.utime(path, (moment, moment))
    return path


class SentinelProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, **overrides):
        base = {
            "sentinel_id": "probe",
            "label": "Nightly analyze",
            "probe": "file_freshness",
            "target": str(self.root / "events.jsonl"),
            "max_age_seconds": 3600.0,
            "expires_at": None,
            "note": None,
        }
        base.update(overrides)
        return base

    def test_fresh_evidence_is_healthy(self):
        touch(self.root / "events.jsonl", age_seconds=60)
        self.assertEqual(evaluate(self.row())["status"], "healthy")

    def test_a_job_that_stopped_writing_is_failing(self):
        touch(self.root / "events.jsonl", age_seconds=4 * 3600)
        result = evaluate(self.row())
        self.assertEqual(result["status"], "failing")
        self.assertIn("beyond the", result["detail"])

    def test_a_missing_target_is_failing_rather_than_silent(self):
        result = evaluate(self.row())
        self.assertEqual(result["status"], "failing")
        self.assertIn("missing", result["detail"])

    def test_an_empty_queue_is_healthy(self):
        queue = self.root / "inbox"
        queue.mkdir()
        result = evaluate(
            self.row(probe="queue_backlog", target=str(queue), max_age_seconds=86400)
        )
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["detail"], "queue is empty")

    def test_a_queue_whose_oldest_item_waited_too_long_is_failing(self):
        queue = self.root / "inbox"
        touch(queue / "brief-a.md", age_seconds=29 * 86400)
        touch(queue / "brief-b.md", age_seconds=60)
        result = evaluate(
            self.row(probe="queue_backlog", target=str(queue), max_age_seconds=7 * 86400)
        )
        self.assertEqual(result["status"], "failing")
        self.assertIn("29d", result["detail"])

    def test_hidden_entries_do_not_count_as_queued_work(self):
        queue = self.root / "inbox"
        touch(queue / ".DS_Store", age_seconds=400 * 86400)
        result = evaluate(
            self.row(probe="queue_backlog", target=str(queue), max_age_seconds=86400)
        )
        self.assertEqual(result["status"], "healthy")

    def test_an_unaffirmed_check_asks_to_be_retired_instead_of_alarming(self):
        """The a2a lesson: a probe for a retired system must not cry failure."""
        touch(self.root / "events.jsonl", age_seconds=40 * 3600)
        result = evaluate(self.row(expires_at=time.time() - 10))
        self.assertEqual(result["status"], "expired")
        self.assertIn("re-affirmed", result["detail"])


class SentinelRosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = ObserverDB(self.root / "state")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def enroll(self, **overrides):
        options = {
            "sentinel_id": "undrudge-analyze",
            "label": "undrudge analyze",
            "probe": "file_freshness",
            "target": str(touch(self.root / "events.jsonl", age_seconds=4 * 3600)),
            "max_age_seconds": 3600.0,
            "expires_at": None,
            "note": None,
        }
        options.update(overrides)
        return self.db.add_sentinel(**options)

    def test_roster_counts_failing_and_expired_separately(self):
        self.enroll()
        self.enroll(
            sentinel_id="retired-daemon",
            label="a2a daemon",
            expires_at=time.time() - 10,
        )
        result = self.db.evaluate_sentinels()
        self.assertEqual(result["failing"], 1)
        self.assertEqual(result["expired"], 1)

    def test_affirming_restores_a_check_to_active_duty(self):
        self.enroll(expires_at=time.time() - 10)
        self.assertEqual(self.db.evaluate_sentinels()["expired"], 1)
        self.db.affirm_sentinel("undrudge-analyze", time.time() + 86400)
        result = self.db.evaluate_sentinels()
        self.assertEqual(result["expired"], 0)
        self.assertEqual(result["failing"], 1)

    def test_removing_a_check_stops_every_claim_it_made(self):
        self.enroll()
        self.assertTrue(self.db.remove_sentinel("undrudge-analyze"))
        self.assertEqual(self.db.evaluate_sentinels()["entries"], [])
        self.assertFalse(self.db.remove_sentinel("undrudge-analyze"))

    def test_unsupported_probes_are_refused_at_enrollment(self):
        with self.assertRaises(ValueError):
            self.enroll(probe="run_this_command")


if __name__ == "__main__":
    unittest.main()
