"""A seat stays with the agent that joined it until `adopt` moves it."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import core, member as member_module  # noqa: E402
from agent_orchestra.core import atomic_write_json, instance_key, member_path, runtime_dir  # noqa: E402

MEMBER = "mb_trumpet0001"


class AdoptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-adopt-")
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name) / "ops"
        self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": str(Path(temp.name) / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        atomic_write_json(member_path(MEMBER), {
            "member_id": MEMBER, "provider": "codex", "cwd": str(self.cwd),
            "instance_key": instance_key("codex", self.cwd), "owner_pid": None,
        })
        self.session = os.getpid()
        for name, value in (("agent_session_pid", self.session), ("agent_kind", "claude")):
            patcher = mock.patch.object(core, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_claude_process_does_not_take_a_codex_seat(self):
        # Trumpet, 2026-09-24: a Claude session ran commands with --provider codex.
        seat = member_module.claim_ownership(member_module.load_member(MEMBER))
        self.assertIsNone(seat["owner_pid"])
        self.assertIsNone(member_module.load_member(MEMBER)["owner_pid"])

    def test_the_same_agent_still_repairs_its_seat(self):
        core.agent_kind.return_value = "codex"
        seat = member_module.claim_ownership(member_module.load_member(MEMBER))
        self.assertEqual(seat["owner_pid"], self.session)

    def test_adopt_moves_the_seat_to_this_agent(self):
        for suffix in (".codex-session.json", ".codex-wake.json"):
            atomic_write_json(runtime_dir() / f"{MEMBER}{suffix}", {"thread_id": "x"})
        result = member_module.adopt(MEMBER, provider="claude", cwd=str(self.cwd))
        seat = member_module.load_member(MEMBER)
        self.assertEqual(seat["provider"], "claude")
        self.assertEqual(seat["instance_key"], instance_key("claude", self.cwd))
        self.assertEqual(seat["owner_pid"], self.session)
        self.assertEqual(seat["adopted"]["provider"], "codex")
        self.assertEqual(result["was"]["provider"], "codex")
        self.assertFalse((runtime_dir() / f"{MEMBER}.codex-session.json").exists())
        self.assertFalse((runtime_dir() / f"{MEMBER}.codex-wake.json").exists())
        # Claude hooks in that directory find it now.
        rows = member_module.iter_members(provider="claude", cwd=str(self.cwd))
        self.assertEqual([row["member_id"] for row in rows], [MEMBER])

    def test_a_closed_seat_is_not_adopted(self):
        seat = member_module.load_member(MEMBER)
        seat["closed_at"] = 1.0
        member_module.save_member(seat)
        with self.assertRaisesRegex(core.OrchestraError, "closed"):
            member_module.adopt(MEMBER, provider="claude", cwd=str(self.cwd))


class AgentKindTests(unittest.TestCase):
    def test_names_from_the_command_line(self):
        cases = {
            "/home/vlad/.nvm/versions/node/v22.22.2/bin/claude --settings {}": "claude",
            "node /usr/lib/node_modules/@openai/codex/bin/codex.js": "codex",
            "/usr/bin/python3 -m agent_orchestra monitor-run": None,
        }
        for args, kind in cases.items():
            with self.subTest(args=args), mock.patch.object(core, "_ps_field", return_value=args):
                self.assertEqual(core.agent_kind(1234), kind)


if __name__ == "__main__":
    unittest.main()
