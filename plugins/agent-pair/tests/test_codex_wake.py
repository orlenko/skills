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
from agent_pair import codex_wake, core


_FAKE_CODEX = r"""
import json, os, sys, uuid
from pathlib import Path
state_path = Path(os.environ['QUEUE_STATE'])
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']; params = message.get('params', {})
    state = json.loads(state_path.read_text()) if state_path.exists() else []
    if method == 'thread/queue/list':
        result = {'data': state, 'nextCursor': None}
    elif method == 'thread/queue/delete':
        state = [x for x in state if x['id'] != params['queuedSubmissionId']]
        state_path.write_text(json.dumps(state)); result = {'deleted': True}
    elif method == 'thread/queue/add':
        entry = {'id': str(uuid.uuid4()), 'input': params['input'], 'clientUserMessageId': params['clientUserMessageId']}
        state.append(entry); state_path.write_text(json.dumps(state))
        with open(os.environ['QUEUE_LOG'], 'a') as f:
            json.dump({'args':['queue','--thread',params['threadId'],'--message',params['input'][0]['text']], 'home':os.environ['CODEX_HOME'],'bypass':os.environ.get('AIQ_BYPASS')}, f); f.write('\n')
        result = {'queuedSubmission': entry}
    else:
        result = {}
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
"""


class CodexWakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-wake-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.thread = str(uuid.uuid4())
        self.fake = self.root / "codex"
        self.log = self.root / "queue.jsonl"
        self.fake.write_text("#!" + sys.executable + "\n" + _FAKE_CODEX)
        self.fake.chmod(0o700)
        patcher = mock.patch.dict(os.environ, {
            "AGENT_PAIR_HOME": str(self.root / "state"),
            "AGENT_PAIR_CODEX_BIN": str(self.fake), "AGENT_PAIR_NO_WAIT": "0",
            "CODEX_THREAD_ID": self.thread, "CODEX_HOME": str(self.root / "account"),
            "QUEUE_LOG": str(self.log), "QUEUE_STATE": str(self.root / "queue-state.json"),
            "AGENT_CODEX_WAKE_HOME": str(self.root / "wake-state"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mailbox = "pair_test-peer_test"
        self.buckets = [self.root / "pending", self.root / "claimed"]
        for path in self.buckets:
            path.mkdir()
        self.clock = mock.patch.object(codex_wake.time, "time", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        codex_wake.register(self.mailbox, prefix="AGENT_PAIR")

    def mail(self, name="m_aaaaaaaa", bucket=0):
        path = self.buckets[bucket] / (name + ".json")
        path.write_text(json.dumps({"id": name, "text": "UNTRUSTED_BODY"}))
        return path

    def wake(self):
        codex_wake.wake(self.mailbox, buckets=self.buckets,
            executable=Path("/plugin with spaces/bin/agent-pair"),
            id_flag="--endpoint-id", label="Agent Pair")

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
        self.assertIn("--endpoint-id pair_test-peer_test", call["args"][-1])
        self.assertIn("untrusted collaboration input", call["args"][-1])
        self.assertTrue(row.exists())
        self.assertEqual(codex_wake.capability(self.mailbox)["state"], "armed")

    def test_restart_does_not_repeat_notice_and_new_mail_waits_a_minute(self):
        row = self.mail()
        self.wake()
        importlib.reload(codex_wake)
        # Reloading the adapter must not forget either receipt or cooldown.
        (self.root / "queue-state.json").write_text("[]")
        self.wake()  # detect dispatch at t=1000
        row.rename(self.buckets[1] / row.name)
        self.mail("m_bbbbbbbb")
        self.now.return_value = 1059
        self.wake()
        self.assertEqual(len(self.calls()), 1)
        self.now.return_value = 1060
        self.wake()
        self.assertEqual(len(self.calls()), 2)

    def test_retry_after_failure_obeys_the_same_minute_limit(self):
        self.mail()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.notices.return_value = []
            client.request.side_effect = RuntimeError("queue unsupported")
            self.wake()
            self.wake()
            self.assertEqual(client.request.call_count, 1)
        self.assertFalse(codex_wake.capability(self.mailbox)["idle_reawaken"])
        self.now.return_value = 1059
        self.wake()
        self.assertEqual(self.calls(), [])
        self.now.return_value = 1060
        self.wake()
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(codex_wake.capability(self.mailbox)["idle_reawaken"])

    def test_timeout_does_not_retire_mail_or_retry_early(self):
        row = self.mail()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.notices.return_value = []
            client.request.side_effect = TimeoutError("queue timeout")
            self.wake()
        self.assertTrue(row.exists())
        self.assertEqual(codex_wake.capability(self.mailbox)["state"], "error")
        self.now.return_value = 1059
        self.wake()
        self.assertEqual(self.calls(), [])

    def test_a_thread_codex_cannot_find_backs_off_doubling(self):
        # 2026-09-24: a Claude session id bound as a Codex thread failed at
        # thread/queue/list, before any attempt was recorded, and the monitor
        # started `codex app-server` twice a pass for two weeks.
        self.mail()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            client = factory.return_value.__enter__.return_value
            client.notices.side_effect = RuntimeError("no rollout found for thread id")
            self.wake()
            self.now.return_value = 1059
            self.wake()
            self.assertEqual(factory.call_count, 1)
            self.now.return_value = 1060
            self.wake()
            self.assertEqual(factory.call_count, 2)
            self.now.return_value = 1179
            self.wake()
            self.assertEqual(factory.call_count, 2)
            self.now.return_value = 1180
            self.wake()
            self.assertEqual(factory.call_count, 3)
        self.assertEqual(codex_wake.capability(self.mailbox)["state"], "error")

    def test_backoff_caps_and_clears_on_success(self):
        self.mail()
        receipt_path = core.runtime_dir() / f"{self.mailbox}.codex-wake.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        receipt.update(target=codex_wake.target(self.mailbox), failures=20, failed_at=1000,
                       last_error="old")
        receipt_path.write_text(json.dumps(receipt))
        self.now.return_value = 1000 + codex_wake.FAILURE_BACKOFF_MAX_SECONDS - 1
        self.wake()
        self.assertEqual(self.calls(), [])
        self.now.return_value = 1000 + codex_wake.FAILURE_BACKOFF_MAX_SECONDS
        self.wake()
        self.assertEqual(len(self.calls()), 1)
        receipt = json.loads(receipt_path.read_text())
        self.assertNotIn("failures", receipt)
        self.assertIsNone(receipt["last_error"])

    def test_rebinding_ends_the_backoff(self):
        self.mail()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            factory.return_value.__enter__.return_value.notices.side_effect = RuntimeError("gone")
            self.wake()
        codex_wake.register(self.mailbox, session_id=str(uuid.uuid4()), prefix="AGENT_PAIR")
        self.wake()
        self.assertEqual(len(self.calls()), 1)

    def test_observed_skips_the_queue_while_backing_off(self):
        self.mail()
        self.wake()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            factory.return_value.__enter__.return_value.notices.side_effect = RuntimeError("gone")
            codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
            self.assertEqual(factory.call_count, 1)
            codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
            self.assertEqual(factory.call_count, 1)

    def test_observed_from_a_hook_does_not_wait_for_the_lock(self):
        self.mail()
        self.wake()
        binding = codex_wake.target(self.mailbox)
        with codex_wake._session_lock(binding) as held, \
                mock.patch.object(codex_wake.time, "sleep") as sleep, \
                mock.patch.object(codex_wake, "_QueueClient") as factory:
            self.assertTrue(held)
            codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair", wait=False)
        sleep.assert_not_called()
        factory.assert_not_called()

    def test_no_mail_means_no_queue(self):
        self.wake()
        self.assertEqual(self.calls(), [])

    def test_rebinding_wakes_new_session_for_unhandled_mail(self):
        self.mail()
        self.wake()
        (self.root / "queue-state.json").write_text("[]")
        other = str(uuid.uuid4())
        codex_wake.register(self.mailbox, session_id=other, prefix="AGENT_PAIR")
        self.wake()
        self.assertEqual(self.calls()[-1]["args"][2], other)
        self.assertEqual(len(self.calls()), 2)

    def test_missing_thread_and_opt_out_do_not_bind(self):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            codex_wake.register("missing", prefix="AGENT_PAIR")
        with mock.patch.dict(os.environ, {"AGENT_PAIR_NO_WAIT": "1"}):
            codex_wake.register("disabled", prefix="AGENT_PAIR")
        self.assertEqual(codex_wake.target("missing"), {})
        self.assertEqual(codex_wake.target("disabled"), {})

    def test_parallel_delivery_has_one_queue_writer(self):
        import fcntl
        self.mail()
        lock = codex_wake._session_path(codex_wake.target(self.mailbox)).with_suffix(".lock")
        with lock.open("w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.wake()
        self.assertEqual(self.calls(), [])
        self.wake()
        self.assertEqual(len(self.calls()), 1)

    def test_monitor_adapter_only_wakes_codex_open_mailboxes(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        with mock.patch.object(codex_wake, "wake") as wake:
            transport._wake_codex(entity)
            self.assertEqual(wake.call_count, 1)
            transport._wake_codex({**entity, "provider": "claude"})
            transport._wake_codex({**entity, "closed_at": 123})
            self.assertEqual(wake.call_count, 1)


    def test_stale_heartbeat_does_not_spawn_a_second_live_monitor(self):
        from agent_pair import client as transport, monitor_lock
        entity = {"endpoint_id": self.mailbox}
        core.atomic_write_json(transport._monitor_state_path(self.mailbox),
                               {"pid": os.getpid(), "updated_at": 1})
        with monitor_lock.hold(self.mailbox) as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(transport, "_spawn_module") as spawn:
                self.assertEqual(transport.start_monitor(entity), os.getpid())
                spawn.assert_not_called()
        self.assertEqual(monitor_lock.owner(self.mailbox), 0)

    def test_duplicate_monitor_cannot_poll_or_wake(self):
        from agent_pair import client as transport, monitor_lock
        with monitor_lock.hold(self.mailbox):
            with mock.patch.object(transport, "_monitor_loop") as loop:
                transport.monitor_loop(self.mailbox)
                loop.assert_not_called()
        with mock.patch.object(transport, "_monitor_loop") as loop:
            transport.monitor_loop(self.mailbox)
            loop.assert_called_once_with(self.mailbox)
        self.assertEqual(monitor_lock.owner(self.mailbox), 0)

    def test_monitor_restart_checks_process_identity(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox}
        state = transport._monitor_state_path(self.mailbox)
        core.atomic_write_json(state, {"pid": 98765})
        with mock.patch.object(transport, "_pid_alive", return_value=True), \
             mock.patch.object(transport.subprocess, "run", return_value=
                 subprocess.CompletedProcess([], 0, "python another_job.py", "")), \
             mock.patch.object(transport.os, "kill") as kill:
            with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
                transport.restart_monitor(entity)
            kill.assert_not_called()
        command = f"python -m agent_pair monitor-run --endpoint-id {self.mailbox}"
        with mock.patch.object(transport, "_pid_alive", return_value=True), \
             mock.patch.object(transport.subprocess, "run", return_value=
                 subprocess.CompletedProcess([], 0, command, "")), \
             mock.patch.object(transport.os, "kill") as kill, \
             mock.patch.object(transport, "start_monitor", return_value=222):
            self.assertEqual(transport.restart_monitor(entity), 222)
            kill.assert_called_once_with(98765, transport.signal.SIGTERM)

    def test_hook_cannot_bind_a_sibling_thread_in_the_same_directory(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox, "provider": "codex",
                  "cwd": str(self.root), "instance_key": core.instance_key("codex", self.root),
                  "expires_at": 9999999999, "created_at": 1, "joined_at": 1,
                  "owner_pid": os.getpid(), "session_id": self.thread}
        core.atomic_write_json(core.endpoint_path(self.mailbox), entity)
        with mock.patch.object(transport, "ensure_monitor", return_value=0):
            bound = transport.hook_endpoint("codex", {"cwd": str(self.root), "session_id": self.thread})
            sibling = transport.hook_endpoint("codex", {"cwd": str(self.root), "session_id": str(uuid.uuid4())})
        self.assertIsNotNone(bound)
        self.assertIsNone(sibling)
        self.assertEqual(codex_wake.target(self.mailbox)["thread_id"], self.thread)


    def test_monitor_delivery_wakes_without_any_followup_hook(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        message = {"id": "m_abcdef123456", "text": "UNTRUSTED_BODY", "from": {"id": "peer"}}
        def api(_entity, method, route, *args, **kwargs):
            if route == "/v1/messages/pending":
                return {"messages": [message]}
            return {}
        with mock.patch.object(transport, "load_endpoint", side_effect=[entity, {**entity, "closed_at": 1}]), \
             mock.patch.object(transport, "api_request", side_effect=api), \
             mock.patch.object(transport, "flush_handled"), \
             mock.patch.object(transport, "flush_outbox"), \
             mock.patch.object(transport, "_notify"):
            transport.monitor_loop(self.mailbox)
        call, = self.calls()
        self.assertEqual(call["args"][2], self.thread)
        self.assertNotIn("UNTRUSTED_BODY", call["args"][-1])
        self.assertTrue((core.inbox_dir(self.mailbox, "pending") / (message["id"] + ".json")).exists())


    def queued(self):
        path = self.root / "queue-state.json"
        return json.loads(path.read_text()) if path.exists() else []

    def test_empty_inbox_cancels_a_previously_queued_notice(self):
        row = self.mail()
        self.wake()
        row.unlink()
        self.wake()
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.calls()), 1)

    def test_inbox_drained_while_reading_queue_does_not_wake(self):
        row = self.mail()
        with mock.patch.object(codex_wake, "_QueueClient") as factory:
            client = factory.return_value.__enter__.return_value
            def drain(*args):
                row.unlink()
                return []
            client.notices.side_effect = drain
            self.wake()
            client.request.assert_not_called()

    def test_done_row_wins_over_a_pending_file_during_finish(self):
        row = self.mail()
        done = self.root / "done"
        done.mkdir()
        (done / row.name).write_text(row.read_text())
        self.wake()
        self.assertEqual(self.calls(), [])

    def test_observed_mail_cancels_queue_before_inbox_is_drained(self):
        self.mail()
        self.wake()
        codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
        self.assertEqual(self.queued(), [])
        self.now.return_value = 1061
        self.wake()
        self.assertEqual(len(self.calls()), 1)

    def test_messages_handled_during_cooldown_do_not_trigger_trailing_wake(self):
        row = self.mail()
        self.wake()
        codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
        row.unlink()
        self.now.return_value = 1010
        new = self.mail("m_bbbbbbbb")
        self.wake()
        new.unlink()
        self.now.return_value = 1061
        self.wake()
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.calls()), 1)

    def test_only_one_notice_waits_behind_a_busy_turn(self):
        self.mail()
        self.wake()
        self.now.return_value = 1200
        self.mail("m_bbbbbbbb")
        self.wake()
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(len(self.calls()), 1)

    def test_cooldown_starts_at_dispatch_not_just_enqueue(self):
        self.mail()
        self.wake()
        self.now.return_value = 1200  # a long active turn finally ends
        (self.root / "queue-state.json").write_text("[]")
        self.mail("m_bbbbbbbb")
        self.wake()
        self.now.return_value = 1259
        self.wake()
        self.assertEqual(len(self.calls()), 1)
        self.now.return_value = 1260
        self.wake()
        self.assertEqual(len(self.calls()), 2)

    def test_other_mailbox_in_same_thread_shares_the_cooldown(self):
        self.mail()
        self.wake()
        codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
        other = "other_mailbox"
        codex_wake.register(other, prefix="AGENT_PAIR")
        args = dict(buckets=self.buckets, executable=Path("/plugin/bin/agent-pair"),
                    id_flag="--endpoint-id", label="Agent Pair")
        self.now.return_value = 1059
        codex_wake.wake(other, **args)
        self.assertEqual(len(self.calls()), 1)
        self.now.return_value = 1060
        codex_wake.wake(other, **args)
        self.assertEqual(len(self.calls()), 2)

    def test_old_duplicate_notices_are_cancelled_without_touching_user_queue(self):
        self.mail()
        self.wake()
        ours = self.queued()[0]
        duplicate = {**ours, "id": "duplicate"}
        human = {"id": "human", "input": [{"type": "text", "text": "Please continue my task"}]}
        (self.root / "queue-state.json").write_text(json.dumps([ours, duplicate, human]))
        receipt = core.runtime_dir() / f"{self.mailbox}.codex-wake.json"
        data = json.loads(receipt.read_text())
        for key in ("queue_checked", "queued_id", "client_id"):
            data.pop(key, None)
        receipt.write_text(json.dumps(data))  # receipt from the first release
        codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
        self.assertEqual(self.queued(), [human])

    def test_binary_path_change_does_not_reset_deduplication(self):
        self.mail()
        self.wake()
        codex_wake.observed(self.mailbox, ["m_aaaaaaaa"], label="Agent Pair")
        other = self.root / "updated-codex"
        other.write_bytes(self.fake.read_bytes())
        other.chmod(0o700)
        with mock.patch.dict(os.environ, {"AGENT_PAIR_CODEX_BIN": str(other)}):
            codex_wake.register(self.mailbox, prefix="AGENT_PAIR")
        self.now.return_value = 1100
        self.wake()
        self.assertEqual(len(self.calls()), 1)

    def test_finish_cancels_notice_before_retiring_inbox(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        directory = core.inbox_dir(self.mailbox, "pending")
        core.atomic_write_json(directory / "m_aaaaaaaa.json", {"id": "m_aaaaaaaa", "text": "mail"})
        transport._wake_codex(entity)
        with mock.patch.object(transport, "api_request", return_value={}):
            transport.finish_messages(entity, ["m_aaaaaaaa"])
        self.assertEqual(self.queued(), [])
        self.assertFalse((directory / "m_aaaaaaaa.json").exists())
        self.assertTrue((core.inbox_dir(self.mailbox, "done") / "m_aaaaaaaa.json").exists())

    def test_claim_cancels_notice_before_returning_mail(self):
        from agent_pair import client as transport
        entity = {"endpoint_id": self.mailbox, "provider": "codex", "expires_at": 9999999999}
        core.atomic_write_json(core.inbox_dir(self.mailbox, "pending") / "m_aaaaaaaa.json",
                               {"id": "m_aaaaaaaa", "text": "mail"})
        transport._wake_codex(entity)
        with mock.patch.object(transport, "ensure_monitor", return_value=0):
            messages = transport.local_messages(entity, claim=True)
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.queued(), [])


if __name__ == "__main__":
    unittest.main()
