from __future__ import annotations

import copy
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_orchestra import herdr_display as display


class CoreFixture:
    def __init__(self, path):
        self.path = str(path)
        self.identity = "fixture-core"
        self.terminal = {"address": "t1", "pid": 200, "created_ms": 1234,
                         "state": "live", "labels": {}}
        self.calls = []
        self.stopping = threading.Event()
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(self.path)
        self.sock.listen()
        self.sock.settimeout(.1)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    with conn.makefile("rwb") as stream:
                        while line := stream.readline():
                            request = json.loads(line)
                            method, params = request["method"], request["params"]
                            self.calls.append((method, params))
                            if method == "hello":
                                result = {"generation": 1, "core": {"id": self.identity}}
                            elif method == "terminal.get":
                                result = copy.deepcopy(self.terminal)
                            elif method == "terminal.label":
                                for key, value in params["labels"].items():
                                    if value:
                                        self.terminal["labels"][key] = value
                                    else:
                                        self.terminal["labels"].pop(key, None)
                                result = self.terminal
                            else:
                                raise AssertionError(method)
                            stream.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode() + b"\n")
                            stream.flush()
                except (OSError, ValueError):
                    pass

    def close(self):
        self.stopping.set()
        self.sock.close()
        self.thread.join(3)


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "Unix display binding")
class DisplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="display-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = CoreFixture(self.root / "core.sock")
        self.addCleanup(self.server.close)
        self.env = patch.dict(os.environ, {"ORLENKO_HERDR_DISPLAY": "1",
            "HC_TERMINAL_SOCKET": self.server.path, "HC_TERMINAL_ID": "t1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.processes = {200: (1, 10, (20,)), 300: (200, 10, (30,)),
                          301: (200, 10, (31,)), 400: (1, 10, (40,))}

        def process(pid):
            if pid not in self.processes:
                raise ValueError("gone")
            return self.processes[pid]

        self.mock_process = patch.object(display, "_process", side_effect=process)
        self.mock_process.start()
        self.addCleanup(self.mock_process.stop)
        self.path = self.root / "state" / "member.herdr-display.json"
        self.metadata = {"group": "or_123", "participant": "mb_123", "name": "Reviewer",
                         "group_name": "Development", "role": "player"}

    def bind(self, path=None, participant="mb_123", owner=300, **kwargs):
        return display.bind(path or self.path, kind="orchestra", participant=participant, owner=owner, **kwargs)

    def refresh(self, path=None, metadata=None):
        display.refresh(path or self.path, lambda: metadata or self.metadata)

    def test_default_is_inert_and_optional(self):
        with patch.dict(os.environ, {"ORLENKO_HERDR_DISPLAY": ""}):
            self.assertFalse(self.bind())
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.server.calls, [])

    def test_publish_allowlisted_fields_and_no_input_operations(self):
        self.assertTrue(self.bind())
        self.refresh(metadata={**self.metadata, "token": "secret-never-publish", "name": "A\nB\u202eX"})
        labels = self.server.terminal["labels"]
        self.assertEqual(len(labels), 1)
        key, raw = next(iter(labels.items()))
        self.assertRegex(key, r"^orlenko\.orchestra\.[0-9a-f]{16}\.[0-9a-f]{32}$")
        payload = json.loads(raw)
        self.assertEqual(payload["name"], "ABX")
        self.assertEqual(payload["created_ms"], 1234)
        self.assertAlmostEqual(payload["expires_at"] - payload["issued_at"], 120)
        self.assertNotIn("secret", raw)
        self.assertNotIn(self.server.path, raw)
        self.assertEqual({call[0] for call in self.server.calls}, {"hello", "terminal.get", "terminal.label"})

    def test_same_kind_multiple_groups_and_dual_kind_do_not_compete(self):
        paths = [self.path, self.root / "second.json", self.root / "pair.json"]
        self.assertTrue(self.bind(paths[0]))
        self.refresh(paths[0])
        self.assertTrue(self.bind(paths[1], "mb_456"))
        self.refresh(paths[1], {**self.metadata, "participant": "mb_456", "group": "or_456"})
        self.assertTrue(display.bind(paths[2], kind="pair", participant="p_123", owner=300))
        self.refresh(paths[2], {**self.metadata, "participant": "p_123", "group": "pair_123"})
        self.assertEqual(len(self.server.terminal["labels"]), 3)
        display.invalidate(paths[0], disable=True)
        self.refresh(paths[0])
        self.assertEqual(len(self.server.terminal["labels"]), 2)

    def test_retired_publication_cannot_be_renewed_or_recreated(self):
        self.bind()
        self.refresh()
        display.invalidate(self.path, disable=True)
        self.refresh()
        self.assertEqual(self.server.terminal["labels"], {})
        self.assertFalse(self.bind(current=lambda: False))
        self.assertEqual(self.server.terminal["labels"], {})

    def test_current_state_is_read_under_lock(self):
        self.bind()
        with display._lock(self.path) as acquired:
            self.assertTrue(acquired)
            with patch.object(display, "_Core", side_effect=AssertionError("no IO under contention")):
                display.refresh(self.path, lambda: self.fail("callback should not run"))
                self.assertFalse(self.bind())
        display.refresh(self.path, lambda: None)
        self.assertFalse(json.loads(self.path.read_text())["enabled"])

    def test_owner_death_rollover_and_failed_nonherdr_adoption(self):
        self.bind()
        self.refresh()
        old_key = next(iter(self.server.terminal["labels"]))
        self.assertFalse(self.bind(owner=301))  # Different live agent cannot steal it.
        del self.processes[300]
        self.assertFalse(display.owned_by(self.path, 300))
        self.assertTrue(self.bind(owner=301))
        self.refresh()
        self.assertNotIn(old_key, self.server.terminal["labels"])
        self.assertTrue(display.owned_by(self.path, 301))
        with patch.dict(os.environ, {"HC_TERMINAL_SOCKET": "", "HC_TERMINAL_ID": ""}):
            self.assertFalse(self.bind(owner=301))
        self.refresh()
        self.assertEqual(self.server.terminal["labels"], {})

    def test_terminal_and_process_incarnation_fail_closed(self):
        for scenario in ("core", "created", "root", "root-start", "owner-start", "tty", "tree", "state", "address", "dead-owner"):
            with self.subTest(scenario=scenario):
                self.server.identity = "fixture-core"
                self.server.terminal.update(address="t1", pid=200, created_ms=1234, state="live", labels={})
                self.processes = {200: (1, 10, (20,)), 300: (200, 10, (30,))}
                self.path.unlink(missing_ok=True)
                self.assertTrue(self.bind())
                if scenario == "core": self.server.identity = "other-core"
                elif scenario == "created": self.server.terminal["created_ms"] = 1235
                elif scenario == "root": self.server.terminal["pid"] = 201
                elif scenario == "root-start": self.processes[200] = (1, 10, (21,))
                elif scenario == "owner-start": self.processes[300] = (200, 10, (31,))
                elif scenario == "tty": self.processes[300] = (200, 11, (30,))
                elif scenario == "tree": self.processes[300] = (1, 10, (30,))
                elif scenario == "state": self.server.terminal["state"] = "finished"
                elif scenario == "address": self.server.terminal["address"] = "t2"
                elif scenario == "dead-owner": del self.processes[300]
                self.refresh()
                self.assertEqual(self.server.terminal["labels"], {})

    def test_owner_metadata_fences_stale_adoption_callback(self):
        self.bind()
        self.refresh(metadata={**self.metadata, "_owner_pid": 301})
        self.assertEqual(self.server.terminal["labels"], {})

    def test_authoritative_adoption_can_replace_a_live_previous_owner(self):
        self.bind()
        self.refresh()
        self.assertFalse(self.bind(owner=301, replace=True))
        self.assertFalse(self.bind(owner=301, replace=True, current=lambda: False))
        self.assertTrue(self.bind(owner=301, replace=True, current=lambda: True))
        self.refresh()
        self.assertEqual(json.loads(next(iter(self.server.terminal["labels"].values())))["owner_pid"], 301)

    def test_explicit_disable_survives_monitor_restart(self):
        self.bind()
        self.refresh()
        self.assertTrue(self.bind(enabled=False))
        for flag in ("", "1"):
            with patch.dict(os.environ, {"ORLENKO_HERDR_DISPLAY": flag}):
                self.assertFalse(self.bind())
        self.refresh()
        self.assertEqual(self.server.terminal["labels"], {})

    def test_unsupported_state_and_nonlocal_targets_are_inert(self):
        for address in ("box/t1", "reviewer", "../t1", "t0"):
            with self.subTest(address=address), patch.dict(os.environ, {"HC_TERMINAL_ID": address}):
                self.assertFalse(self.bind())
        self.path.write_text('{"v":99,"enabled":true}')
        self.refresh()
        self.assertFalse(self.bind())
        self.assertEqual(self.server.calls, [])


class PackagingTest(unittest.TestCase):
    def test_publishers_identical_and_standalone(self):
        first = ROOT / "agent_orchestra" / "herdr_display.py"
        second = ROOT.parent / "agent-pair" / "agent_pair" / "herdr_display.py"
        self.assertEqual(first.read_bytes(), second.read_bytes())
        # Load each publisher under an unrelated name with no plugin/package context.
        for path in (first, second):
            spec = importlib.util.spec_from_file_location("standalone_display", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.LEASE, 120)


if __name__ == "__main__":
    unittest.main()
