"""tmux and the process table: the only two things this tool touches.

Everything goes through `run`, so tests swap it for a fake and nothing here
needs a real tmux.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=True).stdout


run: Runner = _run

# An agent is a process whose executable is named for it. Node-installed Codex
# runs as `node .../@openai/codex/bin/codex.js`, so the script name counts too.
AGENTS = {"claude": "claude", "codex": "codex"}
# Printable on purpose: tmux 3.4 (Ubuntu 24.04) prints a control character in
# -F output as an octal escape ("\\037"), so a \x1f separator never splits there.
_SEP = "|:nudge:|"


@dataclass
class Pane:
    id: str
    pid: int
    session: str
    window: str
    path: str
    opt_out: bool
    agent: str | None = None
    agent_pid: int | None = None
    extra: dict = field(default_factory=dict)


def panes() -> list[Pane]:
    fmt = _SEP.join(("#{pane_id}", "#{pane_pid}", "#{session_name}", "#{window_id}",
                     "#{pane_current_path}", "#{pane_dead}", "#{@nudge}"))
    try:
        out = run(["tmux", "list-panes", "-a", "-F", fmt])
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 7 or parts[5] == "1":
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        rows.append(Pane(id=parts[0], pid=pid, session=parts[2], window=parts[3], path=parts[4],
                         opt_out=parts[6].strip().lower() in {"off", "0", "no", "false"}))
    return rows


def client_activity() -> dict[str, float]:
    """Newest keyboard activity per tmux session, from attached clients."""
    try:
        out = run(["tmux", "list-clients", "-F", f"#{{client_session}}{_SEP}#{{client_activity}}"])
    except (OSError, subprocess.SubprocessError):
        return {}
    newest: dict[str, float] = {}
    for line in out.splitlines():
        session, _, stamp = line.partition(_SEP)
        try:
            newest[session] = max(newest.get(session, 0.0), float(stamp))
        except ValueError:
            continue
    return newest


def capture(pane_id: str) -> str:
    """The visible screen with SGR attributes, so dim ghost text can be told from input."""
    return run(["tmux", "capture-pane", "-p", "-e", "-t", pane_id])


# Codex reads keys that arrive in a burst as a paste, and an Enter inside the
# burst becomes a newline in the composer: on 2026-09-21 three Codex nudges sat
# typed but unsent. A pause lets the burst end before Enter lands.
SUBMIT_PAUSE_SECONDS = 0.8
SUBMIT_CHECK_SECONDS = 1.5
_PROMPT_GLYPHS = ("❯", "›")


def _still_in_composer(pane_id: str, text: str) -> bool:
    """Whether the start of `text` still sits after the prompt glyph, unsent."""
    try:
        screen = run(["tmux", "capture-pane", "-p", "-t", pane_id])
    except (OSError, subprocess.SubprocessError):
        return False
    head = " ".join(text.split())[:24]
    for line in reversed(screen.splitlines()):
        stripped = line.strip(" │┃")
        if stripped.startswith(_PROMPT_GLYPHS):
            return head in " ".join(stripped[1:].split())
    return False


def send(pane_id: str, text: str, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Type `text` and submit it. Returns False if it still sat unsent after a second Enter."""
    run(["tmux", "send-keys", "-t", pane_id, "-l", "--", text])
    sleep(SUBMIT_PAUSE_SECONDS)
    run(["tmux", "send-keys", "-t", pane_id, "Enter"])
    for _ in range(2):
        sleep(SUBMIT_CHECK_SECONDS)
        if not _still_in_composer(pane_id, text):
            return True
        run(["tmux", "send-keys", "-t", pane_id, "Enter"])
    return not _still_in_composer(pane_id, text)


def process_table() -> dict[int, tuple[int, list[str]]]:
    """pid -> (ppid, argv). Arguments stay in memory only: they can hold secrets."""
    try:
        out = run(["ps", "-A", "-o", "pid=,ppid=,args="])
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2].split())
        except ValueError:
            continue
    return table


def agent_of(argv: list[str]) -> str | None:
    if not argv:
        return None
    for word in argv[:2]:
        name = os.path.basename(word)
        for suffix in (".js", ".cjs", ".mjs"):
            name = name.removesuffix(suffix)
        if name in AGENTS:
            return AGENTS[name]
    return None


def find_agent(pane: Pane, table: dict[int, tuple[int, list[str]]], depth: int = 4) -> None:
    """The first agent process at or below the pane's shell, breadth first.

    `extra["agent_pids"]` holds that process and the agent processes under it.
    npm's Codex is a `node .../bin/codex` wrapper around a native `codex`, and a
    hook records the inner one as the session's pid.
    """
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    level = [pane.pid]
    for _ in range(depth + 1):
        for pid in level:
            agent = agent_of(table.get(pid, (0, []))[1])
            if agent:
                pane.agent, pane.agent_pid = agent, pid
                pids, frontier = [pid], [pid]
                for _ in range(2):
                    frontier = [c for p in frontier for c in children.get(p, [])
                                if agent_of(table.get(c, (0, []))[1])]
                    pids.extend(frontier)
                pane.extra["agent_pids"] = pids
                return
        level = [child for pid in level for child in sorted(children.get(pid, []))]
        if not level:
            return
