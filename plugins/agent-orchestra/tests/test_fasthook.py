"""The hook fast path answers only where no membership exists."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import core, fasthook  # noqa: E402
from agent_orchestra.core import atomic_write_json, member_path  # noqa: E402


class FastHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-fasthook-")
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name) / "project"
        self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": str(Path(temp.name) / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        stdin = sys.stdin
        self.addCleanup(lambda: setattr(sys, "stdin", stdin))

    def run_hook(self, command: str, provider: str = "claude") -> tuple[bool, str]:
        sys.stdin = io.StringIO(json.dumps({"cwd": str(self.cwd), "session_id": "s"}))
        out = io.StringIO()
        with redirect_stdout(out):
            done = fasthook.nothing_to_do(["agent-orchestra", command, "--provider", provider])
        return done, out.getvalue()

    def test_it_mirrors_core(self):
        self.assertEqual(fasthook.state_root(), core.state_root())
        for provider in ("claude", "codex", "claude-code", "OpenAI"):
            with self.subTest(provider=provider):
                self.assertEqual(fasthook.instance_key(provider, str(self.cwd)),
                                 core.instance_key(provider, self.cwd))

    def test_no_membership_answers_empty_at_once(self):
        self.assertEqual(self.run_hook("hook-stop"), (True, "{}\n"))
        self.assertEqual(self.run_hook("hook-context"), (True, ""))
        self.assertEqual(self.run_hook("hook-wait"), (True, ""))

    def test_a_membership_here_goes_to_the_full_path_with_its_payload(self):
        atomic_write_json(member_path("mb_here0001"), {
            "member_id": "mb_here0001", "instance_key": core.instance_key("claude", self.cwd)})
        done, out = self.run_hook("hook-stop")
        self.assertEqual((done, out), (False, ""))
        self.assertEqual(json.loads(sys.stdin.read())["cwd"], str(self.cwd))

    def test_another_agents_or_a_closed_membership_is_not_here(self):
        atomic_write_json(member_path("mb_codex0001"), {
            "member_id": "mb_codex0001", "instance_key": core.instance_key("codex", self.cwd)})
        atomic_write_json(member_path("mb_gone00001"), {
            "member_id": "mb_gone00001", "instance_key": core.instance_key("claude", self.cwd),
            "closed_at": 1.0})
        self.assertTrue(self.run_hook("hook-stop")[0])

    def test_an_unreadable_member_file_is_not_proof_of_absence(self):
        path = member_path("mb_broken001")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertFalse(self.run_hook("hook-stop")[0])


if __name__ == "__main__":
    unittest.main()
