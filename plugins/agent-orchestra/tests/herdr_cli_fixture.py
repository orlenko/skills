"""Local-only actual CLI skill dispatch probe; no real provider, account or user pane."""

import json
import os
import pathlib
import queue
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

provider = sys.argv[1]
herdr_binary = os.environ.get("AGENT_TEST_HERDR_BIN", "hc")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from herdr_fixture_env import environment, fixture_root  # noqa: E402

root = fixture_root()
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
env = environment(root)
workspace = root / "workspace"
workspace.mkdir()
subprocess.run(["git", "init", "-q", str(workspace)], env=env, check=True)
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
        'model = "orchestra-fixture"\nmodel_provider = "mock"\napproval_policy = "never"\nsandbox_mode = "read-only"\n[features]\nhooks = false\nplugins = false\nremote_plugin = false\n[model_providers.mock]\nname = "Local fixture"\nbase_url = "http://127.0.0.1:'
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
    version = subprocess.run(
        [argv[0], "--version"],
        env=env,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    print("PROVIDER", provider, version.stdout.strip(), "BINARY", argv[0], flush=True)
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
    from agent_orchestra import terminal_control, member, keys, core
    from unittest import mock

    # Identify only invented descendants, before any input is attempted.
    def private_owner():
        t = rpc("terminal.get", {"terminal": address})
        candidates = []
        root_pid = t["pid"]
        root_info = terminal_control.process(root_pid)
        for pid, info in terminal_control._group_members(
            root_info.group, time.monotonic() + 2
        ):
            try:
                _, _, ancestors = terminal_control._prove(pid, t)
            except keys.KeysError:
                continue
            if (
                pid == root_pid or core.agent_kind(pid) == provider
            ) and terminal_control._reads_tty(pid, info.tty):
                candidates.append((pid, ancestors))
        owners = [
            pid
            for pid, _ in candidates
            if not any(
                pid in ancestors and pid != other for other, ancestors in candidates
            )
        ]
        return owners[0] if len(owners) == 1 else None

    discover_until = time.monotonic() + 20
    discovery_error = "no unique provider UI"
    while True:
        try:
            owner = private_owner()
            if owner:
                break
        except (keys.KeysError, OSError) as exc:
            discovery_error = str(exc)
        assert time.monotonic() < discover_until, (
            "private provider startup not ready",
            discovery_error,
        )
        time.sleep(0.05)
    session = terminal_control.Session(env["HC_SOCKET"], time.monotonic() + 2)
    print("RUNNING CORE", session.version, flush=True)
    session.close()
    with mock.patch.dict(
        os.environ,
        {
            "AGENT_ORCHESTRA_HOME": str(root / "orchestra"),
            "HC_TERMINAL_SOCKET": env["HC_SOCKET"],
            "HC_TERMINAL_ID": address,
        },
    ):
        bind_until = time.monotonic() + 20
        while True:
            binding = terminal_control.capture_binding(owner)
            if binding:
                break
            assert time.monotonic() < bind_until, "fixture binding failed"
            time.sleep(0.05)
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
            # No input has been attempted. Let private startup helpers finish
            # before the test begins, rather than treating a transient worker
            # inheriting readable TTY descriptors as the owning provider UI.
            ready_until = time.monotonic() + 20
            ready_screen = None
            while True:
                control = terminal_control.resolve(
                    seat, lambda: member.load_member(seat["member_id"])
                )
                if control is not None:
                    try:
                        shown = control.prompt(provider)
                        ready_screen = control.last
                    except keys.KeysError:
                        shown = None
                    if shown is not None and shown.idle:
                        break
                assert time.monotonic() < ready_until, (
                    "private CLI never obtained an unambiguous idle composer",
                    (ready_screen.cursor, ready_screen.bounds(), ready_screen.plain)
                    if ready_screen
                    else None,
                )
                time.sleep(0.05)
            print("actual adapter initial prompt", control.prompt(provider), flush=True)
            original_type = terminal_control.Herdr.type_text
            original_submitted = terminal_control.Herdr._submitted

            def diagnostic_submitted(before, after, intended, agent):
                confirmed = original_submitted(before, after, intended, agent)
                if not confirmed:
                    print(
                        "submission bounds",
                        before.bounds(),
                        after.bounds(),
                        "intended",
                        repr(intended),
                        "before cursor",
                        before.cursor,
                        "after cursor",
                        after.cursor,
                        flush=True,
                    )
                    needle = terminal_control._rendered_text(
                        keys.PROMPT_GLYPHS[agent] + intended
                    )
                    print(
                        "transcript counts",
                        *[
                            terminal_control._rendered_text(
                                "\n".join(s.plain.splitlines()[: s.bounds()[0]])
                            ).count(needle)
                            for s in (before, after)
                        ],
                        "prompts",
                        before.prompt(agent),
                        after.prompt(agent),
                        flush=True,
                    )
                    print(
                        "before nonblank",
                        [
                            row
                            for row in before.plain.splitlines()[: before.bounds()[0]]
                            if row.strip()
                        ][-12:],
                        flush=True,
                    )
                return confirmed

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

            with (
                mock.patch.object(terminal_control.Herdr, "type_text", traced_type),
                mock.patch.object(
                    terminal_control.Herdr,
                    "_submitted",
                    staticmethod(diagnostic_submitted),
                ),
            ):
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
        rpc("terminal.wait", {"terminal": address, "timeout": 8})

        def edit(number, flags, body=""):
            operation = {
                **envelope,
                "id": f"m_fixture{number:08d}",
                "text": f"ACT type\nTO mb_fixture0001\nTYPE {flags} quiet=0\n\n{body}",
            }
            outcome = member._handle_type(seat, operation)
            assert outcome == "typed", (
                flags,
                outcome,
                member._typed_log(seat["member_id"]).read_text()[-2000:],
            )

        edit(5, "no-enter idle", "ORCHESTRA_EDIT_AB")
        edit(6, "no-enter anytime", "C")
        edit(7, "anytime key=Left key=BSpace")
        edit(8, "no-enter anytime", "Z")
        edit(9, "key=Enter")
        deadline = time.monotonic() + 10
        edited = False
        while time.monotonic() < deadline:
            try:
                path, payload = requests.get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            if b"ORCHESTRA_EDIT_AZC" in payload and path.split("?")[0].endswith(
                ("/messages", "/responses")
            ):
                edited = True
                break
        assert edited, (
            "real CLI append/navigation/delete/mid-draft insertion not observed"
        )
        print(
            "ANYTIME APPEND / CURSOR NAVIGATION / DELETE / MID-DRAFT INSERTION VERIFIED",
            provider,
            flush=True,
        )

finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    # The unittest parent owns the core even if this process is killed.
