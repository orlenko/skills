"""Opt-in real interactive CLI + private Herdr + local mock model regression.

Set AGENT_TEST_CODEX_BIN and/or AGENT_TEST_CLAUDE_BIN to real executables (not
account-routing shims). AGENT_TEST_HERDR_BIN optionally selects hc. No account,
external model request, production skill, or existing terminal is used.
"""

from __future__ import annotations

import os
import json
import subprocess
import sys
import unittest
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herdr_fixture_env import cleanup, create_root, environment, run_fixture


class InteractiveDispatchTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("AGENT_TEST_HERDR_BIN"),
        "set AGENT_TEST_HERDR_BIN for private timeout test",
    )
    def test_outer_timeout_stops_the_owned_private_core_and_program(self):
        from agent_orchestra import terminal_control, keys

        root = create_root()
        env = environment(root)
        owned = None
        try:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_fixture(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(Path(__file__).with_name("herdr_stalled_fixture.py")),
                    ],
                    root,
                    env,
                    timeout=8,
                )
            self.assertIn(b"PRIVATE PROGRAM STARTED", raised.exception.stdout or b"")
            owned = json.loads((root / "owned-program.json").read_text())
        finally:
            outcome = cleanup(root, env)
        self.assertIn('"stopped":true', outcome)
        self.assertFalse(root.exists())
        deadline = time.monotonic() + 3
        while True:
            try:
                alive = (
                    list(terminal_control.process(owned["pid"]).start) == owned["start"]
                )
            except keys.KeysError:
                alive = False
            if not alive or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        self.assertFalse(alive, "private program survived parent fallback cleanup")

    def invoke(self, fixture: str, *args: str, timeout: float = 90):
        root = create_root()
        env = environment(root)
        # Default hc is resolved by the parent, never by a private child's config.
        if "AGENT_TEST_HERDR_BIN" not in env:
            import shutil

            env["AGENT_TEST_HERDR_BIN"] = shutil.which("hc") or "hc"
        try:
            result = run_fixture(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-W",
                    "error::ResourceWarning",
                    str(Path(__file__).with_name(fixture)),
                    *args,
                ],
                root,
                env,
                timeout=timeout,
            )
        finally:
            outcome = cleanup(root, env)
        result.stdout += "\n" + outcome
        return result

    @unittest.skipUnless(
        os.environ.get("AGENT_TEST_HERDR_BIN"),
        "set AGENT_TEST_HERDR_BIN for private PTY test",
    )
    def test_process_rollover_never_finishes_or_replays_old_input_in_replacement(self):
        result = self.invoke("herdr_rollover_fixture.py", timeout=60)
        self.assertEqual(
            result.returncode, 0, result.stdout[-4000:] + result.stderr[-3000:]
        )
        self.assertIn("ROLLOVER VERIFIED", result.stdout)
        self.assertIn('private core cleanup 0 {"stopped":true}', result.stdout)
        print("private rollover and parent cleanup verified", flush=True)

    def run_fixture(self, provider: str) -> None:
        result = self.invoke("herdr_cli_fixture.py", provider)
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
        self.assertIn("MID-DRAFT INSERTION VERIFIED", result.stdout)
        print(
            "\n".join(
                line
                for line in result.stdout.splitlines()
                if line.startswith(
                    (
                        "RUNNING CORE",
                        "SKILL DISPATCH VERIFIED",
                        "MULTILINE /",
                        "ANYTIME APPEND",
                        "private core cleanup",
                    )
                )
            ),
            flush=True,
        )

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
