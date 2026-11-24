"""Herdr generation-1 observe streams and local foreground verification.

Adapted from Agent Orchestra's terminal_control.py. Plugins are distributed
independently. Only the visible initial paint is retained for the caller.
"""

from __future__ import annotations

import ctypes
import ctypes.util
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
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import screen

MAX_BYTES = 1024 * 1024
MAX_CELLS = 65536
CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")


class NativeError(Exception):
    """The native target or its current state could not be verified."""


@lru_cache(maxsize=1)
def _libc():
    library = ctypes.CDLL(None, use_errno=True)
    if not hasattr(library, "proc_pidinfo"):
        library = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)
    return library


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    tty: int
    foreground: int
    start: tuple[int, ...]


class _ZombieProcess(NativeError):
    """Conclusive kernel exit state; unlike unavailable live process metadata."""


def process(pid: int) -> Process:
    """An OS process incarnation, without reading environment or credentials."""
    try:
        if sys.platform == "darwin":
            buffer = ctypes.create_string_buffer(136)
            size = _libc().proc_pidinfo(pid, 3, ctypes.c_uint64(0), buffer, 136)
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
    raise NativeError("cannot verify native terminal process identity")


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
        raise NativeError(
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
    raise NativeError("owner is not inside the recorded terminal")


def _time_check(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise NativeError("native input deadline reached")


def _group_members(group: int, deadline: float) -> list[tuple[int, Process]]:
    """Bounded numeric process metadata only; never arguments or environment."""
    members = []
    if sys.platform == "darwin":
        buffer = (ctypes.c_int * 129)()
        size = _libc().proc_listpids(2, group, buffer, ctypes.sizeof(buffer))
        if size < 0 or size >= ctypes.sizeof(buffer) or size % 4:
            raise NativeError("foreground process-group enumeration unavailable")
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
                    raise NativeError("process discovery exceeds bound")
                try:
                    if entry.stat(follow_symlinks=False).st_uid == os.getuid():
                        pids.append(int(entry.name))
                except FileNotFoundError:
                    continue
    else:
        raise NativeError("native process groups unsupported")
    for pid in pids:
        _time_check(deadline)
        try:
            info = process(pid)
        except _ZombieProcess:
            # A zombie cannot read a terminal; its parent may not have reaped
            # it yet. Unknown metadata for a live process still refuses below.
            continue
        except NativeError:
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
                raise NativeError("foreground process group exceeds bound")
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
        count = _libc().proc_pidinfo(pid, 1, ctypes.c_uint64(0), listing, 4096)
        if count <= 0 or count >= 4096 or count % 8:
            raise NativeError("descriptor metadata unavailable or exceeds bound")
        for fd, kind in struct.iter_unpack("iI", listing.raw[:count]):
            _time_check(deadline)
            if kind != 1:  # Pipes, sockets and closed descriptors cannot be TTYs.
                continue
            buffer = ctypes.create_string_buffer(176)
            if _libc().proc_pidfdinfo(pid, fd, 1, buffer, 176) != 176:
                raise NativeError("vnode metadata changed or unavailable")
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
                raise NativeError("descriptor metadata exceeds bound")
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
                    raise NativeError("terminal descriptor access unavailable")
                flags = int(match[1], 8)
                if not flags & getattr(os, "O_PATH", 0) and flags & os.O_ACCMODE in (
                    os.O_RDONLY,
                    os.O_RDWR,
                ):
                    return True
            except FileNotFoundError as exc:
                raise NativeError("descriptor changed during verification") from exc
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
            raise NativeError(
                "another foreground terminal reader makes the owner ambiguous"
            )
    if not seen_owner:
        raise NativeError("owner changed during foreground verification")


def _peer_uid(sock: socket.socket) -> int:
    if sys.platform == "darwin":
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if ctypes.CDLL(None).getpeereid(
            sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)
        ):
            raise NativeError("cannot authenticate Herdr peer")
        return uid.value
    if sys.platform.startswith("linux"):
        return struct.unpack(
            "iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )[1]
    raise NativeError("native terminal control needs Unix peer authentication")


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
                raise NativeError("Herdr endpoint is not an owned local socket")
            self.sock = socket.socket(socket.AF_UNIX)
            self._timeout()
            self.sock.connect(endpoint)
            if _peer_uid(self.sock) != os.getuid():
                raise NativeError("Herdr peer belongs to another user")
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
                raise NativeError("invalid Herdr handshake")
            hello = reply.get("result")
            if not isinstance(hello, dict) or not isinstance(hello.get("core"), dict):
                raise NativeError("invalid Herdr handshake")
            self.identity = str(hello["core"].get("id") or "")
            self.version = str(hello.get("version") or "")
            if (
                hello.get("generation") != 1
                or hello.get("framed") is not True
                or not self.identity
                or (identity and identity != self.identity)
            ):
                raise NativeError("Herdr core identity or protocol changed")
        except (OSError, ValueError, struct.error, NativeError) as exc:
            self.close()
            if isinstance(exc, NativeError):
                raise
            raise NativeError("Herdr connection unavailable or invalid") from exc

    def _timeout(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise NativeError("native input deadline reached")
        self.sock.settimeout(min(2, left))

    def read(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            self._timeout()
            chunk = self.sock.recv(size - len(out))
            if not chunk:
                raise NativeError("Herdr connection closed; delivery not confirmed")
            out.extend(chunk)
        return bytes(out)

    def frame(self, channel: int, kind: int, payload: bytes) -> None:
        self._timeout()
        self.sock.sendall(struct.pack("<IBI", channel, kind, len(payload)) + payload)

    def receive(self) -> tuple[int, int, bytes]:
        channel, kind, size = struct.unpack("<IBI", self.read(9))
        if size > MAX_BYTES:
            raise NativeError("Herdr frame exceeds input-control bound")
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
                    raise NativeError(
                        "Herdr refused the operation or returned an invalid response"
                    )
                return reply["result"]
            if channel and kind == 0:
                self.frame(channel, 1, struct.pack("<I", len(payload)))
        raise NativeError("Herdr response exceeded frame bound")

    def paint(self, channel: int) -> bytes:
        data = bytearray()
        at = 0
        for _ in range(128):
            ch, kind, payload = self.receive()
            if ch != channel or kind != 0:
                raise NativeError("incomplete Herdr initial paint")
            data.extend(payload)
            if len(data) > MAX_BYTES:
                raise NativeError("Herdr paint exceeds input-control bound")
            self.frame(ch, 1, struct.pack("<I", len(payload)))
            # Decode strings before recognizing the renderer's post-paint marker;
            # a title containing lookalike bytes is not a completion boundary.
            try:
                text = data.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                if exc.end == len(data) and exc.reason == "unexpected end of data":
                    continue
                raise NativeError("invalid UTF-8 in Herdr paint") from exc
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
                                raise NativeError("win32 input mode is unsupported")
                            return text[:at].encode()
                        at = match.end()
                    else:
                        if text[at] == "\x1b" and (
                            at + 1 == len(text) or text.startswith("\x1b[", at)
                        ):
                            break
                        at += 1
        raise NativeError("Herdr initial paint has no supported completion marker")

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
        return line.startswith(("─", "━", "═")) and bool(screen._RULE.fullmatch(line))

    def bounds(self) -> tuple[int, int] | None:
        budget = PaintBudget(self.deadline)
        rows = self.plain.splitlines()
        for index in range(min(self.cursor[1], len(rows) - 1), -1, -1):
            budget.spend(len(rows[index]) + 1)
            text = rows[index]
            glyph = next(
                (g for g in screen.PROMPT_GLYPHS.values() if text.startswith(g)), None
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


MAX_RENDER_WORK = 2 * MAX_BYTES


class PaintBudget:
    def __init__(self, deadline: float):
        self.deadline, self.left = deadline, MAX_RENDER_WORK

    def spend(self, amount: int = 1) -> None:
        self.left -= amount
        if self.left < 0 or time.monotonic() >= self.deadline:
            raise NativeError("native paint work/deadline bound reached")


def _cell(text: str, start: int) -> tuple[str, int]:
    """Consume one painted base and its modifiers without crossing VT controls."""
    base = text[start]
    width = 2 if unicodedata.east_asian_width(base) in ("W", "F") else 1
    end = start + 1
    if ("🇦" <= base <= "🇿" and end < len(text) and "🇦" <= text[end] <= "🇿"):
        end += 1
        width = 2
    while end < len(text):
        char = text[end]
        if char == "\ufe0f" or char == "\u20e3" or "🏻" <= char <= "🏿":
            width = 2
        elif char == "\u200d":
            if (end + 1 >= len(text) or unicodedata.category(text[end+1])[0] not in "SL"):
                break
            end += 1
            width = max(width, 2 if unicodedata.east_asian_width(text[end]) in ("W", "F") else 1)
        elif unicodedata.category(char) not in ("Mn", "Me") and not "\U000e0020" <= char <= "\U000e007f":
            break
        end += 1
        if end - start > 64:
            raise NativeError("first-paint grapheme exceeds bound")
    return text[start:end], width


def render(
    raw: bytes, cols: int, rows: int, status: str = "", *, deadline: float | None = None
) -> Screen:
    """A bounded canonical first paint; not a general live terminal emulator."""
    if cols <= 0 or rows <= 0 or cols * rows > MAX_CELLS:
        raise NativeError("unsupported Herdr grid")
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
        raise NativeError("native paint exceeds input-control bound")
    at = 0
    while at < len(text):
        budget.spend()
        match = CSI.match(text, at)
        if match:
            params, intermediate, final = match.groups()
            if ":" in params or len(params) > 128:
                raise NativeError("unsupported first-paint parameter grammar")
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
                    raise NativeError("unsupported paint erase")
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
                raise NativeError("unsupported first-paint control sequence")
            x, y = max(0, min(x, cols - 1)), max(0, min(y, rows - 1))
            at = match.end()
            continue
        if text.startswith(("\x1b]", "\x1bP", "\x1b_"), at):
            end = re.search(r"\x07|\x1b\\", text[at + 2 :])
            if not end:
                raise NativeError("incomplete first-paint string")
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
            raise NativeError("unsupported first-paint escape")
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
            raise NativeError("unsupported first-paint control")
        else:
            if x >= cols:
                x, y = 0, y + 1
            if y >= rows:
                raise NativeError("first paint exceeds its grid")
            if char in ("\u200d", "\ufe0e", "\ufe0f") or unicodedata.combining(char):
                pos = y * cols + max(0, x - 1)
                previous, style, cursor_style = grid[pos]
                if len(previous) >= 64:
                    raise NativeError("first-paint combining run exceeds bound")
                grid[pos] = (previous + char, style, cursor_style)
            else:
                cluster, width = _cell(text, at)
                pos = y * cols + x
                grid[pos] = (cluster, dim, reverse)
                if width == 2 and x + 1 < cols:
                    grid[pos + 1] = ("", dim, reverse)
                x += width
                at += len(cluster) - 1
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
