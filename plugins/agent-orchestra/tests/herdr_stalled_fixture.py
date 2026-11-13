"""Deliberately killed by the parent to exercise private-core fallback cleanup."""

import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from herdr_fixture_env import environment, fixture_root, rpc

root = fixture_root()
env = environment(root)
launched = subprocess.run(
    [
        env["AGENT_TEST_HERDR_BIN"],
        "run",
        "--bg",
        "--no-parent",
        "--no-notify",
        "--",
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import signal; signal.pause()",
    ],
    env=env,
    capture_output=True,
    text=True,
    timeout=15,
    check=True,
)
address = json.loads(launched.stdout)["address"]
terminal = rpc(env, "terminal.get", terminal=address)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import terminal_control  # noqa: E402

pid = terminal["pid"]
(root / "owned-program.json").write_text(
    json.dumps(dict(pid=pid, start=terminal_control.process(pid).start))
)
print("PRIVATE PROGRAM STARTED", flush=True)
time.sleep(120)  # Intentionally no child-owned finally: the parent is responsible.
