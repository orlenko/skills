import os
import shutil
import signal
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_pair import client  # noqa: E402
from agent_pair.client import (  # noqa: E402
    _reap_spawned_processes,
    _spawn_module,
    accept_pair,
    close_pair,
    create_pair,
    finish_messages,
    flush_handled,
    hook_context,
    hook_stop,
    hook_wait,
    load_endpoint,
    pair_status,
    pending_count,
    send_message,
    unsynced_handled,
    wait_for_messages,
)
from agent_pair.core import (  # noqa: E402
    AgentPairError,
    decode_invite,
    encode_invite,
    inbox_dir,
    read_json,
    runtime_dir,
)


def _free_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-pair-test-")
        self.previous_home = os.environ.get("AGENT_PAIR_HOME")
        os.environ["AGENT_PAIR_HOME"] = self.state
        self.server_pid = None

    def tearDown(self):
        _reap_spawned_processes(
            [process.pid for process in client._BACKGROUND_PROCESSES], timeout=0
        )
        if self.server_pid:
            try:
                os.kill(self.server_pid, signal.SIGTERM)
            except OSError:
                pass
        if self.previous_home is None:
            os.environ.pop("AGENT_PAIR_HOME", None)
        else:
            os.environ["AGENT_PAIR_HOME"] = self.previous_home
        shutil.rmtree(self.state, ignore_errors=True)

    def test_two_peers_deliver_and_handle_messages(self):
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        tampered = decode_invite(host["invite"])
        tampered["fingerprint"] = "00" * 32
        tampered.pop("v")
        with self.assertRaisesRegex(AgentPairError, "fingerprint"):
            accept_pair(
                encode_invite(tampered),
                provider="test",
                cwd="/tmp/agent-pair-attacker",
                name="wrong endpoint",
            )
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        with self.assertRaises(AgentPairError):
            accept_pair(
                host["invite"],
                provider="test",
                cwd="/tmp/agent-pair-third",
                name="third peer",
            )
        host_endpoint = load_endpoint(host["endpoint_id"])
        guest_endpoint = load_endpoint(guest["endpoint_id"])

        outgoing = send_message(host_endpoint, "Please review parser.py")
        self.assertEqual(outgoing["state"], "queued")

        incoming = wait_for_messages(guest_endpoint, 8, claim=True)
        self.assertEqual([item["text"] for item in incoming], ["Please review parser.py"])
        stop_output = hook_stop(
            "test",
            {
                "cwd": "/tmp/agent-pair-guest",
                "session_id": "guest-test-session",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
        )
        self.assertEqual(stop_output["decision"], "block")
        self.assertNotIn("Please review parser.py", stop_output["reason"])
        message_id = incoming[0]["id"]
        finish_messages(guest_endpoint, [message_id])

        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            state = pair_status(host_endpoint)
            rows = state["remote"]["messages"]
            if rows and rows[0]["state"] == "handled":
                break
            time.sleep(0.1)
        self.assertEqual(state["remote"]["messages"][0]["state"], "handled")

        wake_codes = []
        wake_payload = {
            "cwd": "/tmp/agent-pair-guest",
            "session_id": "guest-test-session",
            "hook_event_name": "Stop",
        }
        watcher = threading.Thread(
            target=lambda: wake_codes.append(hook_wait("test", wake_payload)),
            daemon=True,
        )
        watcher.start()
        time.sleep(0.1)
        self.assertEqual(hook_wait("test", wake_payload), 0)
        wake_message = send_message(host_endpoint, "Wake up for the next handoff")
        self.assertEqual(wake_message["state"], "queued")
        watcher.join(timeout=8)
        self.assertEqual(wake_codes, [2])
        woke_inbox = wait_for_messages(guest_endpoint, 2, claim=True)
        self.assertEqual(woke_inbox[0]["text"], "Wake up for the next handoff")
        finish_messages(guest_endpoint, [woke_inbox[0]["id"]])

        reply = send_message(guest_endpoint, "Reviewed; no blockers")
        self.assertEqual(reply["state"], "queued")
        host_inbox = wait_for_messages(host_endpoint, 8, claim=True)
        self.assertEqual(host_inbox[0]["text"], "Reviewed; no blockers")

        close_pair(guest_endpoint)
        _reap_spawned_processes(
            [host["server_pid"], host["monitor_pid"], guest["monitor_pid"]]
        )
        self.server_pid = None

    def test_hooks_ignore_sessions_beyond_the_first_bound_one(self):
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        host_endpoint = load_endpoint(host["endpoint_id"])
        guest_endpoint = load_endpoint(guest["endpoint_id"])

        owner_payload = {
            "cwd": "/tmp/agent-pair-guest",
            "session_id": "owner-session",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        self.assertEqual(hook_stop("test", owner_payload), {})
        owner_bindings = sorted(runtime_dir().glob("binding-*.json"))
        self.assertEqual(len(owner_bindings), 1)

        child_payload = {
            "cwd": "/tmp/agent-pair-guest",
            "session_id": "print-mode-child",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        child_codes = []
        child_watcher = threading.Thread(
            target=lambda: child_codes.append(hook_wait("test", child_payload)),
            daemon=True,
        )
        child_watcher.start()
        child_watcher.join(timeout=5)
        self.assertFalse(child_watcher.is_alive(), "hook_wait parked a non-owner session")
        self.assertEqual(child_codes, [0])

        send_message(host_endpoint, "Mail addressed to the owner session")
        self.assertTrue(wait_for_messages(guest_endpoint, 8, claim=False))
        self.assertEqual(hook_stop("test", child_payload), {})
        self.assertEqual(hook_context("test", child_payload), {})
        self.assertEqual(hook_stop("test", owner_payload)["decision"], "block")
        self.assertEqual(sorted(runtime_dir().glob("binding-*.json")), owner_bindings)

        close_pair(guest_endpoint)
        _reap_spawned_processes(
            [host["server_pid"], host["monitor_pid"], guest["monitor_pid"]]
        )
        self.server_pid = None

    def test_no_wait_environment_keeps_hooks_inert(self):
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        host_endpoint = load_endpoint(host["endpoint_id"])
        guest_endpoint = load_endpoint(guest["endpoint_id"])

        os.environ["AGENT_PAIR_NO_WAIT"] = "1"
        self.addCleanup(os.environ.pop, "AGENT_PAIR_NO_WAIT", None)
        payload = {
            "cwd": "/tmp/agent-pair-guest",
            "session_id": "harness-child",
            "hook_event_name": "Stop",
            "stop_hook_active": False,
        }
        watcher = threading.Thread(
            target=lambda: hook_wait("test", payload), daemon=True
        )
        watcher.start()
        watcher.join(timeout=5)
        self.assertFalse(watcher.is_alive(), "hook_wait parked despite AGENT_PAIR_NO_WAIT")

        send_message(host_endpoint, "Mail that must not surface through hooks")
        self.assertTrue(wait_for_messages(guest_endpoint, 8, claim=False))
        self.assertEqual(hook_stop("test", payload), {})
        self.assertEqual(hook_context("test", payload), {})
        self.assertEqual(list(runtime_dir().glob("binding-*.json")), [])

        os.environ.pop("AGENT_PAIR_NO_WAIT", None)
        self.assertEqual(hook_stop("test", payload)["decision"], "block")

        close_pair(guest_endpoint)
        _reap_spawned_processes(
            [host["server_pid"], host["monitor_pid"], guest["monitor_pid"]]
        )
        self.server_pid = None

    def test_finish_and_close_survive_a_dead_peer(self):
        port = _free_port()
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            port=port,
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        host_endpoint = load_endpoint(host["endpoint_id"])
        guest_endpoint = load_endpoint(guest["endpoint_id"])

        send_message(host_endpoint, "Handle this before I vanish")
        incoming = wait_for_messages(guest_endpoint, 8, claim=True)
        message_id = incoming[0]["id"]

        _reap_spawned_processes([host["server_pid"], host["monitor_pid"]], timeout=0)
        self.server_pid = None

        results = finish_messages(guest_endpoint, [message_id])
        self.assertEqual(results[0]["state"], "handled-locally")
        self.assertEqual(results[0]["sync"], "unsynced")
        self.assertIn("could not notify peer", results[0]["detail"])

        guest_id = guest["endpoint_id"]
        self.assertFalse((inbox_dir(guest_id, "claimed") / f"{message_id}.json").exists())
        self.assertTrue((inbox_dir(guest_id, "done") / f"{message_id}.json").exists())
        self.assertEqual(pending_count(guest_endpoint), 0)
        self.assertEqual(
            hook_stop(
                "test",
                {
                    "cwd": "/tmp/agent-pair-guest",
                    "session_id": "guest-dead-peer-session",
                    "hook_event_name": "Stop",
                    "stop_hook_active": False,
                },
            ),
            {},
        )

        repeated = finish_messages(guest_endpoint, [message_id])
        self.assertIn("already handled locally", repeated[0]["detail"])
        with self.assertRaisesRegex(AgentPairError, "not found"):
            finish_messages(guest_endpoint, ["m_neverseenbefore"])

        state = pair_status(guest_endpoint)
        self.assertEqual(state["peer"]["state"], "unreachable")
        self.assertEqual(state["local"]["unsynced_handled"], 1)

        closed = close_pair(guest_endpoint)
        self.assertEqual(closed["state"], "closed-locally")
        self.assertIn("could not notify peer", closed["detail"])
        self.assertTrue(load_endpoint(guest_id)["closed_at"])
        _reap_spawned_processes([guest["monitor_pid"]])

    def test_host_status_reports_a_silent_peer_as_stale(self):
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        host_endpoint = load_endpoint(host["endpoint_id"])

        fresh = pair_status(host_endpoint)
        self.assertEqual(fresh["peer"]["state"], "connected")
        self.assertEqual(fresh["peer"]["name"], "macOS Claude")

        _reap_spawned_processes([guest["monitor_pid"]], timeout=0)
        previous_stale = client.PEER_STALE_SECONDS
        client.PEER_STALE_SECONDS = 0.5
        self.addCleanup(setattr, client, "PEER_STALE_SECONDS", previous_stale)
        time.sleep(0.7)

        silent = pair_status(host_endpoint)
        self.assertIsNotNone(silent["remote"])
        self.assertIsNone(silent["remote_error"])
        self.assertEqual(silent["peer"]["state"], "stale")
        self.assertGreater(silent["peer"]["last_seen_age"], 0.5)

        close_pair(host_endpoint)
        _reap_spawned_processes([host["server_pid"], host["monitor_pid"]])
        self.server_pid = None

    def test_handled_notice_resyncs_after_the_peer_returns(self):
        previous_gate = client._HANDLED_RETRY_SECONDS
        client._HANDLED_RETRY_SECONDS = 0
        self.addCleanup(setattr, client, "_HANDLED_RETRY_SECONDS", previous_gate)

        port = _free_port()
        host = create_pair(
            provider="test",
            cwd="/tmp/agent-pair-host",
            name="Ubuntu Codex",
            advertise=["127.0.0.1"],
            port=port,
            ttl_seconds=300,
        )
        self.server_pid = host["server_pid"]
        guest = accept_pair(
            host["invite"],
            provider="test",
            cwd="/tmp/agent-pair-guest",
            name="macOS Claude",
        )
        host_endpoint = load_endpoint(host["endpoint_id"])
        guest_endpoint = load_endpoint(guest["endpoint_id"])

        send_message(host_endpoint, "Survive a restart")
        message_id = wait_for_messages(guest_endpoint, 8, claim=True)[0]["id"]

        pair_id = host["pair_id"]
        _reap_spawned_processes([host["server_pid"], host["monitor_pid"]], timeout=0)
        self.server_pid = None

        finish_messages(guest_endpoint, [message_id])
        self.assertEqual(len(unsynced_handled(guest["endpoint_id"])), 1)

        restarted = _spawn_module(
            ["serve", "--pair-id", pair_id], runtime_dir() / f"{pair_id}.restart.log"
        )
        self.server_pid = restarted

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            flush_handled(guest_endpoint)
            if not unsynced_handled(guest["endpoint_id"]):
                break
            time.sleep(0.2)
        self.assertEqual(unsynced_handled(guest["endpoint_id"]), [])

        done = read_json(inbox_dir(guest["endpoint_id"], "done") / f"{message_id}.json")
        self.assertEqual(done["sync_state"], "synced")

        state = pair_status(host_endpoint)
        self.assertEqual(state["remote"]["messages"][0]["state"], "handled")

        close_pair(guest_endpoint)
        _reap_spawned_processes([restarted, guest["monitor_pid"]])
        self.server_pid = None


if __name__ == "__main__":
    unittest.main()
