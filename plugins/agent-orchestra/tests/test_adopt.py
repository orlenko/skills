"""A seat stays with the agent that joined it until `adopt` moves it."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import codex_wake, core, hooks, member as member_module, terminal_control  # noqa: E402
from agent_orchestra.core import atomic_write_json, instance_key, member_path, runtime_dir  # noqa: E402

MEMBER = "mb_trumpet0001"
AIQ_TERMINAL = "herdr:" + "1" * 32 + "-" + "2" * 32


class AdoptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-adopt-")
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name) / "ops"
        self.cwd.mkdir()
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": str(Path(temp.name) / "state"),
                                              "AGENT_CODEX_WAKE_HOME": str(Path(temp.name) / "wake"),
                                              "HC_TERMINAL_SOCKET": "", "HC_TERMINAL_ID": ""})
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

    def test_a_session_adopts_only_as_its_own_agent(self):
        with self.assertRaisesRegex(core.OrchestraError, "This session is claude"):
            member_module.adopt(MEMBER, provider="codex", cwd=str(self.cwd))
        self.assertIsNone(member_module.load_member(MEMBER).get("adopted"))

    def test_adoption_waits_for_the_old_codex_dispatch_before_retiring_its_target(self):
        target = runtime_dir() / f"{MEMBER}.codex-session.json"
        binding = {"thread_id": "fixture-thread", "codex_home": "fixture-home", "executable": "fixture"}
        atomic_write_json(target, binding)
        waiting = threading.Event()
        result = {}
        lock = codex_wake._session_lock

        def signal_attempt(value, *, wait=False):
            waiting.set()
            return lock(value, wait=wait)

        def transfer():
            result.update(member_module.adopt(MEMBER, provider="claude", cwd=str(self.cwd)))

        with lock(binding) as acquired, mock.patch.object(codex_wake, "_session_lock", side_effect=signal_attempt):
            self.assertTrue(acquired)
            worker = threading.Thread(target=transfer)
            worker.start()
            self.assertTrue(waiting.wait(2))
            self.assertEqual(member_module.load_member(MEMBER)["provider"], "codex")
            self.assertTrue(target.exists())
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result["provider"], "claude")
        self.assertFalse(target.exists())

    def test_a_closed_seat_is_not_adopted(self):
        seat = member_module.load_member(MEMBER)
        seat["closed_at"] = 1.0
        member_module.save_member(seat)
        with self.assertRaisesRegex(core.OrchestraError, "closed"):
            member_module.adopt(MEMBER, provider="claude", cwd=str(self.cwd))

    def native_predecessor(self, *, wrapper=False, subdirectory=False):
        old_owner = 500
        seat = member_module.load_member(MEMBER)
        terminal = {"core": "test-core", "address": "t1", "pid": 100,
                    "created_ms": 5000, "root_start": [12]}
        old_cwd = self.cwd / "subpackage" if subdirectory else self.cwd
        old_cwd.mkdir(exist_ok=True)
        seat.update(provider="claude", cwd=str(old_cwd), instance_key=instance_key("claude", old_cwd),
                    owner_pid=old_owner, session_id="old-claude-session",
                    terminal_control={**terminal, "owner_pid": old_owner,
                                      "aiq_herdr": {"terminal": AIQ_TERMINAL, "generation": 1,
                                                    "launch": 400}})
        member_module.save_member(seat)
        core.agent_kind.side_effect = lambda pid: "codex" if pid == self.session else None
        launch = self.session + 1 if wrapper else self.session
        directory = self.cwd.parent / "aiq" / "herdr"
        record = directory / ("1" * 32 + ".json")
        atomic_write_json(record, {
            "Ref": AIQ_TERMINAL, "State": "running", "Generation": 2, "PID": launch,
            "PreviousPID": 400, "RunnerPID": 100, "Workspace": str(self.cwd),
            "Core": {"ID": "test-core", "Terminal": "t1", "PID": 100},
        })
        process = mock.patch.object(terminal_control, "process", side_effect={
            self.session: terminal_control.Process(launch if wrapper else 100, launch, 1, launch, (20,)),
            launch: terminal_control.Process(100, launch, 1, launch, (20,)),
        }.__getitem__)
        process.start()
        self.addCleanup(process.stop)
        env = {"AIQ_LONG": "1", "AIQ_TERMINAL": AIQ_TERMINAL,
               "AIQ_TERMINAL_PID": str(launch), "AIQ_TERMINAL_GENERATION": "2",
               "AIQ_DATA_DIR": str(directory.parent)}
        binding = {**terminal, "owner_pid": self.session,
                   "aiq_herdr": {"terminal": AIQ_TERMINAL, "generation": 2, "launch": launch,
                                 "previous_launch": 400, "workspace": str(self.cwd),
                                 "core": "test-core", "address": "t1", "pid": 100}}
        return seat, env, binding, record

    def test_aiq_successor_binds_as_codex_on_session_start(self):
        _, env, binding, _ = self.native_predecessor(subdirectory=True)
        wake = runtime_dir() / f"{MEMBER}.codex-session.json"
        atomic_write_json(wake, {"thread_id": "old-codex-thread"})

        def verify_thread_published_before_display(*args, **kwargs):
            self.assertEqual(member_module.load_member(MEMBER)["session_id"], "new-codex-thread")

        with mock.patch.dict(os.environ, env), \
                mock.patch.object(core, "agent_ancestor_pid", return_value=self.session), \
                mock.patch.object(member_module, "_pid_alive", side_effect=lambda pid: pid == self.session), \
                mock.patch.object(terminal_control, "capture_binding", return_value=binding), \
                mock.patch.object(member_module, "_bind_display", side_effect=verify_thread_published_before_display), \
                mock.patch.object(hooks, "ensure_monitor"), \
                mock.patch.object(hooks.codex_wake, "register") as register:
            member = hooks.hook_member("codex", {"cwd": str(self.cwd), "session_id": "new-codex-thread",
                                               "hook_event_name": "SessionStart"})
        self.assertEqual(member["member_id"], MEMBER)
        self.assertEqual(member["provider"], "codex")
        self.assertEqual(member["cwd"], str(self.cwd.resolve()))
        self.assertEqual(member["session_id"], "new-codex-thread")
        self.assertEqual(member["terminal_control"], binding)
        self.assertFalse(wake.exists())
        register.assert_called_once_with(MEMBER, session_id="new-codex-thread", prefix="AGENT_ORCHESTRA")

    def test_aiq_successor_rejects_unproved_replacement(self):
        old, env, binding, record = self.native_predecessor()
        for reason, overrides, new_binding, live in (
            ("live predecessor", {}, binding, True),
            ("different terminal", {}, {**binding, "created_ms": 6000}, False),
            ("skipped generation", {"AIQ_TERMINAL_GENERATION": "3"}, binding, False),
            ("nested session", {"AIQ_TERMINAL_PID": "900"}, binding, False),
            ("no terminal proof", {}, None, False),
        ):
            with self.subTest(reason=reason), mock.patch.dict(os.environ, {**env, **overrides}), \
                    mock.patch.object(core, "agent_ancestor_pid", return_value=self.session), \
                    mock.patch.object(member_module, "_pid_alive", return_value=live), \
                    mock.patch.object(terminal_control, "capture_binding", return_value=new_binding):
                member = hooks.hook_member("codex", {"cwd": str(self.cwd), "session_id": "new-codex-thread",
                                                   "hook_event_name": "SessionStart"})
                self.assertIsNone(member)
                self.assertEqual(member_module.load_member(MEMBER), old)
        unrelated = core.read_json(record)
        unrelated["PreviousPID"] = 999
        atomic_write_json(record, unrelated)
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(member_module, "_pid_alive", return_value=False):
            self.assertIsNone(member_module.adopt_aiq_herdr("codex", str(self.cwd), self.session, "new-codex-thread"))
        self.assertEqual(member_module.load_member(MEMBER), old)

    def test_nested_agent_cannot_use_the_direct_launchers_runner_record(self):
        old, env, _, _ = self.native_predecessor(wrapper=True)
        core.agent_kind.side_effect = lambda _: "codex"
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(terminal_control.aiq_herdr_context(self.session))
        self.assertEqual(member_module.load_member(MEMBER), old)

    def test_prior_codex_thread_cannot_reuse_a_binding_after_round_trip(self):
        cwd = str(self.cwd)
        path = hooks._binding_path("codex", cwd, "old-codex-thread")
        binding = {"member_id": MEMBER, "owner_pid": self.session, "session_id": "old-codex-thread"}
        atomic_write_json(path, binding)
        member_module.adopt(MEMBER, provider="claude", cwd=cwd)
        self.assertFalse(path.exists())
        core.agent_kind.return_value = "codex"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "new-codex-thread"}):
            member_module.adopt(MEMBER, provider="codex", cwd=cwd)
        self.assertEqual(member_module.load_member(MEMBER)["session_id"], "new-codex-thread")
        atomic_write_json(path, binding)
        with mock.patch.object(core, "agent_ancestor_pid", return_value=self.session):
            member = hooks.hook_member("codex", {"cwd": cwd, "session_id": "old-codex-thread"})
        self.assertIsNone(member)
        self.assertEqual(member_module.load_member(MEMBER)["session_id"], "new-codex-thread")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "old-codex-thread"}):
            with self.assertRaisesRegex(core.OrchestraError, "belongs to another Codex thread"):
                member_module.select_member(provider="codex", cwd=cwd, member_id=MEMBER)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "new-codex-thread"}):
            current = member_module.select_member(provider="codex", cwd=cwd, member_id=MEMBER)
        self.assertEqual(current["member_id"], MEMBER)

    def test_prior_notice_cannot_use_a_seat_before_the_successor_thread_is_known(self):
        core.agent_kind.return_value = "codex"
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            member_module.adopt(MEMBER, provider="codex", cwd=str(self.cwd))
        self.assertNotIn("session_id", member_module.load_member(MEMBER))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "old-codex-thread"}):
            with self.assertRaisesRegex(core.OrchestraError, "successor's Codex thread binding"):
                member_module.select_member(provider="codex", cwd=str(self.cwd), member_id=MEMBER)

    def test_hook_cannot_return_context_for_a_seat_that_just_moved(self):
        seat = member_module.load_member(MEMBER)
        path = hooks._binding_path("codex", str(self.cwd), "old-codex-thread")
        with mock.patch.object(hooks, "claim_ownership", side_effect=lambda _: member_module.adopt(
            MEMBER, provider="claude", cwd=str(self.cwd)
        )):
            with self.assertRaisesRegex(core.OrchestraError, "belongs to another session"):
                hooks._finish_hook_binding(seat, "codex", str(self.cwd), "old-codex-thread", self.session, path)
        self.assertFalse(path.exists())

    def test_stale_hook_cannot_register_wake_or_read_context(self):
        seat = member_module.load_member(MEMBER)
        member_module.adopt(MEMBER, provider="claude", cwd=str(self.cwd))
        payload = {"cwd": str(self.cwd), "session_id": "old-codex-thread"}
        with mock.patch.object(hooks, "_bound_hook_member", return_value=seat), \
                mock.patch.object(hooks.codex_wake, "register") as register:
            self.assertIsNone(hooks.hook_member("codex", payload))
        register.assert_not_called()
        with mock.patch.object(hooks, "hook_member", return_value=seat), \
                mock.patch.object(hooks, "_mail_lines") as read:
            self.assertEqual(hooks.hook_context("codex", payload), {})
        read.assert_not_called()


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
