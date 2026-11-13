"""Private PTY readers model an account runner replacing its owning process."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import tty
from unittest import mock


def reader(role: str, endpoint: str) -> None:
    with socket.socket(socket.AF_UNIX) as control:
        control.connect(endpoint)

        def event(**data):
            control.sendall(json.dumps(data).encode() + b"\n")

        draft = transcript = ""

        def draw():
            screen = (
                "\x1b[2J\x1b[H\x1b[?2004h"
                + transcript
                + "─" * 60
                + "\r\n❯ "
                + draft.replace("\n", "\r\n  ")
                + "\r\n"
                + "─" * 60
                + "\r\n? for shortcuts"
            )
            os.write(1, screen.encode() + b"\x1b]7501;state=idle:app=claude\x1b\\")

        draw()
        event(event="ready", role=role, pid=os.getpid())
        pending = b""
        while True:
            chunk = os.read(0, 65536)
            if not chunk:
                return
            pending += chunk
            while pending:
                if pending.startswith(b"\x1b[200~"):
                    end = pending.find(b"\x1b[201~")
                    if end < 0:
                        break
                    draft += pending[6:end].decode()
                    pending = pending[end + 6 :]
                    event(event="paste", text=draft)
                    if role == "A" and draft == "/rollover-after-paste":
                        return  # Runner reaps A and starts B on the same PTY.
                    draw()
                elif pending.startswith(b"\r"):
                    pending = pending[1:]
                    event(event="submitted", text=draft)
                    transcript += f"❯ {draft}\r\nINVENTED {role} DONE\r\n"
                    draft = ""
                    draw()
                elif len(pending) < 6 and b"\x1b[200~".startswith(pending):
                    break
                else:
                    raise AssertionError(f"unexpected invented input: {pending!r}")


def runner(endpoint: str) -> None:
    tty.setraw(0)
    for role in ("A", "B"):
        child = subprocess.Popen([sys.executable, __file__, "--reader", role, endpoint])
        child.wait()


def main() -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent_orchestra import keys, member, terminal_control

    root = Path(tempfile.mkdtemp(prefix="aoroll-", dir="/tmp")).resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HC_", "XDG_"))}
    env.update(
        HC_SOCKET=str(root / "core.sock"),
        HC_SESSION="orchestra-rollover-fixture",
        XDG_STATE_HOME=str(root / "state"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_DATA_HOME=str(root / "data"),
    )
    hc = os.environ.get("AGENT_TEST_HERDR_BIN", "hc")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(root / "reader.sock"))
        listener.listen(2)
        listener.settimeout(10)
        connections = []
        try:
            launched = subprocess.run(
                [
                    hc,
                    "run",
                    "--bg",
                    "--no-parent",
                    "--no-notify",
                    "--",
                    sys.executable,
                    __file__,
                    "--runner",
                    str(root / "reader.sock"),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            address = json.loads(launched.stdout)["address"]

            def accept():
                connection, _ = listener.accept()
                connection.settimeout(10)
                stream = connection.makefile("rb")
                connections.append((connection, stream))
                ready = json.loads(stream.readline())
                assert ready["event"] == "ready"
                return ready["pid"], stream

            owner_a, stream_a = accept()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "AGENT_ORCHESTRA_HOME": str(root / "orchestra"),
                        "HC_TERMINAL_SOCKET": env["HC_SOCKET"],
                        "HC_TERMINAL_ID": address,
                    },
                ),
                mock.patch.object(keys, "find_pane", return_value=None),
            ):
                binding = terminal_control.capture_binding(owner_a)
                assert binding
                seat = {
                    "member_id": "mb_rollover01",
                    "provider": "claude",
                    "owner_pid": owner_a,
                    "conductor_id": "mb_conductor1",
                    "terminal_control": binding,
                }
                member.save_member(seat)
                original = {
                    "id": "m_rollover000001",
                    "from": {"id": "mb_conductor1"},
                    "act": "type",
                    "text": "ACT type\nTO mb_rollover01\nTYPE enter idle quiet=60\n\n/rollover-after-paste",
                }
                assert member._handle_type(seat, original) == "delivery-uncertain"
                assert json.loads(stream_a.readline()) == {
                    "event": "paste",
                    "text": "/rollover-after-paste",
                }
                owner_b, stream_b = accept()
                assert owner_b != owner_a
                assert member._handle_type(seat, original) == "duplicate"
                # The fixture provides the owning caller identity; actual normal
                # claim/adoption code captures and validates B's new OS binding.
                with mock.patch.object(
                    member.core, "agent_session_pid", return_value=owner_b
                ):
                    adopted = member.claim_ownership(
                        member.load_member(seat["member_id"])
                    )
                assert adopted["owner_pid"] == owner_b
                assert adopted["terminal_control"]["pid"] == binding["pid"]
                assert adopted["terminal_control"]["address"] == binding["address"]
                following = {
                    **original,
                    "id": "m_rollover000002",
                    "text": "ACT type\nTO mb_rollover01\nTYPE enter idle quiet=0\n\n/inert-on-b",
                }
                assert member._handle_type(adopted, following) == "typed"
                # An Enter or replay from the old operation would appear first.
                assert json.loads(stream_b.readline()) == {
                    "event": "paste",
                    "text": "/inert-on-b",
                }
                assert json.loads(stream_b.readline()) == {
                    "event": "submitted",
                    "text": "/inert-on-b",
                }
                long_text = "long invented path/" + "x" * 240 + " and following words"
                wrapped = {
                    **following,
                    "id": "m_rollover000003",
                    "text": "ACT type\nTO mb_rollover01\nTYPE enter idle quiet=0\n\n"
                    + long_text,
                }
                assert member._handle_type(adopted, wrapped) == "typed"
                assert json.loads(stream_b.readline()) == {
                    "event": "paste",
                    "text": long_text,
                }
                assert json.loads(stream_b.readline()) == {
                    "event": "submitted",
                    "text": long_text,
                }
                print(
                    "ROLLOVER VERIFIED: stopped after paste, no replay, adopted replacement before binding"
                )
        finally:
            try:
                result = subprocess.run(
                    [hc, "server", "stop", "--json"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=12,
                )
                print("private core cleanup", result.returncode, result.stdout.strip())
                if (
                    result.returncode == 0
                    and os.environ.get("AGENT_TEST_KEEP_HERDR_ARTIFACTS") != "1"
                ):
                    shutil.rmtree(root)
            finally:
                for connection, stream in connections:
                    stream.close()
                    connection.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--reader":
        reader(sys.argv[2], sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "--runner":
        runner(sys.argv[2])
    else:
        main()
