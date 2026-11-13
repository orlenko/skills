"""Parent-owned private Herdr fixture lifecycle, never a default/user core."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import signal
import subprocess
import tempfile


def create_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="ao-private-", dir="/tmp")).resolve()
    root.chmod(0o700)
    (root / "fixture-owner").write_text(str(os.getpid()))
    return root


def fixture_root() -> Path:
    # Only the unittest parent creates the root and owns final cleanup.
    root = Path(os.environ["AGENT_TEST_HERDR_ROOT"])
    assert root.is_absolute() and root.name.startswith("ao-private-")
    assert root.stat().st_uid == os.getuid() and (root / "fixture-owner").is_file()
    return root


def environment(root: Path) -> dict[str, str]:
    env = {
        k: os.environ[k]
        for k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
        if k in os.environ
    }
    for name in ("home", "tmp", "state", "config", "cache", "data", "runtime"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    env.update(
        HOME=str(root / "home"),
        TMPDIR=str(root / "tmp"),
        HC_SOCKET=str(root / "core.sock"),
        HC_SESSION="orchestra-private-fixture",
        XDG_STATE_HOME=str(root / "state"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_DATA_HOME=str(root / "data"),
        XDG_RUNTIME_DIR=str(root / "runtime"),
        AGENT_TEST_HERDR_ROOT=str(root),
        AIQ_BYPASS="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    for name in ("HERDR", "CODEX", "CLAUDE"):
        key = "AGENT_TEST_" + name + "_BIN"
        if key in os.environ:
            binary = shutil.which(os.environ[key])
            if not binary:
                raise ValueError(f"fixture binary unavailable: {key}")
            env[key] = str(Path(binary).absolute())
    return env


def rpc(env: dict[str, str], method: str, **params):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(3)
        connection.connect(env["HC_SOCKET"])
        connection.sendall(
            json.dumps(dict(jsonrpc="2.0", id=1, method=method, params=params)).encode()
            + b"\n"
        )
        with connection.makefile("rb") as stream:
            reply = json.loads(stream.readline(1024 * 1024))
        if "error" in reply:
            raise RuntimeError(reply["error"])
        return reply["result"]


def run_fixture(argv: list[str], root: Path, env: dict[str, str], timeout: float):
    # Stop the entire owned launcher group on timeout; otherwise an hc launcher
    # could outlive the Python child and create a core after fallback cleanup.
    with subprocess.Popen(
        argv,
        env=env,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = child.communicate(timeout=3)
            exc.output, exc.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(
            argv, child.returncode, stdout.decode(), stderr.decode()
        )


def cleanup(root: Path, env: dict[str, str]) -> str:
    assert (root / "fixture-owner").read_text() == str(os.getpid())
    assert env["HC_SOCKET"] == str(root / "core.sock")
    outcome = "private core not started"
    if (root / "core.sock").exists():
        result = subprocess.run(
            [env["AGENT_TEST_HERDR_BIN"], "server", "stop", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=12,
        )
        outcome = f"private core cleanup {result.returncode} {result.stdout.strip()}"
        if result.returncode or '"stopped":true' not in result.stdout:
            raise RuntimeError(f"{outcome}; diagnostics retained at {root}")
    shutil.rmtree(root)
    return outcome
