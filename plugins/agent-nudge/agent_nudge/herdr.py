"""Discover and nudge the local foreground provider of an AIQ Herdr long session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import native_terminal as native
from . import screen, system

Error = native.NativeError
ADDRESS = re.compile(r"t[1-9][0-9bcdfghjkmnpqrstvwxyz]*\Z")
OFF = {"off", "0", "no", "false"}


@dataclass(frozen=True)
class Record:
    file: Path
    ref: str
    generation: int
    workspace: str
    socket: str
    core: str
    address: str
    root: int
    runner: int
    launch: int

    @classmethod
    def read(cls, path: Path) -> Record:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 65536):
                raise Error("not a private AIQ descriptor")
            raw = json.loads(stream.read(65537))
        if not isinstance(raw, dict) or raw.get("State") != "running":
            raise Error("AIQ session is not running")
        core = raw["Core"]
        record = cls(path, raw["Ref"], raw["Generation"], raw["Workspace"], core["Socket"],
                     core["ID"], core["Terminal"], core["PID"], raw["RunnerPID"], raw["PID"])
        if not all(isinstance(s, str) for s in (record.workspace, record.socket, record.core,
                                                record.address, record.ref)):
            raise Error("invalid AIQ descriptor")
        key = hashlib.sha256(os.fsencode(record.workspace)).hexdigest()[:32]
        if (path.name != key + ".json" or not re.fullmatch(r"herdr:" + key + "-[0-9a-f]{32}", record.ref)
                or not os.path.isabs(record.workspace) or not os.path.isabs(record.socket)
                or not record.core or not ADDRESS.fullmatch(record.address)
                or any(type(n) is not int or n <= 1 for n in (record.root, record.runner, record.launch))
                or type(record.generation) is not int or record.generation < 1):
            raise Error("invalid AIQ descriptor identity")
        return record


def _provider(argv):
    if not argv:
        return None
    binary = os.path.basename(argv[0])
    if binary == "node" and len(argv) > 1:
        return system.AGENTS.get(os.path.basename(argv[1]).removesuffix(".js"))
    return system.AGENTS.get(binary)


def panes(table) -> list[system.Pane]:
    data = os.environ.get("AIQ_DATA_DIR") or Path(os.environ.get("XDG_DATA_HOME")
                                                 or Path.home() / ".local/share") / "aiq"
    result = []
    for file in (Path(data) / "herdr").glob("*.json"):
        try:
            record = Record.read(file)
            parent, argv = table.get(record.launch, (0, []))
            agent = _provider(argv)
            if parent != record.runner or not agent:
                continue
            # npm's Codex wrapper and native executable share a process group.
            # Bind the actual reader, keeping both PIDs for Orchestra seat lookup.
            candidates = [record.launch]
            while len(candidates) < 4:
                children = [p for p, (ppid, args) in table.items()
                            if ppid == candidates[-1] and _provider(args) == agent
                            and native.process(p).group == record.launch and
                            native._reads_tty(p, native.process(p).tty)]
                if len(children) > 1:
                    raise Error("ambiguous provider children")
                if not children:
                    break
                candidates.extend(children)
            owner = candidates[-1]
            target = Target(record, agent, owner, native.process(owner).start,
                            native.process(record.root).start)
            pane_id = f"{record.ref}@{record.generation}/{record.address}"
            result.append(system.Pane(pane_id, record.root, "Herdr " + record.address, "",
                                      record.workspace, False, agent, owner,
                                      {"agent_pids": candidates, "native": target}))
        except (OSError, ValueError, KeyError, TypeError, Error):
            continue
    return result


@dataclass(frozen=True)
class Target:
    record: Record
    agent: str
    owner: int
    owner_start: tuple[int, ...]
    root_start: tuple[int, ...]

    def check(self, terminal, deadline):
        r = self.record
        if (Record.read(r.file) != r or terminal.get("state") != "live"
                or terminal.get("address") != r.address or terminal.get("pid") != r.root
                or native.process(r.launch).parent != r.runner):
            raise Error("AIQ terminal generation changed")
        agent, root, ancestry = native._prove(self.owner, terminal)
        if (agent.start != self.owner_start or root.start != self.root_start
                or r.launch not in ancestry or agent.group != r.launch):
            raise Error("foreground provider changed")
        if (not native._reads_tty(self.owner, agent.tty, deadline)
                or _provider(system.run(["ps", "-p", str(self.owner), "-o", "args="]).split()) != self.agent):
            raise Error("foreground provider is no longer the terminal reader")
        native._exclusive_consumer(self.owner, agent, ancestry, deadline)

    @contextmanager
    def opened(self, pane, *, write=False):
        r = self.record
        deadline = time.monotonic() + 2
        with native.Session(r.socket, deadline, r.core) as session:
            terminal = session.request("terminal.get", terminal=r.address)
            self.check(terminal, deadline)
            cols, rows = terminal["cols"], terminal["rows"]
            if min(cols, rows) <= 0 or cols * rows > native.MAX_CELLS:
                raise Error("unsupported Herdr terminal size")
            attached = session.request("terminal.attach", terminal=r.address, cols=cols, rows=rows,
                                       history=0, observe=not write, resumed=True)
            actual, channel = attached["terminal"], attached["channel"]
            self.check(actual, deadline)
            if ((cols, rows) != (actual["cols"], actual["rows"])
                    or terminal["created_ms"] != actual["created_ms"] or type(channel) is not int
                    or not 0 < channel <= 0xffffffff):
                raise Error("terminal changed during attachment")
            pane.opt_out = actual["labels"].get("nudge", "").lower() in OFF
            status = (actual.get("status") or {}).get("state", "")
            paint = native.render(session.paint(channel), cols, rows, status, deadline=deadline)
            paint.deadline = deadline
            yield session, channel, paint


def _screen(paint, agent):
    parsed = screen.parse(paint.styled, agent)
    bounds = paint.bounds()
    if bounds is None:
        parsed.has_prompt = False
    else:
        first, end = bounds
        lines = paint.styled.splitlines()
        # Use the cursor-bounded composer, including all wrapped draft rows.
        # Transcript glyphs and user-typed lookalikes cannot hide preceding text.
        parsed = screen.parse("\n".join(lines[:first+1] + lines[end:]), agent)
        glyph = screen.PROMPT_GLYPHS[agent]
        parsed.has_prompt &= paint.plain.splitlines()[first].startswith(glyph)
        parts = []
        for index in range(first, end):
            text, mask, reverse = screen._styled_text(lines[index])
            start = len(glyph) if index == first else 0
            # A reverse cursor on the first cell of a dim suggestion makes
            # that cell alone non-dim. Other selected cells are ambiguous.
            marked = [i for i, rev in enumerate(reverse) if rev]
            if marked and (len(marked) != 1 or (marked[0], index) != paint.cursor):
                parsed.has_prompt = False
            if index == first and marked:
                cell = marked[0]
                real = [i for i in range(start, len(text)) if not mask[i] and not text[i].isspace()]
                if real == [cell] and cell + 1 < len(mask) and mask[cell + 1]:
                    mask[cell] = True
            parts.append("".join(c for c, dim in zip(text[start:], mask[start:]) if not dim).strip())
        parsed.typed = "\n".join(parts).strip()
    parsed.has_prompt &= paint.status not in ("blocked", "error")
    parsed.working_marker |= paint.status == "working"
    return parsed


def capture(pane):
    with pane.extra["native"].opened(pane) as (_, _, paint):
        result = _screen(paint, pane.agent)
        pane.extra["cursor"] = paint.cursor
        return result


class Unconfirmed(Error):
    """The core may already have received input. Do not retry this generation."""


def send(pane, text, expected, before_write):
    target = pane.extra["native"]
    if not text or len(text.encode()) > 4096 or any(not c.isprintable() for c in text):
        raise Error("unsupported nudge text")
    armed = False
    try:
        for submit in (False, True):
            with target.opened(pane, write=True) as (session, channel, paint):
                fresh = _screen(paint, pane.agent)
                if (pane.opt_out or not fresh.has_prompt or fresh.working_marker or fresh.background_work
                        or (not submit and (fresh.body_hash != expected.body_hash
                                            or fresh.watchers != expected.watchers))
                        or (not submit and (fresh.typed or paint.cursor != pane.extra["cursor"]))
                        or (submit and "".join(fresh.typed.split()) != "".join(text.split()))):
                    raise Error("screen or composer changed before input")
                terminal = session.request("terminal.get", terminal=target.record.address)
                target.check(terminal, session.deadline)
                if (terminal["labels"].get("nudge", "").lower() in OFF
                        or (terminal.get("status") or {}).get("state") in ("working", "blocked", "error")
                        or (terminal["cols"], terminal["rows"]) != paint.size):
                    raise Error("terminal state changed before input")
                if not armed:
                    before_write()
                    armed = True
                data = text.encode()
                if paint.paste:
                    data = b"\x1b[200~" + data + b"\x1b[201~"
                session.frame(channel, 0, b"\r" if submit else data)
                target.check(session.request("terminal.get", terminal=target.record.address), session.deadline)
            time.sleep(system.SUBMIT_CHECK_SECONDS if submit else system.SUBMIT_PAUSE_SECONDS)
        after = capture(pane)

        def echo_count(body):
            glyph = screen.PROMPT_GLYPHS[pane.agent]
            transcript = "".join(("\0" if line.startswith(glyph) else "") + "".join(line.split())
                                 for line in body.splitlines())
            return transcript.count("\0" + glyph + "".join(text.split()))

        echoed = echo_count(after.body) > echo_count(expected.body)
        if not (after.has_prompt and not after.typed and (after.working_marker or echoed)):
            raise Error("submission not confirmed on screen")
    except Exception as exc:
        if armed:
            raise Unconfirmed("Herdr input may still arrive") from exc
        raise
