"""Opt-in integration regression against a real Codex and a local model fixture.

AGENT_TEST_CODEX_BIN=/path/to/codex python3 -m unittest discover \
    -s plugins/agent-pair/tests -p test_codex_native_wake.py -v
No account, real model request, or existing Codex thread is used.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


@unittest.skipUnless(os.environ.get("AGENT_TEST_CODEX_BIN"), "set AGENT_TEST_CODEX_BIN for native test")
class NativeWakeTest(unittest.TestCase):
    def test_busy_mail_handled_before_idle_never_causes_an_empty_wake(self):
        release = threading.Event()
        release.set()
        model_started = threading.Event()

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", "0")))
                model_started.set()
                if not release.wait(40):
                    return
                message = {"id": "msg_probe", "type": "message", "role": "assistant",
                           "status": "completed", "content": [{"type": "output_text",
                           "text": "Fixture complete.", "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {"id": "resp_probe"}},
                    {"type": "response.output_item.done", "output_index": 0, "item": message},
                    {"type": "response.completed", "response": {"id": "resp_probe",
                     "status": "completed", "output": [message],
                     "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                data = "".join("data: " + json.dumps(item) + "\n\n" for item in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        with tempfile.TemporaryDirectory(prefix="codex-mail-native-") as temporary:
            root = Path(temporary)
            home = root / "codex"
            home.mkdir()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            (home / "config.toml").write_text(
                'model = "wake-test"\nmodel_provider = "mock"\napproval_policy = "never"\n'
                'sandbox_mode = "read-only"\n[features]\nhooks = false\n'
                '[model_providers.mock]\nname = "Local fixture"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n'
            )
            binary = str(Path(os.environ["AGENT_TEST_CODEX_BIN"]).expanduser().resolve())
            env = os.environ.copy()
            env.update(CODEX_HOME=str(home), AIQ_BYPASS="1")
            with (root / "server.log").open("wb") as log:
                process = subprocess.Popen([binary, "app-server", "--stdio"], env=env,
                    cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
                events = queue.Queue()

                def read():
                    for line in process.stdout:
                        events.put(json.loads(line))

                reader = threading.Thread(target=read, daemon=True)
                reader.start()

                def send(method, params=None, ident=None):
                    item = {"method": method}
                    if params is not None:
                        item["params"] = params
                    if ident is not None:
                        item["id"] = ident
                    process.stdin.write(json.dumps(item) + "\n")
                    process.stdin.flush()

                def until(predicate):
                    deadline = time.monotonic() + 35
                    while True:
                        item = events.get(timeout=max(0.01, deadline - time.monotonic()))
                        if predicate(item):
                            return item

                try:
                    send("initialize", {"clientInfo": {"name": "wake_regression", "version": "1"},
                         "capabilities": {"experimentalApi": True}}, 1)
                    self.assertNotIn("error", until(lambda item: item.get("id") == 1))
                    send("initialized")
                    plugins = Path(__file__).resolve().parents[2]
                    for plugin, package, module, prefix, ident, bucket, mailbox in [
                        ("agent-pair", "agent_pair", "client", "AGENT_PAIR", "endpoint", "inbox_dir", "pair_test-peer_test"),
                        ("agent-orchestra", "agent_orchestra", "member", "AGENT_ORCHESTRA", "member", "bucket_dir", "mb_aaaa1111"),
                    ]:
                        with self.subTest(plugin=plugin):
                            sys.path.insert(0, str(plugins / plugin))
                            adapter = importlib.import_module(package + ".codex_wake")
                            transport = importlib.import_module(package + "." + module)
                            core = importlib.import_module(package + ".core")
                            send("thread/start", {"cwd": str(root), "model": "wake-test",
                                 "modelProvider": "mock", "approvalPolicy": "never", "sandbox": "read-only"}, 2)
                            result = until(lambda item: item.get("id") == 2)
                            self.assertNotIn("error", result)
                            thread = result["result"]["thread"]["id"]
                            def turn():
                                send("turn/start", {"threadId": thread, "input": [
                                    {"type": "text", "text": "Fixture turn", "text_elements": []}]}, 3)
                            turn()
                            until(lambda item: item.get("method") == "turn/completed")
                            with mock.patch.dict(os.environ, {
                                prefix + "_HOME": str(root / plugin), prefix + "_CODEX_BIN": binary,
                                "AGENT_CODEX_WAKE_HOME": str(root / "wake-state"),
                                "CODEX_HOME": str(home), "CODEX_THREAD_ID": thread,
                            }):
                                adapter.register(mailbox, prefix=prefix)
                                entity = {ident + "_id": mailbox, "provider": "codex", "expires_at": 9999999999}
                                pending = getattr(core, bucket)(mailbox, "pending")
                                message_id = "m_aaaaaaaa"
                                core.atomic_write_json(pending / (message_id + ".json"), {"id": message_id, "text": "mail"})
                                model_started.clear()
                                release.clear()
                                turn()
                                self.assertTrue(model_started.wait(5))
                                transport._wake_codex(entity)
                                self.assertIsNone(adapter.capability(mailbox)["last_error"])
                                with adapter._QueueClient(adapter.target(mailbox)) as client:
                                    self.assertEqual(len(client.notices(None, None)), 1)
                                with mock.patch.object(transport, "api_request", return_value={}):
                                    transport.finish_messages(entity, [message_id])
                                with adapter._QueueClient(adapter.target(mailbox)) as client:
                                    self.assertEqual(client.notices(None, None), [])
                                release.set()
                                until(lambda item: item.get("method") == "turn/completed")
                                deadline = time.monotonic() + 11  # covers native queue's 10s external poll
                                while time.monotonic() < deadline:
                                    try:
                                        item = events.get(timeout=max(.01, deadline - time.monotonic()))
                                    except queue.Empty:
                                        break
                                    self.assertNotEqual(item.get("method"), "turn/started", "empty wake after finish")
                                core.atomic_write_json(pending / "m_bbbbbbbb.json", {"id": "m_bbbbbbbb", "text": "new mail"})
                                with mock.patch.object(adapter.time, "time", return_value=time.time() + 61):
                                    transport._wake_codex(entity)
                                until(lambda item: item.get("method") == "turn/started")
                                complete = until(lambda item: item.get("method") == "turn/completed")
                                self.assertEqual(complete["params"]["turn"]["status"], "completed")
                finally:
                    release.set()
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    reader.join(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)
