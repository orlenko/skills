"""Opt-in real interactive CLI + private Herdr + local mock model regression.

Set AGENT_TEST_CODEX_BIN and/or AGENT_TEST_CLAUDE_BIN to real executables (not
account-routing shims). AGENT_TEST_HERDR_BIN optionally selects hc. No account,
external model request, production skill, or existing terminal is used.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


class InteractiveDispatchTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AGENT_TEST_HERDR_BIN"),
        "set AGENT_TEST_HERDR_BIN for private PTY test",
    )
    def test_process_rollover_never_finishes_or_replays_old_input_in_replacement(self):
        fixture = Path(__file__).with_name("herdr_rollover_fixture.py")
        result = subprocess.run(
            [sys.executable, str(fixture)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(
            result.returncode, 0, result.stdout[-4000:] + result.stderr[-3000:]
        )
        self.assertIn("ROLLOVER VERIFIED", result.stdout)
        self.assertIn('private core cleanup 0 {"stopped":true}', result.stdout)

    def run_fixture(self, provider: str) -> None:
        fixture = Path(__file__).with_name("herdr_cli_fixture.py")
        result = subprocess.run(
            [sys.executable, str(fixture), provider],
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(
            result.returncode, 0, result.stdout[-6000:] + result.stderr[-2000:]
        )
        self.assertIn("ACTUAL ADAPTER OUTCOME typed", result.stdout)
        self.assertIn(f"SKILL DISPATCH VERIFIED {provider}", result.stdout)
        self.assertIn(
            f"MULTILINE / NO-ENTER / DRAFT REFUSAL / KEY-ONLY VERIFIED {provider}",
            result.stdout,
        )
        self.assertIn('private core cleanup 0 {"stopped":true}', result.stdout)

    @unittest.skipUnless(
        os.environ.get("AGENT_TEST_CODEX_BIN"),
        "set AGENT_TEST_CODEX_BIN for native CLI test",
    )
    def test_codex_skill_dispatch_and_composer_controls(self):
        self.run_fixture("codex")

    @unittest.skipUnless(
        os.environ.get("AGENT_TEST_CLAUDE_BIN"),
        "set AGENT_TEST_CLAUDE_BIN for native CLI test",
    )
    def test_claude_skill_dispatch_and_composer_controls(self):
        self.run_fixture("claude")
