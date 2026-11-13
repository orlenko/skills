"""Local-only actual CLI skill dispatch probe; no real provider, account or user pane."""

import json
import os
import pathlib
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

provider = sys.argv[1]
herdr_binary = os.environ.get("AGENT_TEST_HERDR_BIN", "hc")
root = pathlib.Path(tempfile.mkdtemp(prefix="aocli-", dir="/tmp")).resolve()
print("fixture", root, flush=True)
requests = queue.Queue()
marker = "ORCHESTRA_INVENTED_SKILL_BODY_8E84"


class Model(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        data = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        requests.put((self.path, payload))
        with (root / "requests.jsonl").open("ab") as log:
            log.write(payload + b"\n")
        if self.path.endswith("/count_tokens"):
            data = b'{"input_tokens":10}'
            ctype = "application/json"
        elif "/messages" in self.path:
            message = {
                "id": "msg_invented",
                "type": "message",
                "role": "assistant",
                "model": "claude-fixture",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            }
            events = [
                ("message_start", {"type": "message_start", "message": message}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "text_delta",
                            "text": "INVENTED SKILL FINISHED",
                        },
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 4},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
            data = "".join(
                "event: " + t + "\ndata: " + json.dumps(d) + "\n\n" for t, d in events
            ).encode()
            ctype = "text/event-stream"
        else:
            item = {
                "id": "msg_invented",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "INVENTED SKILL FINISHED",
                        "annotations": [],
                    }
                ],
            }
            events = [
                {"type": "response.created", "response": {"id": "resp_invented"}},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_invented",
                        "status": "completed",
                        "output": [item],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 4,
                            "total_tokens": 14,
                        },
                    },
                },
            ]
            data = "".join("data: " + json.dumps(d) + "\n\n" for d in events).encode()
            ctype = "text/event-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
env = {
    k: v
    for k, v in os.environ.items()
    if not k.startswith(
        ("HC_", "XDG_", "CODEX_", "CLAUDE_", "ANTHROPIC_", "OPENAI_", "AIQ_")
    )
}
env.update(
    HC_SOCKET=str(root / "core.sock"),
    HC_SESSION="orchestra-cli-fixture",
    XDG_STATE_HOME=str(root / "state"),
    XDG_CONFIG_HOME=str(root / "config"),
    XDG_CACHE_HOME=str(root / "cache"),
    XDG_DATA_HOME=str(root / "data"),
    AIQ_BYPASS="1",
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    DISABLE_AUTOUPDATER="1",
)
workspace = root / "workspace"
workspace.mkdir()
subprocess.run(["git", "init", "-q", str(workspace)], check=True)
skilltext = (
    "---\nname: orchestra-inert\ndescription: Inert local Orchestra transport fixture.\n---\n\n"
    + marker
    + "\nReply only INVENTED SKILL FINISHED. Do not run tools.\n"
)
if provider == "codex":
    fixture_home = root / "codex"
    fixture_home.mkdir()
    env["CODEX_HOME"] = str(fixture_home)
    (fixture_home / "config.toml").write_text(
        'model = "orchestra-fixture"\nmodel_provider = "mock"\napproval_policy = "never"\nsandbox_mode = "read-only"\n[features]\nhooks = false\n[model_providers.mock]\nname = "Local fixture"\nbase_url = "http://127.0.0.1:'
        + str(server.server_port)
        + '/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n[projects."'
        + str(workspace)
        + '"]\ntrust_level = "trusted"\n'
    )
    skill = workspace / ".agents/skills/orchestra-inert"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skilltext)
    argv = [
        os.environ["AGENT_TEST_CODEX_BIN"],
        "--no-daemon",
        "--no-alt-screen",
        "-C",
        str(workspace),
    ]
    command = "$orchestra-inert"
else:
    fixture_home = root / "claude"
    fixture_home.mkdir()
    (fixture_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "customApiKeyResponses": {
                    "approved": ["sk-ant-invented-local-only"[-20:]],
                    "rejected": [],
                },
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    env.update(
        CLAUDE_CONFIG_DIR=str(fixture_home),
        ANTHROPIC_API_KEY="sk-ant-invented-local-only",
        ANTHROPIC_BASE_URL="http://127.0.0.1:" + str(server.server_port),
    )
    # Explicit plugin keeps --bare's skill lookup completely in invented fixture source.
    plugin = root / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        '{"name":"fixture","version":"1.0.0"}'
    )
    skill = plugin / "skills/orchestra-inert"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(skilltext)
    argv = [
        os.environ["AGENT_TEST_CLAUDE_BIN"],
        "--bare",
        "--model",
        "claude-fixture",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "",
        "--plugin-dir",
        str(plugin),
    ]
    command = "/fixture:orchestra-inert"


def rpc(method, params):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(15)
        s.connect(env["HC_SOCKET"])
        s.sendall(
            json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)).encode()
            + b"\n"
        )
        with s.makefile("rb") as stream:
            d = json.loads(stream.readline(2**20))
        if "error" in d:
            raise RuntimeError(d["error"])
        return d["result"]


try:
    r = subprocess.run(
        [
            herdr_binary,
            "run",
            "--bg",
            "--no-parent",
            "--no-notify",
            "--cwd",
            str(workspace),
            "--",
            *argv,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode:
        raise RuntimeError(r.stderr)
    address = json.loads(r.stdout)["address"]
    print("private terminal", address, "provider", provider, flush=True)
    # Wait on output in this isolated terminal; not on a real user's agent/pane.
    subprocess.run(
        [herdr_binary, "wait", address, "--output", "for shortcuts", "--timeout", "12"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    screen = rpc("terminal.read", {"terminal": address})["text"]
    print("initial screen", screen[-3500:], flush=True)
    if "trust" in screen.lower() and (
        "directory" in screen.lower()
        or "folder" in screen.lower()
        or "workspace" in screen.lower()
    ):
        print("accepting trust for invented empty fixture workspace only", flush=True)
        rpc("terminal.keys", {"terminal": address, "keys": ["down", "enter"]})
        subprocess.run(
            [
                herdr_binary,
                "wait",
                address,
                "--output",
                "for shortcuts",
                "--timeout",
                "8",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=12,
        )
        print(
            "after fixture trust",
            rpc("terminal.read", {"terminal": address})["text"][-2000:],
            flush=True,
        )
    if provider == "codex" and "loading" in screen:
        subprocess.run(
            [
                herdr_binary,
                "wait",
                address,
                "--output",
                "orchestra-fixture",
                "--timeout",
                "12",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from agent_orchestra import terminal_control, member, keys
    from unittest import mock

    t = rpc("terminal.get", {"terminal": address})
    owner = t["pid"]
    with mock.patch.dict(
        os.environ,
        {
            "AGENT_ORCHESTRA_HOME": str(root / "orchestra"),
            "HC_TERMINAL_SOCKET": env["HC_SOCKET"],
            "HC_TERMINAL_ID": address,
        },
    ):
        binding = terminal_control.capture_binding(owner)
        assert binding, "fixture binding failed"
        seat = {
            "member_id": "mb_fixture0001",
            "name": "fixture",
            "provider": provider,
            "owner_pid": owner,
            "conductor_id": "mb_conductor01",
            "terminal_control": binding,
        }
        member.save_member(seat)
        with mock.patch.object(keys, "find_pane", return_value=None):
            control = terminal_control.resolve(
                seat, lambda: member.load_member(seat["member_id"])
            )
            print("actual adapter initial prompt", control.prompt(provider), flush=True)
            original_type = terminal_control.Herdr.type_text

            def traced_type(self, *a, **kw):
                try:
                    return original_type(self, *a, **kw)
                except Exception as exc:
                    print(
                        "adapter diagnostic", type(exc).__name__, str(exc), flush=True
                    )
                    if self.last:
                        print(
                            "last prompt",
                            repr(self.last.prompt(provider)),
                            "screen",
                            self.last.plain[-2000:],
                            flush=True,
                        )
                    print(
                        "actual plain read",
                        rpc("terminal.read", {"terminal": address})["text"][-2000:],
                        flush=True,
                    )
                    raise

            with mock.patch.object(terminal_control.Herdr, "type_text", traced_type):
                envelope = {
                    "id": "m_fixture00000001",
                    "from": {"id": "mb_conductor01"},
                    "act": "type",
                    "text": "ACT type\nTO mb_fixture0001\nTYPE enter idle quiet=0\n\n"
                    + command,
                }
                result = member._handle_type(seat, envelope)
            print("ACTUAL ADAPTER OUTCOME", result, flush=True)
            assert result == "typed", result
    deadline = time.monotonic() + 15
    matched = False
    while time.monotonic() < deadline:
        try:
            path, payload = requests.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            break
        # Skill body must be expanded by actual CLI input handling, not merely its command text.
        if marker.encode() in payload and (
            path.split("?")[0].endswith("/messages")
            or path.split("?")[0].endswith("/responses")
        ):
            matched = True
            print(
                "SKILL DISPATCH VERIFIED",
                provider,
                "request",
                path,
                "contains invented skill BODY",
                flush=True,
            )
            break
    print(
        "final screen",
        rpc("terminal.read", {"terminal": address})["text"][-2500:],
        flush=True,
    )
    assert matched, "actual CLI skill expansion not observed"
    # Real composer continuation rows, no-enter, draft refusal, and key-only submit.
    rpc("terminal.wait", {"terminal": address, "timeout": 8})
    multiline = "\nORCHESTRA_INVENTED_MULTILINE_BODY\nthird line"
    with (
        mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": str(root / "orchestra")}),
        mock.patch.object(keys, "find_pane", return_value=None),
    ):
        envelope = {
            "id": "m_fixture00000002",
            "from": {"id": "mb_conductor01"},
            "act": "type",
            "text": "ACT type\nTO mb_fixture0001\nTYPE no-enter idle quiet=0\n\n"
            + multiline,
        }
        no_enter = member._handle_type(seat, envelope)
        if no_enter != "typed":
            print("no-enter outcome", no_enter, flush=True)
            print(member._typed_log(seat["member_id"]).read_text()[-2500:], flush=True)
            print(
                rpc("terminal.read", {"terminal": address})["text"][-2500:], flush=True
            )
        assert no_enter == "typed", "no-enter failed"
        assert (
            "ORCHESTRA_INVENTED_MULTILINE_BODY"
            in rpc("terminal.read", {"terminal": address})["text"]
        )
        assert not any(
            b"ORCHESTRA_INVENTED_MULTILINE_BODY" in payload
            for _, payload in list(requests.queue)
        ), "no-enter submitted"
        refused = {
            **envelope,
            "id": "m_fixture00000003",
            "text": "ACT type\nTO mb_fixture0001\nTYPE enter idle quiet=0\n\nshould refuse",
        }
        assert member._handle_type(seat, refused) == "refused-busy", (
            "multiline draft was not protected"
        )
        key = {
            **envelope,
            "id": "m_fixture00000004",
            "text": "ACT type\nTO mb_fixture0001\nTYPE key=Enter quiet=0\n\n",
        }
        assert member._handle_type(seat, key) == "typed", "key-only failed"
        deadline = time.monotonic() + 10
        multiline_matched = False
        while time.monotonic() < deadline:
            try:
                path, payload = requests.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            if b"ORCHESTRA_INVENTED_MULTILINE_BODY" in payload and (
                path.split("?")[0].endswith("/messages")
                or path.split("?")[0].endswith("/responses")
            ):
                multiline_matched = True
                break
        assert multiline_matched, "multiline body did not reach local model"
        print(
            "MULTILINE / NO-ENTER / DRAFT REFUSAL / KEY-ONLY VERIFIED",
            provider,
            flush=True,
        )

finally:
    try:
        r = subprocess.run(
            [herdr_binary, "server", "stop", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=12,
        )
        print("private core cleanup", r.returncode, r.stdout.strip(), flush=True)
        if (
            r.returncode == 0
            and os.environ.get("AGENT_TEST_KEEP_HERDR_ARTIFACTS") != "1"
        ):
            shutil.rmtree(root)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
