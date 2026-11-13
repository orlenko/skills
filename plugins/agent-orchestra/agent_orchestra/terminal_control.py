"""Local interactive input. tmux keeps its existing path; Herdr uses DATA.

Herdr's send/keys RPCs deliberately wait on human input. They cannot implement
our checked-composer operation. Writable DATA has no delivery acknowledgement
and can itself be held: only fresh composer evidence confirms an effect. Never
retry, discover a successor, or finish a partial operation after uncertainty.
"""

from __future__ import annotations

import ctypes
import copy
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import core, keys

MAX_BYTES = 1024 * 1024
MAX_CELLS = 65536
OP_SECONDS = 12
CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
ADDRESS = re.compile(r"t[1-9][0-9bcdfghjkmnpqrstvwxyz]*\Z")
KEY_ALIASES = {
    "Return": "Enter",
    "Esc": "Escape",
    "BackSpace": "BSpace",
    "Delete": "DC",
    "PageUp": "PPage",
    "PageDown": "NPage",
    "BackTab": "BTab",
}


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    tty: int
    foreground: int
    start: tuple[int, ...]


class _ZombieProcess(keys.KeysError):
    """Conclusive kernel exit state; unlike unavailable live process metadata."""


def process(pid: int) -> Process:
    """An OS process incarnation, without reading environment or credentials."""
    try:
        if sys.platform == "darwin":
            buffer = ctypes.create_string_buffer(136)
            size = core._libc().proc_pidinfo(pid, 3, ctypes.c_uint64(0), buffer, 136)
            raw = buffer.raw
            if size != 136 or struct.unpack_from("I", raw, 20)[0] != os.getuid():
                raise ValueError()
            if struct.unpack_from("I", raw, 4)[0] == 5:  # SZOMB
                raise _ZombieProcess("native terminal process has exited")
            return Process(
                struct.unpack_from("I", raw, 16)[0],
                struct.unpack_from("I", raw, 100)[0],
                struct.unpack_from("I", raw, 108)[0],
                struct.unpack_from("I", raw, 112)[0],
                struct.unpack_from("QQ", raw, 120),
            )
        if sys.platform.startswith("linux"):
            path = Path("/proc") / str(pid)
            if path.stat().st_uid != os.getuid():
                raise ValueError()
            raw = (path / "stat").read_text()
            fields = raw[raw.rindex(")") + 2 :].split()
            if fields[0] == "Z":
                raise _ZombieProcess("native terminal process has exited")
            return Process(*(int(fields[i]) for i in (1, 2, 4, 5)), (int(fields[19]),))
    except (OSError, ValueError, IndexError, AttributeError, struct.error):
        pass
    raise keys.KeysError("cannot verify native terminal process identity")


def _prove(owner: int, terminal: dict[str, Any]) -> tuple[Process, Process, set[int]]:
    root = int(terminal.get("pid") or 0)
    agent, shell = process(owner), process(root)
    # Herdr samples foreground metadata periodically. The OS tpgid is current
    # even during startup and account rollover, when that sample can be absent.
    if (
        agent.tty in (0, 0xFFFFFFFF)
        or agent.tty != shell.tty
        or agent.group != agent.foreground
    ):
        raise keys.KeysError(
            "owner is not the direct foreground consumer of this terminal"
        )
    current = owner
    ancestry = set()
    for _ in range(32):
        ancestry.add(current)
        if current == root:
            return agent, shell, ancestry
        current = process(current).parent
        if current <= 1:
            break
    raise keys.KeysError("owner is not inside the recorded terminal")


def _time_check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise keys.KeysError("native input deadline reached")


def _group_members(group: int, deadline: float) -> list[tuple[int, Process]]:
    """Bounded numeric process metadata only; never arguments or environment."""
    members = []
    if sys.platform == "darwin":
        buffer = (ctypes.c_int * 129)()
        size = core._libc().proc_listpids(2, group, buffer, ctypes.sizeof(buffer))
        if size < 0 or size >= ctypes.sizeof(buffer) or size % 4:
            raise keys.KeysError("foreground process-group enumeration unavailable")
        pids = list(buffer)[: size // 4]
    elif sys.platform.startswith("linux"):
        pids = []
        with os.scandir("/proc") as entries:
            count = 0
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                count += 1
                _time_check(deadline)
                if count > 8192:
                    raise keys.KeysError("process discovery exceeds bound")
                try:
                    if entry.stat(follow_symlinks=False).st_uid == os.getuid():
                        pids.append(int(entry.name))
                except FileNotFoundError:
                    continue
    else:
        raise keys.KeysError("native process groups unsupported")
    for pid in pids:
        _time_check(deadline)
        try:
            info = process(pid)
        except _ZombieProcess:
            # A zombie cannot read a terminal; its parent may not have reaped
            # it yet. Unknown metadata for a live process still refuses below.
            continue
        except keys.KeysError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            if sys.platform == "darwin":
                # libproc omits zombies from pidinfo, although listpids and
                # kill(0) retain them. Query ONLY this PID's kernel state; no
                # arguments, paths, environment or credentials are read.
                _time_check(deadline)
                try:
                    state = subprocess.run(
                        ["/bin/ps", "-p", str(pid), "-o", "stat="],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=min(0.25, deadline - time.monotonic()),
                    )
                    _time_check(deadline)
                    if state.returncode == 0 and state.stdout.strip().startswith("Z"):
                        continue
                except (OSError, subprocess.TimeoutExpired):
                    pass
            raise
        if info.group == group:
            members.append((pid, info))
            if len(members) > 128:
                raise keys.KeysError("foreground process group exceeds bound")
    return members


def _reads_tty(pid: int, tty: int, deadline: float | None = None) -> bool:
    """Bounded descriptor/access metadata, including /dev/tty opened after fd0.

    Never resolve descriptor paths or open/read another process's file contents.
    Linux fdinfo contains numeric kernel metadata, not the underlying file.
    """
    deadline = deadline if deadline is not None else time.monotonic() + 2
    devices = (tty, os.stat("/dev/tty").st_rdev)
    if sys.platform == "darwin":
        # SDK proc_info.h: 8-byte proc_fdinfo, 176-byte vnode_fdinfo;
        # fi_openflags at 0 uses FREAD=1, mode at 28 and rdev at 140.
        listing = ctypes.create_string_buffer(4096)
        count = core._libc().proc_pidinfo(pid, 1, ctypes.c_uint64(0), listing, 4096)
        if count <= 0 or count >= 4096 or count % 8:
            raise keys.KeysError("descriptor metadata unavailable or exceeds bound")
        for fd, kind in struct.iter_unpack("iI", listing.raw[:count]):
            _time_check(deadline)
            if kind != 1:  # Pipes, sockets and closed descriptors cannot be TTYs.
                continue
            buffer = ctypes.create_string_buffer(176)
            if core._libc().proc_pidfdinfo(pid, fd, 1, buffer, 176) != 176:
                raise keys.KeysError("vnode metadata changed or unavailable")
            flags = struct.unpack_from("I", buffer.raw, 0)[0]
            mode = struct.unpack_from("H", buffer.raw, 28)[0]
            device = struct.unpack_from("I", buffer.raw, 140)[0]
            if flags & 1 and stat.S_ISCHR(mode) and device in devices:
                return True
        return False
    with os.scandir(f"/proc/{pid}/fd") as entries:
        for count, entry in enumerate(entries, 1):
            _time_check(deadline)
            if count > 512 or not entry.name.isdecimal():
                raise keys.KeysError("descriptor metadata exceeds bound")
            try:
                info = entry.stat()
                if not stat.S_ISCHR(info.st_mode) or info.st_rdev not in devices:
                    continue
                with open(
                    f"/proc/{pid}/fdinfo/{entry.name}", encoding="ascii"
                ) as metadata:
                    raw = metadata.read(4097)
                match = re.search(r"^flags:\s*([0-7]+)$", raw, re.M)
                if len(raw) > 4096 or not match:
                    raise keys.KeysError("terminal descriptor access unavailable")
                flags = int(match[1], 8)
                if not flags & getattr(os, "O_PATH", 0) and flags & os.O_ACCMODE in (
                    os.O_RDONLY,
                    os.O_RDWR,
                ):
                    return True
            except FileNotFoundError as exc:
                raise keys.KeysError("descriptor changed during verification") from exc
    return False


def _exclusive_consumer(
    owner: int, info: Process, ancestry: set[int], deadline: float
) -> None:
    seen_owner = False
    for pid, candidate in _group_members(info.group, deadline):
        _time_check(deadline)
        if pid == owner:
            seen_owner = (
                candidate.start == info.start and candidate.foreground == info.group
            )
        if (
            pid not in ancestry
            and candidate.tty == info.tty
            and _reads_tty(pid, info.tty, deadline)
        ):
            raise keys.KeysError(
                "another foreground terminal reader makes the owner ambiguous"
            )
    if not seen_owner:
        raise keys.KeysError("owner changed during foreground verification")


def _peer_uid(sock: socket.socket) -> int:
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None).getpeereid(
            sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)
        ):
            raise keys.KeysError("cannot authenticate Herdr peer")
        return uid.value
    if sys.platform.startswith("linux"):
        return struct.unpack(
            "iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[1]
    raise keys.KeysError("native terminal control needs Unix peer authentication")


def _owned_socket(endpoint: str) -> bool:
    info = os.stat(endpoint)
    return (
        os.path.isabs(endpoint)
        and stat.S_ISSOCK(info.st_mode)
        and info.st_uid == os.getuid()
    )


class Session:
    def __init__(self, endpoint: str, deadline: float, identity: str = ""):
        self.deadline, self.sequence, self.sock = deadline, 0, None
        try:
            if not _owned_socket(endpoint):
                raise keys.KeysError("Herdr endpoint is not an owned local socket")
            self.sock = socket.socket(socket.AF_UNIX)
            self._timeout()
            self.sock.connect(endpoint)
            if _peer_uid(self.sock) != os.getuid():
                raise keys.KeysError("Herdr peer belongs to another user")
            self.sock.sendall(
                b'{"jsonrpc":"2.0","id":0,"method":"hello",'
                b'"params":{"generation":1,"framed":true}}\n'
            )
            line = bytearray()
            while len(line) < 65536:
                line.extend(self.read(1))
                if line[-1:] == b"\n":
                    break
            reply = json.loads(line)
            if (
                not line.endswith(b"\n")
                or not isinstance(reply, dict)
                or reply.get("id") != 0
            ):
                raise keys.KeysError("invalid Herdr handshake")
            hello = reply.get("result")
            if not isinstance(hello, dict) or not isinstance(hello.get("core"), dict):
                raise keys.KeysError("invalid Herdr handshake")
            self.identity = str(hello["core"].get("id") or "")
            self.version = str(hello.get("version") or "")
            if (
                hello.get("generation") != 1
                or hello.get("framed") is not True
                or not self.identity
                or (identity and identity != self.identity)
            ):
                raise keys.KeysError("Herdr core identity or protocol changed")
        except (OSError, ValueError, struct.error, keys.KeysError) as exc:
            self.close()
            if isinstance(exc, keys.KeysError):
                raise
            raise keys.KeysError("Herdr connection unavailable or invalid") from exc

    def _timeout(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise keys.KeysError("native input deadline reached")
        self.sock.settimeout(min(2, left))

    def read(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            self._timeout()
            chunk = self.sock.recv(size - len(out))
            if not chunk:
                raise keys.KeysError("Herdr connection closed; delivery not confirmed")
            out.extend(chunk)
        return bytes(out)

    def frame(self, channel: int, kind: int, payload: bytes) -> None:
        self._timeout()
        self.sock.sendall(struct.pack("<IBI", channel, kind, len(payload)) + payload)

    def receive(self) -> tuple[int, int, bytes]:
        channel, kind, size = struct.unpack("<IBI", self.read(9))
        if size > MAX_BYTES:
            raise keys.KeysError("Herdr frame exceeds input-control bound")
        return channel, kind, self.read(size)

    def request(self, method: str, **params: Any) -> dict[str, Any]:
        self.sequence += 1
        self.frame(
            0,
            0,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.sequence,
                    "method": method,
                    "params": params,
                }
            ).encode(),
        )
        for _ in range(128):
            channel, kind, payload = self.receive()
            if channel == 0 and kind == 0:
                reply = json.loads(payload)
                if (
                    not isinstance(reply, dict)
                    or reply.get("id") != self.sequence
                    or not isinstance(reply.get("result"), dict)
                ):
                    raise keys.KeysError(
                        "Herdr refused the operation or returned an invalid response"
                    )
                return reply["result"]
            if channel and kind == 0:
                self.frame(channel, 1, struct.pack("<I", len(payload)))
        raise keys.KeysError("Herdr response exceeded frame bound")

    def paint(self, channel: int) -> bytes:
        data = bytearray()
        at = 0
        for _ in range(128):
            ch, kind, payload = self.receive()
            if ch != channel or kind != 0:
                raise keys.KeysError("incomplete Herdr initial paint")
            data.extend(payload)
            if len(data) > MAX_BYTES:
                raise keys.KeysError("Herdr paint exceeds input-control bound")
            self.frame(ch, 1, struct.pack("<I", len(payload)))
            # Decode strings before recognizing the renderer's post-paint marker;
            # a title containing lookalike bytes is not a completion boundary.
            try:
                text = data.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                if exc.end == len(data) and exc.reason == "unexpected end of data":
                    continue
                raise keys.KeysError("invalid UTF-8 in Herdr paint") from exc
            while at < len(text):
                if at % 256 == 0:
                    _time_check(self.deadline)
                if text.startswith(("\x1b]", "\x1bP", "\x1b_"), at):
                    end = re.search(r"\x07|\x1b\\", text[at + 2 :])
                    if not end:
                        break
                    at += 2 + end.end()
                else:
                    match = CSI.match(text, at)
                    if match:
                        if match[1] == "?9001" and match[3] in "hl":
                            if match[3] == "h":
                                raise keys.KeysError("win32 input mode is unsupported")
                            return text[:at].encode()
                        at = match.end()
                    else:
                        if text[at] == "\x1b" and (
                            at + 1 == len(text) or text.startswith("\x1b[", at)
                        ):
                            break
                        at += 1
        raise keys.KeysError("Herdr initial paint has no supported completion marker")

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


@dataclass
class Screen:
    styled: str
    plain: str
    paste: bool
    kitty: int
    application: bool
    status: str
    size: tuple[int, int]
    cursor: tuple[int, int]
    deadline: float = float("inf")

    @staticmethod
    def _rule(line: str) -> bool:
        return line.startswith(("─", "━", "═")) and bool(keys._RULE.fullmatch(line))

    def bounds(self) -> tuple[int, int] | None:
        budget = PaintBudget(self.deadline)
        rows = self.plain.splitlines()
        for index in range(min(self.cursor[1], len(rows) - 1), -1, -1):
            budget.spend(len(rows[index]) + 1)
            text = rows[index]
            glyph = next(
                (g for g in keys.PROMPT_GLYPHS.values() if text.startswith(g)), None
            )
            if glyph is None:
                continue
            if glyph != "›":
                if not index or not self._rule(rows[index - 1]):
                    continue
                width = len(rows[index - 1].rstrip())
                budget.spend(len(rows) - index)
                end = next(
                    (
                        i
                        for i in range(max(index, self.cursor[1]) + 1, len(rows))
                        if self._rule(rows[i]) and len(rows[i].rstrip()) == width
                    ),
                    None,
                )
                if end is not None:
                    return index, end
                continue
            # Codex has no bottom rule. Prefer its model/status row, followed
            # only by known footer rows. Some idle layouts omit the model row;
            # then the final nonblank shortcut row bounds the whole composer.
            # Pick the last footer, never a user-written lookalike before it.
            budget.spend(len(rows) - index)
            candidates = [
                i
                for i in range(max(index + 2, self.cursor[1] + 1), len(rows))
                if rows[i].startswith("  ")
                and " · " in rows[i]
                and not rows[i - 1].strip()
            ]
            if len(candidates) == 1:
                end = candidates[0]
                if all(
                    not row.strip()
                    or "for shortcuts" in row
                    or ("warning" in row and "f2 to view" in row)
                    for row in rows[end + 1 :]
                ) and (
                    text[len(glyph) :].strip()
                    or any(row.strip() for row in rows[index + 1 : end])
                ):
                    # Empty real composers render a suggestion; a bare wrapped
                    # literal glyph must not hide the earlier draft prefix.
                    return index, end
            nonblank = [i for i in range(len(rows)) if rows[i].strip()]
            if not nonblank:
                continue
            end = nonblank[-1]
            if (
                end >= max(index + 2, self.cursor[1] + 1)
                and not rows[end - 1].strip()
                and re.fullmatch(
                    r"  (?:← for agents · )?\? for shortcuts", rows[end].rstrip()
                )
                and (
                    text[len(glyph) :].strip()
                    or any(row.strip() for row in rows[index + 1 : end])
                )
            ):
                return index, end
        return None

    def prompt(self, agent: str) -> keys.Prompt:
        _time_check(self.deadline)
        bounds = self.bounds()
        if bounds is None:
            return keys.Prompt(False, "", False)
        index, end = bounds
        styled = self.styled.splitlines()
        parsed = [keys._plain_and_dim(row) for row in styled]
        _time_check(self.deadline)
        # Continuation glyphs and horizontal Markdown rules are user text.
        result = keys.read_prompt(
            "\n".join(styled[: index + 1] + ["footer " + row for row in styled[end:]]),
            agent,
        )
        text, mask, reverse = parsed[index]
        glyph = next(g for g in keys.PROMPT_GLYPHS.values() if text.startswith(g))
        start = len(glyph)
        first = (
            ""
            if keys._ghost_cursor_only(text[start:], mask[start:], reverse[start:])
            and not any(
                c in "│┃" and not dim for c, dim in zip(text[start:], mask[start:])
            )
            else "".join(
                c for c, dim in zip(text[start:], mask[start:]) if not dim
            ).strip()
        )
        # These canonical composer rows have no side-border decoration. Keep
        # literal user │/┃ characters, including an otherwise empty first row;
        # the tmux parser's decorative-border trimming must not hide a draft.
        continuation = [
            "".join(c for c, dim in zip(text, mask) if not dim).strip()
            for text, mask, _reverse in parsed[index + 1 : end]
        ]
        return keys.Prompt(
            result.shown and self.status not in ("blocked", "error"),
            "\n".join([first, *continuation]).strip(),
            result.working or self.status == "working",
        )

    def selection(self) -> bool:
        """Refuse highlighted composer spans; allow a single reverse cursor cell."""
        bounds = self.bounds()
        if not bounds:
            return False
        for y, row in enumerate(
            self.styled.splitlines()[bounds[0] : bounds[1]], bounds[0]
        ):
            text, _dim, reverse = keys._plain_and_dim(row)
            if any(mark and (x, y) != self.cursor for x, mark in enumerate(reverse)):
                return True
            _time_check(self.deadline)
        return False

    def relative_cursor(self) -> tuple[int, int] | None:
        bounds = self.bounds()
        return (self.cursor[0], self.cursor[1] - bounds[0]) if bounds else None

    def completion(self) -> tuple[str, ...]:
        bounds = self.bounds()
        if not bounds:
            return ()
        index, _ = bounds
        rows = self.plain.splitlines()
        if (
            index >= 2
            and rows[index - 2].strip() == "enter insert · esc close"
            and not rows[index - 1].strip()
        ):
            menu = []
            for row in reversed(rows[max(0, index - 12) : index - 2]):
                if not row.strip():
                    if menu:
                        break
                    continue
                if not re.match(r"(?:› |  )\S.*\[Skill\]", row):
                    break
                menu.insert(0, row)
            if any(row.startswith("› ") for row in menu):
                return tuple(menu)
        return ()


MAX_RENDER_WORK = 2 * MAX_BYTES


class PaintBudget:
    def __init__(self, deadline: float):
        self.deadline, self.left = deadline, MAX_RENDER_WORK

    def spend(self, amount: int = 1) -> None:
        self.left -= amount
        if self.left < 0 or time.monotonic() >= self.deadline:
            raise keys.KeysError("native paint work/deadline bound reached")


def render(
    raw: bytes, cols: int, rows: int, status: str = "", *, deadline: float | None = None
) -> Screen:
    """A bounded canonical first paint; not a general live terminal emulator."""
    if cols <= 0 or rows <= 0 or cols * rows > MAX_CELLS:
        raise keys.KeysError("unsupported Herdr grid")
    budget = PaintBudget(deadline if deadline is not None else time.monotonic() + 2)
    cells = cols * rows
    budget.spend(cells)
    blank = (" ", False, False)
    grid = [blank] * cells
    x = y = 0
    dim = reverse = paste = application = False
    kitty = 0
    text = raw.decode("utf-8", "strict")
    if len(raw) > MAX_BYTES:
        raise keys.KeysError("native paint exceeds input-control bound")
    at = 0
    while at < len(text):
        budget.spend()
        match = CSI.match(text, at)
        if match:
            params, intermediate, final = match.groups()
            if ":" in params or len(params) > 128:
                raise keys.KeysError("unsupported first-paint parameter grammar")
            budget.spend(len(params))
            numbers = [int(v or "0") for v in params.lstrip("?=>").split(";")]
            n = numbers[0] or 1
            if final == "m":
                i = 0
                while i < len(numbers):
                    code = numbers[i]
                    if code in (38, 48, 58) and i + 1 < len(numbers):
                        i += 4 if numbers[i + 1] == 2 else 2
                    elif code == 0:
                        dim = reverse = False
                    elif code == 22:
                        dim = False
                    elif code == 2:
                        dim = True
                    elif code == 7:
                        reverse = True
                    elif code == 27:
                        reverse = False
                    i += 1
            elif final in "Hf":
                y, x = n - 1, (numbers[1] or 1) - 1 if len(numbers) > 1 else 0
            elif final == "G":
                x = n - 1
            elif final in "ABCD":
                y += -n if final == "A" else n if final == "B" else 0
                x += n if final == "C" else -n if final == "D" else 0
            elif final in "JK":
                mode = numbers[0]
                if mode not in (0, 1, 2, 3):
                    raise keys.KeysError("unsupported paint erase")
                position = min(cells - 1, max(0, y * cols + x))
                if final == "K":
                    first, last = (
                        (position // cols) * cols,
                        (position // cols + 1) * cols,
                    )
                else:
                    first, last = 0, cells
                if mode == 0:
                    first = position
                elif mode == 1:
                    last = position + 1
                budget.spend(last - first)
                grid[first:last] = [blank] * (last - first)
            elif final in "hl" and params.startswith("?"):
                if n == 2004:
                    paste = final == "h"
                elif n == 1:
                    application = final == "h"
                elif n in (47, 1047, 1049) and final == "h":
                    budget.spend(cells)
                    grid = [blank] * cells
                    x = y = 0
            elif final == "u" and params.startswith("="):
                kitty = numbers[0]
            elif final == "p" and intermediate == "!":
                dim = reverse = False
            elif final not in ("g", "p", "r", "q", "h", "l"):
                raise keys.KeysError("unsupported first-paint control sequence")
            x, y = max(0, min(x, cols - 1)), max(0, min(y, rows - 1))
            at = match.end()
            continue
        if text.startswith(("\x1b]", "\x1bP", "\x1b_"), at):
            end = re.search(r"\x07|\x1b\\", text[at + 2 :])
            if not end:
                raise keys.KeysError("incomplete first-paint string")
            budget.spend(end.end())
            at += 2 + end.end()
            continue
        char = text[at]
        if char == "\x1b":
            if text[at : at + 2] == "\x1bH":
                at += 2
                continue
            if at + 2 < len(text) and text[at + 1] in "()":
                at += 3
                continue
            raise keys.KeysError("unsupported first-paint escape")
        if char == "\r":
            x = 0
        elif char == "\n":
            y += 1
        elif char == "\t":
            x = min(cols - 1, (x // 8 + 1) * 8)
        elif char == "\b":
            x = max(0, x - 1)
        elif char in "\x18\x07\x0e\x0f":
            pass
        elif ord(char) < 32:
            raise keys.KeysError("unsupported first-paint control")
        else:
            if x >= cols:
                x, y = 0, y + 1
            if y >= rows:
                raise keys.KeysError("first paint exceeds its grid")
            if char in ("\u200d", "\ufe0f"):
                raise keys.KeysError("unsupported complex first-paint grapheme")
            if unicodedata.combining(char):
                pos = y * cols + max(0, x - 1)
                previous, style, cursor_style = grid[pos]
                if len(previous) >= 64:
                    raise keys.KeysError("first-paint combining run exceeds bound")
                grid[pos] = (previous + char, style, cursor_style)
            else:
                grid[y * cols + x] = (char, dim, reverse)
                x += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        at += 1
    styled, plain = [], []
    for row in range(rows):
        budget.spend(cols)
        line, state = [], (False, False)
        chunk = grid[row * cols : (row + 1) * cols]
        for char, style, cursor_style in chunk:
            if state != (style, cursor_style):
                line.append(
                    "\x1b[0m"
                    + ("\x1b[2m" if style else "")
                    + ("\x1b[7m" if cursor_style else "")
                )
                state = style, cursor_style
            line.append(char)
        styled.append("".join(line) + "\x1b[0m")
        plain.append("".join(char for char, _, _ in chunk).rstrip())
    budget.spend()
    return Screen(
        "\n".join(styled),
        "\n".join(plain).rstrip(),
        paste,
        kitty,
        application,
        status,
        (cols, rows),
        (x, y),
    )


def capture_binding(owner: int) -> dict[str, Any] | None:
    endpoint, address = (
        os.environ.get("HC_TERMINAL_SOCKET", ""),
        os.environ.get("HC_TERMINAL_ID", ""),
    )
    if not endpoint or not ADDRESS.fullmatch(address):
        return None
    try:
        if process(owner).tty in (0, 0xFFFFFFFF):
            return None
        with Session(endpoint, time.monotonic() + 2) as session:
            terminal = session.request("terminal.get", terminal=address)
            agent, root, ancestry = _prove(owner, terminal)
            _exclusive_consumer(owner, agent, ancestry, session.deadline)
            if terminal.get("state") != "live" or terminal.get("address") != address:
                return None
            return {
                "socket": endpoint,
                "core": session.identity,
                "address": address,
                "pid": terminal["pid"],
                "created_ms": terminal["created_ms"],
                "root_start": list(root.start),
                "owner_pid": owner,
                "owner_start": list(agent.start),
            }
    except (keys.KeysError, OSError, ValueError, KeyError, TypeError):
        return None


def encode(name: str, screen: Screen) -> bytes:
    if screen.kitty & ~7:
        raise keys.KeysError("unsupported keyboard enhancement mode")
    name = KEY_ALIASES.get(name, name)
    controls = {
        "Enter": b"\r",
        "Escape": b"\x1b",
        "Tab": b"\t",
        "BSpace": b"\x7f",
        "Space": b" ",
        "BTab": b"\x1b[Z",
        "Home": b"\x1b[H",
        "End": b"\x1b[F",
        "DC": b"\x1b[3~",
        "IC": b"\x1b[2~",
        "PPage": b"\x1b[5~",
        "NPage": b"\x1b[6~",
    }
    if name == "Escape" and screen.kitty & 1:
        return b"\x1b[27u"
    if name in controls:
        return controls[name]
    if name in ("Up", "Down", "Right", "Left"):
        return (
            "\x1b"
            + ("O" if screen.application else "[")
            + {"Up": "A", "Down": "B", "Right": "C", "Left": "D"}[name]
        ).encode()
    if re.fullmatch(r"C-[a-zA-Z]", name):
        char = name[-1].lower()
        return (
            f"\x1b[{ord(char)};5u".encode()
            if screen.kitty & 1
            else bytes([ord(char) - 96])
        )
    if re.fullmatch(r"M-[a-zA-Z]", name):
        char = name[-1]
        return (
            f"\x1b[{ord(char)};3u".encode()
            if screen.kitty & 1
            else b"\x1b" + char.encode()
        )
    if name in ("F1", "F2", "F3", "F4"):
        return b"\x1bO" + bytes([ord("P") + int(name[1:]) - 1])
    if re.fullmatch(r"F(?:[5-9]|1[0-2])", name):
        return f"\x1b[{[15, 17, 18, 19, 20, 21, 23, 24][int(name[1:]) - 5]}~".encode()
    raise keys.KeysError(
        f"unsupported native key {name!r}; nothing from this operation was typed"
    )


def _rendered_text(text: str) -> str:
    # Composer wrapping inserts rows and indentation. This is correlation of
    # rendered characters; the actual paste bytes retain all original whitespace.
    return "".join(text.split())


class Tmux:
    backend = "tmux"
    possibly_sent = False

    def __init__(self, pane: keys.Pane):
        self.id, self.session = pane.id, pane.session

    def prompt(self, agent: str) -> keys.Prompt:
        return keys.prompt(self.id, agent)

    def prepare(self, body: str, options: Any) -> None:
        pass

    def type_text(self, agent: str, text: str, *, submit: bool) -> bool:
        return keys.type_text(self.id, agent, text, submit=submit)

    def send_keys(self, names: tuple[str, ...]) -> None:
        keys.send_keys(self.id, names)


class Herdr:
    backend = "herdr"

    def __init__(
        self,
        member: dict[str, Any],
        binding: dict[str, Any],
        current: Callable[[], dict[str, Any]],
        deadline: float | None = None,
    ):
        self.binding, self.current, self.owner = (
            copy.deepcopy(binding),
            current,
            int(member["owner_pid"]),
        )
        self.stamp = (
            member.get("owner_pid"),
            member.get("provider"),
            copy.deepcopy(member.get("terminal_control")),
        )
        if binding.get("owner_pid") != self.owner or not ADDRESS.fullmatch(
            str(binding.get("address") or "")
        ):
            raise keys.KeysError("native binding does not name this local owner")
        self.id, self.session = (
            "herdr/" + binding["address"],
            "Herdr " + binding["address"],
        )
        self.deadline, self.possibly_sent, self.anytime = (
            deadline if deadline is not None else time.monotonic() + OP_SECONDS,
            False,
            False,
        )
        self.last: Screen | None = None

    def _check(self, terminal: dict[str, Any], deadline: float | None = None) -> None:
        if not isinstance(terminal, dict):
            raise keys.KeysError("invalid native terminal record")
        fresh = self.current()
        if fresh.get("closed_at") or self.stamp != (
            fresh.get("owner_pid"),
            fresh.get("provider"),
            fresh.get("terminal_control"),
        ):
            raise keys.KeysError(
                "member ownership changed during input; operation stopped"
            )
        if (
            any(terminal.get(k) != self.binding[k] for k in ("pid", "created_ms"))
            or terminal.get("state") != "live"
        ):
            raise keys.KeysError("native terminal incarnation changed")
        agent, root, ancestry = _prove(self.owner, terminal)
        _exclusive_consumer(self.owner, agent, ancestry, deadline or self.deadline)
        if (
            list(agent.start) != self.binding["owner_start"]
            or list(root.start) != self.binding["root_start"]
        ):
            raise keys.KeysError("native process incarnation changed")

    def _open(self, *, write: bool = False) -> tuple[Session, int, Screen]:
        deadline = min(self.deadline, time.monotonic() + 2)
        session = Session(self.binding["socket"], deadline, self.binding["core"])
        try:
            terminal = session.request("terminal.get", terminal=self.binding["address"])
            self._check(terminal, deadline)
            cols, rows = int(terminal["cols"]), int(terminal["rows"])
            if cols <= 0 or rows <= 0 or cols * rows > MAX_CELLS:
                raise keys.KeysError("unsupported Herdr grid")
            attached = session.request(
                "terminal.attach",
                terminal=self.binding["address"],
                cols=cols,
                rows=rows,
                history=0,
                observe=not write,
                resumed=True,
            )
            actual = attached["terminal"]
            self._check(actual, deadline)
            if (actual["cols"], actual["rows"]) != (cols, rows):
                raise keys.KeysError("terminal resized during attachment")
            channel = int(attached["channel"])
            if not 0 < channel <= 0xFFFFFFFF:
                raise keys.KeysError("invalid native stream channel")
            status = actual.get("status") or {}
            if not isinstance(status, dict):
                raise keys.KeysError("invalid native terminal status")
            screen = render(
                session.paint(channel),
                cols,
                rows,
                str(status.get("state") or ""),
                deadline=deadline,
            )
            screen.deadline = self.deadline
            return session, channel, screen
        except BaseException:
            session.close()
            raise

    def snapshot(self) -> Screen:
        session, _, screen = self._open()
        session.close()
        self.last = screen
        return screen

    def prompt(self, agent: str) -> keys.Prompt:
        return self.snapshot().prompt(agent)

    def _eligible(self, screen: Screen, agent: str) -> keys.Prompt:
        prompt = screen.prompt(agent)
        if not prompt.shown or screen.status in ("blocked", "error"):
            raise keys.KeysError(
                "no recognized unblocked composer; dialog input refused"
            )
        if screen.selection():
            raise keys.KeysError("highlighted composer selection is unsupported")
        if prompt.working and not self.anytime:
            raise keys.KeysError("a turn started before input")
        return prompt

    def prepare(self, body: str, options: Any) -> None:
        self.anytime = options.anytime
        screen = self.last or self.snapshot()
        self._eligible(screen, str(self.stamp[1]))
        if len(body.encode()) > core.MAX_MESSAGE_BYTES:
            raise keys.KeysError("native input exceeds the message bound")
        for name in (
            *options.keys,
            *(("Enter",) if options.submit and not options.keys else ()),
        ):
            encode(name, screen)
        if any(ord(c) < 32 and c not in "\n\t" for c in body) or "\x7f" in body:
            raise keys.KeysError("native text contains unsupported control characters")
        if "\n" in body and not screen.paste:
            raise keys.KeysError("multiline input requires bracketed paste mode")

    def _write(self, data: bytes, agent: str, expected: Screen) -> None:
        session, channel, fresh = self._open(write=True)
        try:
            prompt, previous = self._eligible(fresh, agent), expected.prompt(agent)
            if (
                prompt.typed != previous.typed
                or fresh.relative_cursor() != expected.relative_cursor()
                or (not self.anytime and prompt.working != previous.working)
            ):
                raise keys.KeysError("composer changed before input")
            if (fresh.paste, fresh.kitty, fresh.application) != (
                expected.paste,
                expected.kitty,
                expected.application,
            ):
                raise keys.KeysError("keyboard modes changed before input")
            actual = session.request("terminal.get", terminal=self.binding["address"])
            self._check(actual, session.deadline)
            state = actual.get("status") or {}
            if not isinstance(state, dict) or state.get("state") in (
                "blocked",
                "error",
            ):
                raise keys.KeysError("dialog input refused")
            if state.get("state") == "working" and not self.anytime:
                raise keys.KeysError("a turn started before input")
            if (actual["cols"], actual["rows"]) != fresh.size:
                raise keys.KeysError("terminal resized before input")
            self.possibly_sent = True
            session.frame(channel, 0, data)
            self._check(
                session.request("terminal.get", terminal=self.binding["address"]),
                session.deadline,
            )
        finally:
            session.close()

    def _pause(self, seconds: float) -> None:
        if time.monotonic() + seconds > self.deadline:
            raise keys.KeysError(
                "native input deadline reached; input may still arrive"
            )
        keys.sleep(seconds)

    def _observe(
        self, agent: str, confirmed: Callable[[Screen], bool], *, idle: bool = False
    ) -> Screen:
        """Read a partial repaint again; never retry any input or its target."""
        for _ in range(20):
            _time_check(self.deadline)
            after = self.snapshot()  # Each sample closes; same 12s owner fence.
            prompt = after.prompt(agent)
            if after.status in ("blocked", "error"):
                raise keys.KeysError("dialog input refused")
            if (
                idle
                and not self.anytime
                and (prompt.working or after.status == "working")
            ):
                raise keys.KeysError("a turn started before input confirmation")
            if confirmed(after):
                return after
            self._pause(0.1)
        raise keys.KeysError(
            "input effect unconfirmed; input may still arrive, do not resend"
        )

    @staticmethod
    def _inserted(
        old: str, new: str, text: str, *, deadline: float = float("inf")
    ) -> bool:
        _time_check(deadline)
        old, new, text = _rendered_text(old), _rendered_text(new), _rendered_text(text)
        if not text or len(new) != len(old) + len(text):
            return False
        prefix = suffix = 0
        while prefix < len(old) and old[prefix] == new[prefix]:
            if prefix % 256 == 0:
                _time_check(deadline)
            prefix += 1
        while suffix < len(old) and old[-1 - suffix] == new[-1 - suffix]:
            if suffix % 256 == 0:
                _time_check(deadline)
            suffix += 1
        first, last = max(0, len(old) - suffix), prefix
        found = first <= last and new.find(text, first, last + len(text)) >= 0
        _time_check(deadline)
        return found

    @staticmethod
    def _submitted(before: Screen, after: Screen, intended: str, agent: str) -> bool:
        previous, current = before.prompt(agent), after.prompt(agent)
        if not current.shown or current.typed or not intended:
            return False
        # Completion menus can already contain the whole intended command.
        # Submission needs a new user echo, including its provider prompt glyph.
        needle = _rendered_text(keys.PROMPT_GLYPHS.get(agent, "") + intended)

        def transcript(screen):
            bounds = screen.bounds()
            if not bounds:
                return ""
            glyph = keys.PROMPT_GLYPHS.get(agent, "")
            # A completion entry can have an indented prompt-like arrow. Only
            # a column-zero user echo starts a candidate; preserve row markers
            # while allowing its continuation to wrap across following rows.
            return "".join(
                ("\x00" if row.startswith(glyph) else "") + _rendered_text(row)
                for row in screen.plain.splitlines()[: bounds[0]]
            )

        fresh_text = transcript(after).count("\x00" + needle) > transcript(
            before
        ).count("\x00" + needle)
        _time_check(after.deadline)
        return fresh_text or (current.working and not previous.working)

    def type_text(self, agent: str, text: str, *, submit: bool) -> bool:
        before = self.last or self.snapshot()
        previous = self._eligible(before, agent)
        payload = text.encode()
        if before.paste:
            payload = b"\x1b[200~" + payload + b"\x1b[201~"
        self._write(payload, agent, before)
        self._pause(keys.SUBMIT_PAUSE_SECONDS)
        inserted = self._observe(
            agent,
            lambda screen: screen.prompt(agent).shown
            and self._inserted(
                previous.typed, screen.prompt(agent).typed, text, deadline=self.deadline
            ),
            idle=True,
        )
        draft = self._eligible(inserted, agent).typed
        if not self._inserted(previous.typed, draft, text, deadline=self.deadline):
            raise keys.KeysError(
                "fresh composer insertion unconfirmed; input may still arrive, do not resend"
            )
        if not submit:
            return True
        if agent == "codex" and inserted.completion():
            self._write(encode("Enter", inserted), agent, inserted)
            self._pause(keys.SUBMIT_PAUSE_SECONDS)
            selected = self._observe(
                agent,
                lambda screen: screen.prompt(agent).shown
                and _rendered_text(screen.prompt(agent).typed) == _rendered_text(draft)
                and not screen.completion(),
                idle=True,
            )
            prompt = self._eligible(selected, agent)
            if (
                _rendered_text(prompt.typed) != _rendered_text(draft)
                or selected.completion()
            ):
                raise keys.KeysError(
                    "skill completion selection unconfirmed; no submission retry"
                )
            inserted = selected
        self._write(encode("Enter", inserted), agent, inserted)
        self._pause(keys.SUBMIT_CHECK_SECONDS)
        after = self._observe(
            agent, lambda screen: self._submitted(inserted, screen, draft, agent)
        )
        if self._submitted(inserted, after, draft, agent):
            return True
        raise keys.KeysError(
            "submission unconfirmed; input may still arrive, do not resend"
        )

    def _key_effect(self, name: str, before: Screen, after: Screen, agent: str) -> bool:
        previous, current = before.prompt(agent), after.prompt(agent)
        if not current.shown or before.size != after.size:
            return False
        name = KEY_ALIASES.get(name, name)
        if name == "Enter":
            selected = (
                before.completion()
                and not after.completion()
                and previous.typed == current.typed
            )
            return bool(
                selected or self._submitted(before, after, previous.typed, agent)
            )
        left, right = before.relative_cursor(), after.relative_cursor()
        old = left[1] * before.size[0] + left[0] if left else None
        new = right[1] * after.size[0] + right[0] if right else None
        same_text = previous.typed == current.typed
        direction = {
            "C-a": "Home",
            "C-e": "End",
            "C-b": "Left",
            "C-f": "Right",
            "C-p": "Up",
            "C-n": "Down",
            "M-b": "Left",
            "M-f": "Right",
        }.get(name, name)
        if direction in ("Left", "Home"):
            return same_text and old is not None and new is not None and new < old
        if direction in ("Right", "End"):
            return same_text and old is not None and new is not None and new > old
        if direction in ("Up", "Down", "Tab", "BTab"):
            return (
                previous.typed != current.typed
                or (
                    before.completion() != after.completion()
                    and bool(before.completion() or after.completion())
                )
                or (same_text and old is not None and new is not None and new != old)
            )
        if name in ("BSpace", "DC", "C-d", "C-h", "C-u", "C-k"):
            return len(current.typed) < len(previous.typed) or (
                same_text and old is not None and new is not None and new < old
            )
        if name in ("Escape", "C-c"):
            cleared = bool(previous.typed) and not current.typed
            dismissed = bool(before.completion()) and not after.completion()
            # Turn completion and prose (even fresh cancellation words) cannot
            # acknowledge an interrupt. Without a composer/menu effect, stop
            # uncertain after this possible key and send no remainder.
            return cleared or dismissed
        return False

    def send_keys(self, names: tuple[str, ...]) -> None:
        for name in names:
            agent = str(self.stamp[1])
            before = self.last or self.snapshot()
            self._write(encode(name, before), agent, before)
            self._pause(
                keys.SUBMIT_CHECK_SECONDS
                if KEY_ALIASES.get(name, name) == "Enter"
                else 0.1
            )
            self._observe(
                agent, lambda screen: self._key_effect(name, before, screen, agent)
            )


def resolve(
    member: dict[str, Any], current: Callable[[], dict[str, Any]]
) -> Tmux | Herdr | None:
    deadline = time.monotonic() + OP_SECONDS
    owner = int(member.get("owner_pid") or 0)
    if owner <= 1:
        return None
    binding = member.get("terminal_control")
    if isinstance(binding, dict) and binding:
        try:
            # A proved direct native TTY cannot also be an inner tmux pane.
            # Avoid the legacy tmux subprocess discovery before native deadlines.
            if process(owner).tty == process(int(binding["pid"])).tty:
                control = Herdr(member, binding, current, deadline)
                with Session(
                    binding["socket"],
                    min(deadline, time.monotonic() + 2),
                    binding["core"],
                ) as session:
                    control._check(
                        session.request("terminal.get", terminal=binding["address"]),
                        session.deadline,
                    )
                return control
        except (keys.KeysError, OSError, ValueError, KeyError, TypeError):
            return None
    pane = keys.find_pane(owner)
    if pane is not None:
        try:
            same_tty = process(owner).tty == process(pane.pid).tty
        except keys.KeysError:
            same_tty = True  # Existing tmux remains supported on other hosts.
        if same_tty:
            return Tmux(pane)
    return None
