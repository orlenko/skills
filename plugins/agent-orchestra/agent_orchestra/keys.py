"""Type into the tmux pane that holds a member's agent session, as a person would.

The screen reading and the paste-safe submit come from agent-nudge
(`agent_nudge/system.py`, `agent_nudge/screen.py`); each plugin installs on its
own, so the parts this needs live here. Every tmux and ps call goes through
`run`, so tests swap in a fake and never need a real tmux.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=10, check=True).stdout


run: Runner = _run
sleep: Callable[[float], None] = time.sleep

PROMPT_GLYPHS = {"claude": "❯", "codex": "›"}
# Codex reads a burst of keys as a paste, and an Enter inside the burst becomes
# a newline in the composer. A pause lets the burst end before Enter lands.
SUBMIT_PAUSE_SECONDS = 0.8
SUBMIT_CHECK_SECONDS = 1.5
_ANCESTOR_LEVELS = 12
_SEP = "|:orchestra:|"
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_OTHER_ESC = re.compile(r"\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")
_WORKING = re.compile(r"esc to interrupt|ctrl\+c to interrupt|press esc to stop", re.I)
# Claude's live spinner: a glyph and one word ending in an ellipsis ("✳ Nucleating…").
_SPINNER = re.compile(r"^\s*\S\s+[A-Z][\w-]*…")
# The rule Claude draws above its input box, sometimes with the session name
# in it ("──── Trumpet ─").
_RULE = re.compile(r"^[\s─━═╌┄-]+(\S.*\S\s*[─━═]+\s*)?$")


class KeysError(Exception):
    """tmux or ps failed, or the pane went away."""


@dataclass
class Pane:
    id: str
    pid: int
    session: str


@dataclass
class Prompt:
    shown: bool
    typed: str
    working: bool

    @property
    def idle(self) -> bool:
        return self.shown and not self.typed and not self.working


def _call(args: list[str]) -> str:
    try:
        return run(args)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeysError(f"{args[0]} {args[1] if len(args) > 1 else ''}: {exc}".strip()) from exc


def find_pane(pid: int) -> Pane | None:
    """The tmux pane whose shell is `pid` or one of its ancestors."""
    try:
        listing = run(["tmux", "list-panes", "-a", "-F",
                       _SEP.join(("#{pane_id}", "#{pane_pid}", "#{session_name}", "#{pane_dead}"))])
        table = run(["ps", "-A", "-o", "pid=,ppid="])
    except (OSError, subprocess.SubprocessError):
        return None
    panes: dict[int, Pane] = {}
    for line in listing.splitlines():
        parts = line.split(_SEP)
        if len(parts) != 4 or parts[3] == "1":
            continue
        try:
            panes[int(parts[1])] = Pane(id=parts[0], pid=int(parts[1]), session=parts[2])
        except ValueError:
            continue
    parents: dict[int, int] = {}
    for line in table.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            parents[int(fields[0])] = int(fields[1])
    current = pid
    for _ in range(_ANCESTOR_LEVELS):
        if current in panes:
            return panes[current]
        current = parents.get(current, 0)
        if current <= 1:
            return None
    return None


def _plain_and_dim(line: str) -> tuple[str, list[bool]]:
    line = _OTHER_ESC.sub("", line)
    text: list[str] = []
    dim_mask: list[bool] = []
    dim, pos = False, 0
    for match in _SGR.finditer(line):
        chunk = line[pos:match.start()]
        text.append(chunk)
        dim_mask.extend([dim] * len(chunk))
        for code in [c for c in match.group(1).split(";") if c != ""] or ["0"]:
            if code in {"0", "22"}:
                dim = False
            elif code == "2":
                dim = True
        pos = match.end()
    chunk = line[pos:]
    text.append(chunk)
    dim_mask.extend([dim] * len(chunk))
    return "".join(text), dim_mask


def read_prompt(raw: str, agent: str = "") -> Prompt:
    """Whether the input box shows, what is typed in it, and whether a turn runs.

    Suggestions and placeholders are drawn dim, so `❯ check messages` can be an
    empty box; only text without the dim attribute counts as typed. Either
    agent's glyph counts: a seat records the provider that joined it, and on
    2026-09-24 a Claude session held a seat joined by Codex, so reading only
    Codex's `›` found no input box on a Claude screen. `agent` is kept only to
    say which glyph to prefer on a line that starts with both.
    """
    glyphs = tuple(dict.fromkeys((PROMPT_GLYPHS.get(agent, "❯"), *PROMPT_GLYPHS.values())))
    parsed = [_plain_and_dim(line) for line in raw.rstrip("\n").split("\n")]
    prompt_at = glyph = None
    for index in range(len(parsed) - 1, -1, -1):
        head = parsed[index][0].lstrip(" │┃")
        glyph = next((item for item in glyphs if head.startswith(item)), None)
        # Claude also draws `❯` as a menu cursor ("❯ No, exit" in the trust
        # dialog) and before every past prompt in the transcript. Only the
        # line under a rule is the input box, and Enter on a menu picks it.
        if glyph == PROMPT_GLYPHS["claude"] and not (
            index > 0 and parsed[index - 1][0].strip() and _RULE.match(parsed[index - 1][0])
        ):
            glyph = None
        if glyph:
            prompt_at = index
            break
    if prompt_at is None or glyph is None:
        return Prompt(shown=False, typed="", working=False)
    text, dim = parsed[prompt_at]
    start = text.index(glyph) + len(glyph)
    typed = "".join(ch for ch, d in zip(text[start:], dim[start:]) if not d)
    typed = typed.replace("\xa0", " ").strip(" │┃").strip()
    above = [line for line, _ in parsed[:prompt_at]]
    tail = "\n".join(line for line in above[-40:] if line.strip())
    footer = "\n".join(line for line, _ in parsed[prompt_at + 1:])
    working = bool(_WORKING.search(tail[-600:] + "\n" + footer)) or any(
        _SPINNER.match(line) for line in above[-6:]
    )
    return Prompt(shown=True, typed=typed, working=working)


def prompt(pane_id: str, agent: str) -> Prompt:
    return read_prompt(_call(["tmux", "capture-pane", "-p", "-e", "-t", pane_id]), agent)


def send_keys(pane_id: str, names: tuple[str, ...]) -> None:
    """Press named keys (tmux key names: Enter, Escape, C-c, Up, Tab)."""
    for name in names:
        _call(["tmux", "send-keys", "-t", pane_id, name])
        sleep(0.1)


def type_text(pane_id: str, agent: str, text: str, *, submit: bool) -> bool:
    """Type `text`; with `submit`, press Enter and confirm the box emptied.

    One line goes in as literal keys. Several go in as a bracketed paste, so a
    newline stays in the text instead of submitting its first line. Returns
    False when the text still sat in the box after a second Enter.
    """
    if "\n" in text:
        buffer = f"orchestra-{os.getpid()}"
        _call(["tmux", "set-buffer", "-b", buffer, "--", text])
        _call(["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", pane_id])
    else:
        _call(["tmux", "send-keys", "-t", pane_id, "-l", "--", text])
    if not submit:
        return True
    sleep(SUBMIT_PAUSE_SECONDS)
    _call(["tmux", "send-keys", "-t", pane_id, "Enter"])
    for attempt in range(2):
        sleep(SUBMIT_CHECK_SECONDS)
        if not prompt(pane_id, agent).typed:
            return True
        if attempt == 0:
            _call(["tmux", "send-keys", "-t", pane_id, "Enter"])
    return False
