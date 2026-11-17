from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import cli, core, herdr_display, member


class DisplayLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orchestra-display-")
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": self.tmp.name,
            "HC_TERMINAL_SOCKET": "", "HC_TERMINAL_ID": "", "ORLENKO_HERDR_DISPLAY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.record = {"member_id": "mb_test", "orchestra_id": "or_test", "provider": "test",
                       "owner_pid": 300, "name": "Reviewer", "hub_name": "Development",
                       "role": "player", "closed_at": None,
                       "cwd": self.tmp.name, "instance_key": core.instance_key("test", self.tmp.name)}
        member.save_member(self.record)

    def test_explicit_enrollment_checks_current_owner_and_open_state(self):
        with patch.object(core, "agent_session_pid", return_value=300), \
             patch.object(herdr_display, "bind", return_value=True) as bind, \
             patch.object(member, "_refresh_display") as refresh:
            result = member.set_herdr_display(self.record, True)
            self.assertTrue(result["enabled"])
            self.assertTrue(bind.call_args.kwargs["current"]())
            self.record["closed_at"] = 1
            member.save_member(self.record)
            self.assertFalse(bind.call_args.kwargs["current"]())
            refresh.assert_called_once_with("mb_test")

    def test_other_live_session_cannot_enroll(self):
        with patch.object(core, "agent_session_pid", return_value=301), \
             patch.object(herdr_display, "bind") as bind:
            self.assertFalse(member.set_herdr_display(self.record, True)["ok"])
            bind.assert_not_called()

    def test_metadata_is_fresh_and_allowlisted(self):
        self.record.update(role="conductor", name="Changed", token="never-display")
        member.save_member(self.record)
        with patch.object(herdr_display, "refresh") as refresh:
            member._refresh_display("mb_test")
            metadata = refresh.call_args.args[1]()
            self.assertEqual(metadata["role"], "conductor")
            self.assertEqual(metadata["name"], "Changed")
            self.assertEqual(metadata["_owner_pid"], 300)
            self.assertNotIn("token", metadata)
            self.record["closed_at"] = 1
            member.save_member(self.record)
            self.assertIsNone(refresh.call_args.args[1]())

    def test_close_paths_persist_before_display_retirement(self):
        for operation in ("leave", "close", "remote-410"):
            with self.subTest(operation=operation):
                self.record.update(closed_at=None, closed_reason=None)
                member.save_member(self.record)

                def invalidated(path, **kwargs):
                    self.assertTrue(member.load_member("mb_test")["closed_at"])
                    self.assertTrue(kwargs["disable"])

                with patch.object(member, "ensure_hub_if_local"), \
                     patch.object(member, "api_request", return_value={"ok": True}), \
                     patch.object(herdr_display, "invalidate", side_effect=invalidated) as invalidate:
                    if operation == "remote-410":
                        member._close_locally("mb_test", "kicked")
                    else:
                        getattr(member, operation)(self.record.copy())
                    invalidate.assert_called_once()

    def test_adoption_invalidates_prior_binding_before_rebinding(self):
        events = []
        with patch.object(core, "agent_session_pid", return_value=301), \
             patch.object(core, "agent_kind", return_value=None), \
             patch.object(member, "_pid_alive", return_value=False), \
             patch.object(member, "_bound_to_another_session", return_value=False), \
             patch.object(herdr_display, "invalidate", side_effect=lambda *a, **k: events.append("invalidate")), \
             patch.object(member, "_bind_display", side_effect=lambda *a, **k: events.append("bind")):
            result = member.adopt("mb_test", provider="test", cwd=self.tmp.name)
        self.assertEqual(result["owner_pid"], 301)
        self.assertEqual(events, ["invalidate", "bind"])

    def test_enable_disable_parser(self):
        for action in ("enable", "disable"):
            args = cli.build_parser().parse_args(["herdr-display", action, "--member-id", "mb_test", "--json"])
            self.assertEqual(args.command, "herdr-display")
            self.assertEqual(args.action, action)

    def test_missing_display_state_is_inert(self):
        with patch.object(core, "agent_session_pid", side_effect=AssertionError("no process walk")), \
             patch.object(herdr_display, "bind") as bind:
            self.assertFalse(member._bind_display(self.record))
            bind.assert_not_called()


if __name__ == "__main__":
    unittest.main()
