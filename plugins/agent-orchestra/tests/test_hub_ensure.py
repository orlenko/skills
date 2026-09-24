"""ensure_hub when another process starts the hub first."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import hub  # noqa: E402
from agent_orchestra.core import OrchestraError  # noqa: E402


class EnsureHubRaceTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("read_json", mock.Mock(return_value={"port": 1})),
            ("hub_dir", mock.Mock(return_value=Path("/nonexistent/orc"))),
            ("_hub_log", mock.Mock(return_value=Path("/nonexistent/log"))),
            ("_spawn_module", mock.Mock(return_value=111)),
            ("_wait_for_ready", mock.Mock(side_effect=OrchestraError("Hub process did not start"))),
        ):
            patcher = mock.patch.object(hub, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_hub_another_process_started_counts(self):
        with mock.patch.object(hub, "_running_pid", side_effect=[None, 222]):
            self.assertEqual(hub.ensure_hub("orc_0123456789abcdef"), 222)

    def test_no_hub_at_all_still_fails(self):
        with mock.patch.object(hub, "_running_pid", return_value=None):
            with self.assertRaisesRegex(OrchestraError, "did not start"):
                hub.ensure_hub("orc_0123456789abcdef")


if __name__ == "__main__":
    unittest.main()
