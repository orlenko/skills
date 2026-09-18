"""The send-time check: gated, NEED-none only, advice only, never fatal."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra import jev  # noqa: E402
from agent_orchestra.core import runtime_dir  # noqa: E402

TEXT = "ACT tell\nTO conductor\n\nCan you confirm the migration landed on staging?"


class SendCheckTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-orchestra-jevsend-")
        keys = ("AGENT_ORCHESTRA_HOME", jev.FLAG_ENV, jev.LEGACY_FLAG_ENV, jev.KEY_ENV)
        self.env = {k: os.environ.get(k) for k in keys}
        os.environ["AGENT_ORCHESTRA_HOME"] = self.state
        os.environ.pop(jev.LEGACY_FLAG_ENV, None)
        os.environ[jev.FLAG_ENV] = "1"
        os.environ[jev.KEY_ENV] = "k"
        self.calls = []
        self.p = 0.95
        self._ask = jev.ask
        jev.ask = lambda state, questions, timeout: (
            self.calls.append((state, timeout)),
            ({"needs_reply": {"noul": self.p}}, {"latency_ms": 3, "input_tokens": 90}),
        )[1]

    def tearDown(self):
        jev.ask = self._ask
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.state, ignore_errors=True)

    def log(self):
        path = runtime_dir() / "mb_a.jev-send.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_warns_and_logs_at_threshold(self):
        warning = jev.send_check("mb_a", "m_1", "tell", "none", TEXT)
        self.assertIn("RE m_1", warning)
        self.assertEqual(self.calls[0][0], "Can you confirm the migration landed on staging?")
        self.assertLessEqual(self.calls[0][1], jev.SEND_TIMEOUT_SECONDS)
        [row] = self.log()
        self.assertTrue(row["warned"])
        self.assertEqual(row["needs_reply_p"], 0.95)
        self.assertEqual((runtime_dir() / "mb_a.jev-send.jsonl").stat().st_mode & 0o777, 0o600)

    def test_below_threshold_is_quiet(self):
        self.p = 0.89
        self.assertIsNone(jev.send_check("mb_a", "m_1", "tell", "none", TEXT))
        self.assertFalse(self.log()[0]["warned"])

    def test_only_need_none_is_checked(self):
        self.assertIsNone(jev.send_check("mb_a", "m_1", "ask", "yes/no", TEXT))
        self.assertEqual(self.calls, [])

    def test_off_without_flag_or_key(self):
        os.environ.pop(jev.KEY_ENV)
        self.assertIsNone(jev.send_check("mb_a", "m_1", "tell", "none", TEXT))
        os.environ[jev.KEY_ENV] = "k"
        os.environ[jev.FLAG_ENV] = "0"
        self.assertIsNone(jev.send_check("mb_a", "m_1", "tell", "none", TEXT))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.log(), [])

    def test_legacy_shadow_flag_still_enables(self):
        os.environ.pop(jev.FLAG_ENV)
        os.environ[jev.LEGACY_FLAG_ENV] = "1"
        self.assertTrue(jev.enabled())

    def test_short_body_skipped(self):
        self.assertIsNone(jev.send_check("mb_a", "m_1", "tell", "none", "ACT tell\nTO all\n\nok"))
        self.assertEqual(self.calls, [])

    def test_failure_is_logged_not_raised(self):
        def boom(*args, **kwargs):
            raise TimeoutError("slow")
        jev.ask = boom
        self.assertIsNone(jev.send_check("mb_a", "m_1", "tell", "none", TEXT))
        self.assertIn("TimeoutError", self.log()[0]["error"])


if __name__ == "__main__":
    unittest.main()
