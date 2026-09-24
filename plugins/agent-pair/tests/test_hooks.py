import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from agent_pair import client, core  # noqa: E402
from agent_pair.core import (  # noqa: E402
    atomic_write_json,
    endpoint_path,
    inbox_dir,
    instance_key,
    read_json,
    runtime_dir,
)


DAY = 24 * 60 * 60


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class HookStateTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-pair-hooks-")
        self.addCleanup(shutil.rmtree, self.state, True)
        environment = patch.dict(os.environ, {"AGENT_PAIR_HOME": self.state})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AGENT_PAIR_NO_WAIT", None)
        self.cwd = str(Path(self.state) / "work")
        os.mkdir(self.cwd)

    def endpoint(self, name, *, expires_in=3600.0, closed_at=None):
        endpoint_id = f"pair_{name}-p_{name}"
        record = {
            "endpoint_id": endpoint_id,
            "pair_id": f"pair_{name}",
            "provider": "test",
            "role": "guest",
            "instance_key": instance_key("test", self.cwd),
            "endpoints": ["https://127.0.0.1:9"],
            "fingerprint": "ab" * 32,
            "token": "token",
            "created_at": time.time(),
            "expires_at": time.time() + expires_in,
            "closed_at": closed_at,
        }
        atomic_write_json(endpoint_path(endpoint_id), record)
        return record

    def mail(self, endpoint_id, message_id, bucket="pending"):
        path = inbox_dir(endpoint_id, bucket) / f"{message_id}.json"
        atomic_write_json(path, {"id": message_id, "from": {"name": "Peer"},
                                 "text": f"body of {message_id}", "sent_at": time.time()})
        return path

    def payload(self, session_id):
        return {"cwd": self.cwd, "session_id": session_id,
                "hook_event_name": "Stop", "stop_hook_active": False}

    def bind(self, session_id, endpoint, *, owner, age=0.0):
        path = client._binding_path("test", self.cwd, session_id)
        client._write_binding(path, endpoint, "test", self.cwd, session_id, owner)
        record = read_json(path)
        record["bound_at"] = time.time() - age
        atomic_write_json(path, record)
        return path


class StaleBindingTests(HookStateTests):
    def test_a_dead_sessions_binding_frees_its_endpoint(self):
        endpoint = self.endpoint("a1")
        stale = self.bind("gone-session", endpoint, owner=_dead_pid())
        with patch.object(client, "ensure_monitor", return_value=0), \
             patch.object(client, "agent_ancestor_pid", return_value=os.getpid()):
            bound = client.hook_endpoint("test", self.payload("new-session"))
        self.assertEqual(bound["endpoint_id"], endpoint["endpoint_id"])
        self.assertFalse(stale.exists())
        fresh = read_json(client._binding_path("test", self.cwd, "new-session"))
        self.assertEqual(fresh["owner_pid"], os.getpid())

    def test_a_pidless_binding_holds_only_while_young(self):
        endpoint = self.endpoint("a2")
        self.bind("old-version", endpoint, owner=None)
        with patch.object(client, "ensure_monitor", return_value=0):
            self.assertIsNone(client.hook_endpoint("test", self.payload("second")))
            self.bind("old-version", endpoint, owner=None, age=client._BINDING_STALE_SECONDS + 5)
            bound = client.hook_endpoint("test", self.payload("second"))
        self.assertEqual(bound["endpoint_id"], endpoint["endpoint_id"])

    def test_a_live_sessions_binding_keeps_other_sessions_inert(self):
        endpoint = self.endpoint("a3")
        held = self.bind("owner", endpoint, owner=os.getpid(), age=DAY)
        with patch.object(client, "ensure_monitor", return_value=0):
            self.assertIsNone(client.hook_endpoint("test", self.payload("print-child")))
        self.assertTrue(held.exists())

    def test_the_holding_session_keeps_its_pidless_binding_young(self):
        endpoint = self.endpoint("a4")
        path = self.bind("owner", endpoint, owner=None, age=120)
        with patch.object(client, "ensure_monitor", return_value=0), \
             patch.object(client, "agent_ancestor_pid", return_value=None):
            client.hook_endpoint("test", self.payload("owner"))
        self.assertLess(time.time() - read_json(path)["bound_at"], 5)

    def test_no_endpoint_in_the_directory_means_no_process_walk(self):
        with patch.object(client, "agent_ancestor_pid") as walk:
            self.assertIsNone(client.hook_endpoint("test", self.payload("any")))
        walk.assert_not_called()

    def test_ancestor_walk_skips_shells_that_quote_the_agent(self):
        tree = {
            ("ppid=", 100): "90",
            ("args=", 90): "/bin/zsh -c /Users/me/.claude/plugins/agent-pair/bin/agent-pair",
            ("ppid=", 90): "80",
            ("args=", 80): "node /usr/local/bin/claude --resume",
        }
        with patch.object(core, "_ps_field", side_effect=lambda f, p: tree.get((f, p))):
            self.assertEqual(core.agent_ancestor_pid(100), 80)


class HookWaitTests(HookStateTests):
    def test_wait_parks_past_shown_mail_and_wakes_on_new_mail(self):
        endpoint = self.endpoint("w1")
        endpoint_id = endpoint["endpoint_id"]
        self.mail(endpoint_id, "m_shown")
        codes = []
        errors = io.StringIO()

        def close():
            record = read_json(endpoint_path(endpoint_id))
            record["closed_at"] = time.time()
            atomic_write_json(endpoint_path(endpoint_id), record)

        with patch.object(client, "ensure_monitor", return_value=0), redirect_stderr(errors):
            watcher = threading.Thread(
                target=lambda: codes.append(client.hook_wait("test", self.payload("s"))),
                daemon=True,
            )
            watcher.start()
            try:
                time.sleep(0.8)
                self.assertTrue(watcher.is_alive(), "hook_wait exited on mail already shown")
                self.mail(endpoint_id, "m_new")
                watcher.join(timeout=5)
            finally:
                if watcher.is_alive():
                    close()
                    watcher.join(timeout=5)
        self.assertEqual(codes, [2])
        self.assertIn("claim_token: m_new", errors.getvalue())


class LocalMessagesTests(HookStateTests):
    def test_an_unreadable_row_does_not_hide_the_others(self):
        endpoint = self.endpoint("l1")
        endpoint_id = endpoint["endpoint_id"]
        (inbox_dir(endpoint_id, "pending") / "m_bad.json").write_text("{not json")
        self.mail(endpoint_id, "m_good")
        with patch.object(client, "ensure_monitor", return_value=0):
            result = client.hook_stop("test", self.payload("s"))
        self.assertEqual(result["decision"], "block")
        self.assertIn("claim_token: m_good", result["reason"])
        self.assertNotIn("m_bad", result["reason"])


class MonitorGuardTests(HookStateTests):
    def test_no_monitor_for_a_closed_or_expired_endpoint(self):
        closed = self.endpoint("g1")
        stale_copy = dict(closed)
        closed["closed_at"] = time.time()
        atomic_write_json(endpoint_path(closed["endpoint_id"]), closed)
        expired = self.endpoint("g2", expires_in=-1)
        live = self.endpoint("g3")
        with patch.object(client, "_spawn_module", return_value=4242) as spawn:
            self.assertEqual(client.ensure_monitor(stale_copy), 0)
            self.assertEqual(client.ensure_monitor(expired), 0)
            self.assertEqual(client.start_monitor(expired), 0)
            spawn.assert_not_called()
            self.assertEqual(client.ensure_monitor(live), 4242)
            spawn.assert_called_once()


class PruneTests(HookStateTests):
    def runtime_files(self, endpoint_id, *, monitor_pid, heartbeat):
        runtime = runtime_dir()
        atomic_write_json(runtime / f"{endpoint_id}.monitor.json",
                          {"pid": monitor_pid, "updated_at": heartbeat})
        (runtime / f"{endpoint_id}.monitor.log").write_text("log\n")
        atomic_write_json(runtime / f"{endpoint_id}.wake.0123456789abcdef0123.json", {"pid": 0})

    def test_prune_removes_long_retired_idle_endpoints_only(self):
        dead = _dead_pid()
        idle = self.endpoint("p1", expires_in=-8 * DAY)
        self.runtime_files(idle["endpoint_id"], monitor_pid=dead, heartbeat=time.time() - 8 * DAY)
        self.mail(idle["endpoint_id"], "m_done", bucket="done")
        binding = self.bind("gone", idle, owner=os.getpid())
        waiting = self.endpoint("p2", expires_in=-8 * DAY)
        self.mail(waiting["endpoint_id"], "m_unread")
        claimed = self.endpoint("p3", closed_at=time.time() - 8 * DAY)
        self.mail(claimed["endpoint_id"], "m_claimed", bucket="claimed")
        running = self.endpoint("p4", closed_at=time.time() - 8 * DAY)
        self.runtime_files(running["endpoint_id"], monitor_pid=os.getpid(), heartbeat=time.time())
        recent = self.endpoint("p5", expires_in=-2 * DAY)
        active = self.endpoint("p6")

        rows = client.iter_endpoints(provider="test", cwd=self.cwd)

        self.assertEqual([row["endpoint_id"] for row in rows], [active["endpoint_id"]])
        self.assertFalse(endpoint_path(idle["endpoint_id"]).exists())
        self.assertEqual(list(runtime_dir().glob(f"{idle['endpoint_id']}.*")), [])
        self.assertFalse(binding.exists())
        self.assertTrue((inbox_dir(idle["endpoint_id"], "done") / "m_done.json").exists())
        for kept in (waiting, claimed, running, recent, active):
            self.assertTrue(endpoint_path(kept["endpoint_id"]).exists(), kept["endpoint_id"])
        self.assertTrue((runtime_dir() / f"{running['endpoint_id']}.monitor.log").exists())

    def test_prune_runs_at_most_once_an_hour(self):
        self.endpoint("q1", expires_in=-8 * DAY)
        client.iter_endpoints(provider="test", cwd=self.cwd)
        later = self.endpoint("q2", expires_in=-8 * DAY)
        client.iter_endpoints(provider="test", cwd=self.cwd)
        self.assertTrue(endpoint_path(later["endpoint_id"]).exists())
        stamp = runtime_dir() / "prune.stamp"
        past = time.time() - client._PRUNE_INTERVAL_SECONDS - 60
        os.utime(stamp, (past, past))
        client.iter_endpoints(provider="test", cwd=self.cwd)
        self.assertFalse(endpoint_path(later["endpoint_id"]).exists())


if __name__ == "__main__":
    unittest.main()
