import os
import shutil
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_pair.client import (  # noqa: E402
    _reap_spawned_processes,
    accept_pair,
    close_pair,
    create_pair,
    finish_messages,
    hook_stop,
    hook_wait,
    load_endpoint,
    pair_status,
    send_message,
    wait_for_messages,
)
from agent_pair.core import AgentPairError, decode_invite, encode_invite  # noqa: E402


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-pair-test-")
        self.previous_home = os.environ.get("AGENT_PAIR_HOME")
        os.environ["AGENT_PAIR_HOME"] = self.state
        self.server_pid = None

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
