from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import codex_wake, core


class CodexWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-wake-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.thread = str(uuid.uuid4())
        self.fake = self.root / "codex"
        self.log = self.root / "queue.jsonl"
        self.fake.write_text("#!" + sys.executable + "\n" +
            "import json, os, sys\n" +
            "with open(os.environ['QUEUE_LOG'], 'a') as f:\n" +
            " json.dump({'args':sys.argv[1:], 'home':os.environ['CODEX_HOME'], " +
            "'bypass':os.environ.get('AIQ_BYPASS')}, f); f.write('\\n')\n")
        self.fake.chmod(0o700)
        patcher = mock.patch.dict(os.environ, {
            "AGENT_ORCHESTRA_HOME": str(self.root / "state"),
            "AGENT_ORCHESTRA_CODEX_BIN": str(self.fake), "AGENT_ORCHESTRA_NO_WAIT": "0",
            "CODEX_THREAD_ID": self.thread, "CODEX_HOME": str(self.root / "account"),
            "QUEUE_LOG": str(self.log),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mailbox = "mb_aaaa1111"
        self.buckets = [self.root / "pending", self.root / "claimed"]
        for path in self.buckets:
            path.mkdir()
        codex_wake.register(self.mailbox, prefix="AGENT_ORCHESTRA")

    def mail(self, name="m_aaaaaaaa", bucket=0):
        path = self.buckets[bucket] / (name + ".json")
        path.write_text(json.dumps({"id": name, "text": "UNTRUSTED_BODY"}))
        return path

    def wake(self):
        codex_wake.wake(self.mailbox, buckets=self.buckets,
            executable=Path("/plugin with spaces/bin/agent-orchestra"),
            id_flag="--member-id", label="Agent Orchestra")

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_idle_mail_queues_exact_thread_and_home_without_claiming(self):
        row = self.mail()
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/wrong-account"}):
            self.wake()
        call, = self.calls()
        self.assertEqual(call["args"][:3], ["queue", "--thread", self.thread])
        self.assertEqual(call["home"], str((self.root / "account").resolve()))
        self.assertEqual(call["bypass"], "1")
        self.assertNotIn("UNTRUSTED_BODY", call["args"][-1])
        self.assertIn("--member-id mb_aaaa1111", call["args"][-1])
        self.assertIn("untrusted collaboration input", call["args"][-1])
        self.assertTrue(row.exists())
        self.assertEqual(codex_wake.capability(self.mailbox)["state"], "armed")

    def test_restart_does_not_repeat_notice_but_new_mail_does(self):
        row = self.mail()
        self.wake()
        importlib.reload(codex_wake)
        self.wake()
        row.rename(self.buckets[1] / row.name)
        self.wake()
        self.assertEqual(len(self.calls()), 1)
        self.mail("m_bbbbbbbb")
        self.wake()
        self.assertEqual(len(self.calls()), 2)

    def test_retry_after_failure_and_recovery_is_observable(self):
        self.mail()
        with mock.patch.object(codex_wake.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 1, "", "queue unsupported")) as run:
            with mock.patch.object(codex_wake.time, "time", return_value=100):
                self.wake()
                self.wake()
            self.assertEqual(run.call_count, 1)
        status = codex_wake.capability(self.mailbox)
        self.assertFalse(status["idle_reawaken"])
        self.assertIn("queue unsupported", status["last_error"])
        with mock.patch.object(codex_wake.time, "time", return_value=116):
            self.wake()
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(codex_wake.capability(self.mailbox)["idle_reawaken"])

    def test_timeout_does_not_retire_mail(self):
        row = self.mail()
        with mock.patch.object(codex_wake.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("codex queue", 10)):
            self.wake()
        self.assertTrue(row.exists())
        self.assertEqual(codex_wake.capability(self.mailbox)["state"], "error")

    def test_no_mail_means_no_queue(self):
        self.wake()
        self.assertEqual(self.calls(), [])

    def test_rebinding_wakes_new_session_for_unhandled_mail(self):
        self.mail()
        self.wake()
        other = str(uuid.uuid4())
        codex_wake.register(self.mailbox, session_id=other, prefix="AGENT_ORCHESTRA")
        self.wake()
        self.assertEqual(self.calls()[-1]["args"][2], other)
        self.assertEqual(len(self.calls()), 2)

    def test_missing_thread_and_opt_out_do_not_bind(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            codex_wake.register("missing", prefix="AGENT_ORCHESTRA")
        with mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_NO_WAIT": "1"}):
            codex_wake.register("disabled", prefix="AGENT_ORCHESTRA")
        self.assertEqual(codex_wake.target("missing"), {})
        self.assertEqual(codex_wake.target("disabled"), {})

    def test_parallel_delivery_has_one_queue_writer(self):
        import fcntl
        self.mail()
        lock = core.runtime_dir() / f"{self.mailbox}.codex-queue.lock"
        with lock.open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.wake()
        self.assertEqual(self.calls(), [])
        self.wake()
        self.assertEqual(len(self.calls()), 1)

    def test_monitor_adapter_only_wakes_codex_open_mailboxes(self):
        from agent_orchestra import member as transport
        entity = {"member_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        with mock.patch.object(codex_wake, "wake") as wake:
            transport._wake_codex(entity)
            self.assertEqual(wake.call_count, 1)
            transport._wake_codex({**entity, "provider": "claude"})
            transport._wake_codex({**entity, "closed_at": 123})
            self.assertEqual(wake.call_count, 1)


    def test_monitor_restart_checks_process_identity(self):
        from agent_orchestra import member as transport
        entity = {"member_id": self.mailbox}
        state = transport._monitor_state_path(self.mailbox)
        core.atomic_write_json(state, {"pid": 98765})
        with mock.patch.object(transport, "_pid_alive", return_value=True), \
             mock.patch.object(transport.subprocess, "run", return_value=
                 subprocess.CompletedProcess([], 0, "python another_job.py", "")), \
             mock.patch.object(transport.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                transport.restart_monitor(entity)
            kill.assert_not_called()
        command = f"python -m agent_orchestra monitor-run --member-id {self.mailbox}"
        with mock.patch.object(transport, "_pid_alive", return_value=True), \
             mock.patch.object(transport.subprocess, "run", return_value=
                 subprocess.CompletedProcess([], 0, command, "")), \
             mock.patch.object(transport.os, "kill") as kill, \
             mock.patch.object(transport, "start_monitor", return_value=222):
            self.assertEqual(transport.restart_monitor(entity), 222)
            kill.assert_called_once_with(98765, transport.signal.SIGTERM)

    def test_hook_cannot_bind_a_sibling_thread_in_the_same_directory(self):
        from agent_orchestra import member as transport
        entity = {"member_id": self.mailbox, "provider": "codex",
                  "cwd": str(self.root), "instance_key": core.instance_key("codex", self.root),
                  "expires_at": 9999999999, "created_at": 1, "joined_at": 1,
                  "owner_pid": os.getpid(), "session_id": self.thread}
        core.atomic_write_json(core.member_path(self.mailbox), entity)
        from agent_orchestra import hooks
        # Two Codex threads may have the SAME app-server ancestor process.
        with mock.patch.object(hooks, "ensure_monitor", return_value=0), \
             mock.patch.object(core, "agent_ancestor_pid", return_value=os.getpid()):
            sibling = hooks.hook_member("codex", {"cwd": str(self.root), "session_id": str(uuid.uuid4()), "hook_event_name": "Stop"})
            self.assertIsNone(sibling)
            bound = hooks.hook_member("codex", {"cwd": str(self.root), "session_id": self.thread})
        self.assertIsNotNone(bound)
        self.assertEqual(codex_wake.target(self.mailbox)["thread_id"], self.thread)


    def test_monitor_delivery_wakes_without_any_followup_hook(self):
        from agent_orchestra import member as transport
        entity = {"member_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        message = {"id": "m_abcdef123456", "text": "UNTRUSTED_BODY", "from": {"id": "peer"}}
        def api(_entity, method, route, *args, **kwargs):
            if route == "/v1/messages/pending":
                return {"messages": [message]}
            return {}
        with mock.patch.object(transport, "load_member", side_effect=[entity, {**entity, "closed_at": 1}]), \
             mock.patch.object(transport, "api_request", side_effect=api), \
             mock.patch.object(transport, "flush_handled"), \
             mock.patch.object(transport, "flush_outbox"), \
             mock.patch.object(transport, "_notify"):
            with mock.patch.object(transport, "ensure_hub_if_local"), \
                 mock.patch.object(transport, "_save_task_snapshot"):
                transport.monitor_loop(self.mailbox)
        call, = self.calls()
        self.assertEqual(call["args"][2], self.thread)
        self.assertNotIn("UNTRUSTED_BODY", call["args"][-1])
        self.assertTrue((core.bucket_dir(self.mailbox, "pending") / (message["id"] + ".json")).exists())

    def test_system_presence_event_does_not_wake_codex(self):
        from agent_orchestra import member as transport
        entity = {"member_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        message = {"id": "m_abcdef123456", "text": "UNTRUSTED_BODY", "from": {"id": "sys"}}
        def api(_entity, method, route, *args, **kwargs):
            if route == "/v1/messages/pending":
                return {"messages": [message]}
            return {}
        with mock.patch.object(transport, "load_member", side_effect=[entity, {**entity, "closed_at": 1}]), \
             mock.patch.object(transport, "api_request", side_effect=api), \
             mock.patch.object(transport, "flush_handled"), \
             mock.patch.object(transport, "flush_outbox"), \
             mock.patch.object(transport, "_notify"):
            with mock.patch.object(transport, "ensure_hub_if_local"), \
                 mock.patch.object(transport, "_save_task_snapshot"):
                transport.monitor_loop(self.mailbox)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
