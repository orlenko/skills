"""Native discovery and input use invented ownership records and a private core."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
from agent_nudge import daemon, herdr, native_terminal, system


def descriptor(workspace, socket_path="/invented/core.sock", core_id="invented", root=200, launch=201):
    key = hashlib.sha256(os.fsencode(workspace)).hexdigest()[:32]
    return key, dict(Ref="herdr:" + key + "-" + "0"*32, Generation=1, Workspace=str(workspace),
                     State="running", RunnerPID=root, PID=launch,
                     Core=dict(Socket=socket_path, ID=core_id, Terminal="t1", PID=root))


def save_record(path, record):
    path.write_text(json.dumps(record))
    path.chmod(0o600)


class Discovery(unittest.TestCase):
    def test_only_running_owned_local_records_are_candidates(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, AIQ_DATA_DIR=""):
            os.environ["AIQ_DATA_DIR"] = data
            directory = Path(data) / "herdr"
            directory.mkdir()
            key, record = descriptor("/invented/work")
            path = directory / (key + ".json")
            proc = mock.Mock(start=(100,), group=201)
            table = {201: (200, ["node", "/example/codex.js"]), 202: (201, ["codex"])}
            with mock.patch.object(native_terminal, "process", return_value=proc), \
                    mock.patch.object(native_terminal, "_reads_tty", return_value=True):
                save_record(path, record)
                [pane] = herdr.panes(table)
                self.assertEqual((pane.agent, pane.agent_pid, pane.extra["agent_pids"]),
                                 ("codex", 202, [201, 202]))
                for changed in (dict(State="ended"), dict(Generation=0), dict(Workspace="/elsewhere"),
                                dict(Core=dict(record["Core"], Terminal="remote/t1")), dict(RunnerPID=300)):
                    with self.subTest(changed=changed):
                        save_record(path, dict(record, **changed))
                        self.assertEqual(herdr.panes(table), [])
                save_record(path, dict(record, Generation=2))
                [successor] = herdr.panes(table)
                self.assertNotEqual(pane.id, successor.id)
                with self.assertRaises(herdr.Error):
                    pane.extra["native"].check({}, float("inf"))
                path.chmod(0o644)
                self.assertEqual(herdr.panes(table), [])
                path.unlink()
                path.symlink_to("missing")
                self.assertEqual(herdr.panes(table), [])

    def test_composer_gates(self):
        cases = [
            ("\x1b[7m/\x1b[0;2mstatus\x1b[0m", "", "", True),
            ("\x1b[2mplaceholder\x1b[0m", "blocked", "", False),
            ("draft", "", "draft", True),
            ("draft\r\n  continued", "", "draft\ncontinued", True),
            ("\x1b[7mselected\x1b[0m", "", "selected", False),
        ]
        for content, status, typed, prompt in cases:
            with self.subTest(content=content, status=status):
                raw = ("─"*40 + "\r\n❯ " + content + "\r\n" + "─"*40 +
                       "\r\n? for shortcuts\x1b[2;3H").encode()
                paint = native_terminal.render(raw, 60, 12, status)
                parsed = herdr._screen(paint, "claude")
                self.assertEqual((parsed.typed, parsed.has_prompt), (typed, prompt))

    def test_second_runner_leaves_owner_and_state_alone(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, AGENT_NUDGE_HOME=data):
            with daemon.run_lock():
                with self.assertRaises(SystemExit):
                    with daemon.run_lock():
                        self.fail("acquired busy daemon lock")
                self.assertEqual(daemon.daemon_pid(), os.getpid())

    def test_ambiguous_write_survives_restart_and_discovery_outage(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(os.environ, AGENT_NUDGE_HOME=data), \
                mock.patch.object(daemon, "agent_panes", return_value=[]), \
                mock.patch.object(system, "client_activity", return_value={}), \
                mock.patch.object(daemon.orchestra, "seats_by_pid", return_value={}):
            key = "herdr:invented@1/t1:201"
            daemon.save_state({key: {"herdr_delivery_pending": True}})
            daemon.Nudger().tick()
            self.assertTrue(daemon.Nudger().state[key]["herdr_delivery_pending"])


@unittest.skipUnless(os.environ.get("AGENT_TEST_HERDR_BIN"), "set AGENT_TEST_HERDR_BIN for private core")
class NativeIntegration(unittest.TestCase):
    def test_native_discovery_styled_capture_and_delivery(self):
        # Same isolated, parent-owned core lifecycle as Orchestra's native PTY tests.
        sys.path.insert(0, str(PLUGIN.parent / "agent-orchestra/tests"))
        from herdr_fixture_env import create_root, environment, cleanup, rpc
        root = create_root()
        env = environment(root)
        self.addCleanup(cleanup, root, env)
        endpoint = root / "ready.sock"
        provider = root / "codex.js"
        provider.write_text(r"""
const net = require('net');
const conn = net.connect(process.argv[2]);
const tty = process.stdin;
tty.setRawMode(true);
let body = 'Invented idle session', draft = '';
function draw() {
    const ghost = '\x1b[2mInvent a fixture\x1b[0m';
    const width = process.stdout.columns;
    const cursor = 3 + Math.floor((draft.length+2) / width);
    process.stdout.write('\x1b[2J\x1b[H\x1b[?2004h'+body+'\r\n\r\n› '+(draft || ghost)
        +'\r\n\r\n  ? for shortcuts'+ '\x1b['+cursor+';'+((draft.length+2)%width+1)+'H');
}
draw();
conn.on('connect', () => conn.write(process.pid+'\n'));
tty.on('data', b => {
    const chunk = b.toString();
    if (chunk === '\r') {
        conn.write(draft+'\n');
        body = '› '+draft;
        draft = '';
    } else {
        draft += chunk.replace('\x1b[200~','').replace('\x1b[201~','');
    }
    draw();
});
""")
        # AIQ puts the provider in its own foreground process group. Match that
        # topology without invoking AIQ, provider accounts or their real stores.
        runner = root / "runner.py"
        runner.write_text("""
import os, signal, sys
pid = os.fork()
if not pid:
    os.setpgid(0, 0)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    os.tcsetpgrp(0, os.getpid())
    os.execv(sys.argv[1], sys.argv[1:])
os.waitpid(pid, 0)
""")
        hc = env["AGENT_TEST_HERDR_BIN"]
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(endpoint))
            listener.listen()
            listener.settimeout(12)
            launched = subprocess.run([hc, "run", "--bg", "--no-parent", "--no-notify",
                                       "--", sys.executable, str(runner), shutil.which("node"),
                                       str(provider), str(endpoint)], env=env, check=True,
                                      capture_output=True, timeout=15)
            address = json.loads(launched.stdout)["address"]
            conn, _ = listener.accept()
            with conn, conn.makefile("rb") as events:
                conn.settimeout(8)
                launch = int(events.readline())
                terminal = rpc(env, "terminal.get", terminal=address)
                key, record = descriptor(root, env["HC_SOCKET"], rpc(env, "hello")["core"]["id"],
                                         terminal["pid"], launch)
                record["Core"]["Terminal"] = address
                directory = root / "herdr"
                directory.mkdir()
                path = directory / (key+".json")
                save_record(path, record)
                with mock.patch.dict(os.environ, AIQ_DATA_DIR=str(root)):
                    [pane] = herdr.panes(system.process_table())
                    with (mock.patch.dict(os.environ, AGENT_NUDGE_HOME=str(root/"nudge")),
                          mock.patch.object(system, "panes", return_value=[]),
                          mock.patch.object(system, "client_activity", return_value={}),
                          mock.patch.object(daemon.orchestra, "seats_by_pid", return_value={}),
                          mock.patch.object(daemon.judge, "enabled", return_value=False)):
                        clock = mock.Mock(return_value=1000)
                        nudger = daemon.Nudger(now=clock)
                        self.assertEqual(nudger.tick(), [])
                        clock.return_value += 601
                        [would_send] = nudger.tick()
                        self.assertEqual(would_send["event"], "would_nudge")
                        self.assertEqual(would_send["pane"], pane.id)
                    expected = daemon.capture_screen(pane)
                    self.assertTrue(expected.has_prompt)
                    self.assertFalse(expected.typed)
                    text = "[agent-nudge] An invented nudge."
                    armed = mock.Mock()
                    herdr.send(pane, text, expected, armed)
                    self.assertEqual(events.readline().decode().strip(), text)
                    armed.assert_called_once()
                    rpc(env, "terminal.label", terminal=address, labels={"nudge": "off"})
                    herdr.capture(pane)
                    self.assertTrue(pane.opt_out)
                    with self.assertRaises(herdr.Error):
                        herdr.send(pane, text, expected, armed)
                    save_record(path, dict(record, Generation=2))
                    with self.assertRaises(herdr.Error):
                        herdr.capture(pane)
                    armed.assert_called_once()
