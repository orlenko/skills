"""Local interactive input. tmux keeps its existing path; Herdr uses DATA.

Herdr's send/keys RPCs deliberately wait on human input. They cannot implement
our checked-composer operation. Writable DATA has no delivery acknowledgement
and can itself be held: only fresh composer evidence confirms an effect. Never
retry, discover a successor, or finish a partial operation after uncertainty.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import stat
import struct
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


@dataclass(frozen=True)
class Process:
    parent: int
    group: int
    tty: int
    foreground: int
    start: tuple[int, ...]


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
                raise ValueError()
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
                raise ValueError()
            return Process(*(int(fields[i]) for i in (1, 2, 4, 5)), (int(fields[19]),))
    except (OSError, ValueError, IndexError, AttributeError, struct.error):
        pass
    raise keys.KeysError("cannot verify native terminal process identity")


def _prove(owner: int, terminal: dict[str, Any]) -> tuple[Process, Process]:
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
    for _ in range(32):
        if current == root:
            return agent, shell
        current = process(current).parent
        if current <= 1:
            break
    raise keys.KeysError("owner is not inside the recorded terminal")


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
            at = 0
            while at < len(text):
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

    def prompt(self, agent: str) -> keys.Prompt:
        styled_rows = self.styled.splitlines()
        rows = [keys._plain_and_dim(row) for row in styled_rows]
        glyphs = tuple(keys.PROMPT_GLYPHS.values())
        index = None
        for i in range(len(rows) - 1, -1, -1):
            head = rows[i][0].lstrip(" │┃")
            glyph = next((g for g in glyphs if head.startswith(g)), None)
            if glyph == "›" and not rows[i][0].startswith("›"):
                continue  # An indented continuation glyph is text, not a prompt.
            if glyph in keys._RULED_GLYPHS and not (
                i and keys._RULE.fullmatch(rows[i - 1][0])
            ):
                continue
            if glyph:
                index = i
                break
        if index is None:
            return keys.Prompt(False, "", False)
        end = next(
            (
                i
                for i in range(index + 1, len(rows))
                if rows[i][0].strip() and keys._RULE.fullmatch(rows[i][0])
            ),
            None,
        )
        if end is None:  # Codex: model/status row and shortcut row below the composer.
            footer = next(
                (
                    i
                    for i in range(index + 1, len(rows))
                    if "for shortcuts" in rows[i][0]
                ),
                None,
            )
            if footer is None:
                # Codex can hide shortcuts during completion and selection.
                # Its model/status row remains below the real input cursor.
                candidates = [
                    i
                    for i in range(index + 2, len(rows))
                    if i > self.cursor[1]
                    and " · " in rows[i][0]
                    and not rows[i - 1][0].strip()
                ]
                if not rows[index][0].startswith("›") or len(candidates) != 1:
                    return keys.Prompt(False, "", False)
                end = candidates[0]
                for row, _ in rows[end + 1 :]:
                    if row.strip() and not ("warning" in row and "f2 to view" in row):
                        return keys.Prompt(False, "", False)
            else:
                end = footer
                if footer > index + 1 and rows[footer - 1][0].strip():
                    end = footer - 1
        # Continuation text must not introduce another apparent prompt glyph.
        result = keys.read_prompt(
            "\n".join(
                styled_rows[: index + 1]
                + ["footer " + row for row in styled_rows[end:]]
            ),
            agent,
        )
        continuation = [
            "".join(c for c, dim in zip(text, mask) if not dim).strip(" │┃")
            for text, mask in rows[index + 1 : end]
        ]
        typed = "\n".join([result.typed, *continuation]).strip()
        blocked = self.status in ("blocked", "error")
        return keys.Prompt(
            result.shown and not blocked,
            typed,
            result.working or self.status == "working",
        )


def render(raw: bytes, cols: int, rows: int, status: str = "") -> Screen:
    """Decode only a fresh, bounded Ghostty first paint, never a live stream."""
    if cols <= 0 or rows <= 0 or cols * rows > MAX_CELLS:
        raise keys.KeysError("unsupported Herdr grid")
    grid = [[(" ", False) for _ in range(cols)] for _ in range(rows)]
    x = y = 0
    dim = paste = application = False
    kitty = 0
    text = raw.decode("utf-8", "strict")
    at = 0
    while at < len(text):
        match = CSI.match(text, at)
        if match:
            params, intermediate, final = match.groups()
            if ":" in params:
                raise keys.KeysError("unsupported first-paint parameter grammar")
            numbers = [int(v or "0") for v in params.lstrip("?=>").split(";")]
            n = numbers[0] or 1
            if final == "m":
                i = 0
                while i < len(numbers):
                    code = numbers[i]
                    if code in (38, 48, 58) and i + 1 < len(numbers):
                        i += 4 if numbers[i + 1] == 2 else 2
                    elif code in (0, 22):
                        dim = False
                    elif code == 2:
                        dim = True
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
                for row in range(rows):
                    for col in range(cols):
                        before = row * cols + col <= y * cols + x
                        if (final == "J" or row == y) and (
                            mode in (2, 3) or before == (mode == 1)
                        ):
                            grid[row][col] = (" ", False)
            elif final in "hl" and params.startswith("?"):
                if n == 2004:
                    paste = final == "h"
                elif n == 1:
                    application = final == "h"
                elif n in (47, 1047, 1049) and final == "h":
                    grid = [[(" ", False) for _ in range(cols)] for _ in range(rows)]
                    x = y = 0
            elif final == "u" and params.startswith("="):
                kitty = numbers[0]
            elif final == "p" and intermediate == "!":
                dim = False
            elif final not in ("g", "p", "r", "q", "h", "l"):
                raise keys.KeysError("unsupported first-paint control sequence")
            x, y = max(0, min(x, cols - 1)), max(0, min(y, rows - 1))
            at = match.end()
            continue
        if text.startswith(("\x1b]", "\x1bP", "\x1b_"), at):
            end = re.search(r"\x07|\x1b\\", text[at + 2 :])
            if not end:
                raise keys.KeysError("incomplete first-paint string")
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
            if unicodedata.combining(char):
                previous, style = grid[y][max(0, x - 1)]
                grid[y][max(0, x - 1)] = (previous + char, style)
            else:
                grid[y][x] = (char, dim)
                x += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        at += 1
    styled, plain = [], []
    for row in grid:
        line, state = [], False
        for char, style in row:
            if state != style:
                line.append("\x1b[2m" if style else "\x1b[22m")
                state = style
            line.append(char)
        styled.append("".join(line) + "\x1b[0m")
        plain.append("".join(char for char, _ in row).rstrip())
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
            agent, root = _prove(owner, terminal)
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
    name = {
        "Return": "Enter",
        "Esc": "Escape",
        "BackSpace": "BSpace",
        "Delete": "DC",
        "PageUp": "PPage",
        "PageDown": "NPage",
        "BackTab": "BTab",
    }.get(name, name)
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
    ):
        self.binding, self.current, self.owner = (
            binding,
            current,
            int(member["owner_pid"]),
        )
        self.stamp = (
            member.get("owner_pid"),
            member.get("provider"),
            member.get("terminal_control"),
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
            time.monotonic() + OP_SECONDS,
            False,
            False,
        )
        self.last: Screen | None = None

    def _check(self, terminal: dict[str, Any]) -> None:
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
        agent, root = _prove(self.owner, terminal)
        if (
            list(agent.start) != self.binding["owner_start"]
            or list(root.start) != self.binding["root_start"]
        ):
            raise keys.KeysError("native process incarnation changed")

    def _open(self, *, write: bool = False) -> tuple[Session, int, Screen]:
        session = Session(self.binding["socket"], self.deadline, self.binding["core"])
        try:
            terminal = session.request("terminal.get", terminal=self.binding["address"])
            self._check(terminal)
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
            self._check(actual)
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
            )
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

    def prepare(self, body: str, options: Any) -> None:
        self.anytime = options.anytime
        screen = self.last or self.snapshot()
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
            if not self.anytime and fresh.prompt(agent) != expected.prompt(agent):
                raise keys.KeysError("composer changed before input")
            if (fresh.paste, fresh.kitty, fresh.application) != (
                expected.paste,
                expected.kitty,
                expected.application,
            ):
                raise keys.KeysError("keyboard modes changed before input")
            # Recheck the grid immediately before DATA takes viewer ownership.
            actual = session.request("terminal.get", terminal=self.binding["address"])
            self._check(actual)
            if (actual["cols"], actual["rows"]) != fresh.size:
                raise keys.KeysError("terminal resized before input")
            self.possibly_sent = (
                True  # Even a failed sendall may have written a prefix.
            )
            session.frame(channel, 0, data)
            self._check(
                session.request("terminal.get", terminal=self.binding["address"])
            )
        finally:
            session.close()

    def _pause(self, seconds: float) -> None:
        if time.monotonic() + seconds > self.deadline:
            raise keys.KeysError(
                "native input deadline reached; input may still arrive"
            )
        keys.sleep(seconds)

    def type_text(self, agent: str, text: str, *, submit: bool) -> bool:
        before = self.last or self.snapshot()
        payload = text.encode()
        if before.paste:
            payload = b"\x1b[200~" + payload + b"\x1b[201~"
        self._write(payload, agent, before)
        self._pause(keys.SUBMIT_PAUSE_SECONDS)
        inserted = self.snapshot()
        if _rendered_text(inserted.prompt(agent).typed) != _rendered_text(text):
            raise keys.KeysError(
                "composer insertion unconfirmed; input may still arrive, do not resend"
            )
        if not submit:
            return True
        if (
            agent == "codex"
            and "enter insert" in inserted.plain
            and "esc close" in inserted.plain
        ):
            self._write(encode("Enter", inserted), agent, inserted)
            self._pause(keys.SUBMIT_PAUSE_SECONDS)
            selected = self.snapshot()
            prompt = selected.prompt(agent)
            if (
                _rendered_text(prompt.typed) != _rendered_text(text)
                or prompt.working
                or "enter insert" in selected.plain
            ):
                raise keys.KeysError(
                    "skill completion selection unconfirmed; no submission retry"
                )
            inserted = selected
        self._write(encode("Enter", inserted), agent, inserted)
        self._pause(keys.SUBMIT_CHECK_SECONDS)
        after = self.snapshot()
        prompt = after.prompt(agent)
        if _rendered_text(prompt.typed) == _rendered_text(text):
            raise keys.KeysError(
                "submission unconfirmed; Enter may still arrive, do not resend"
            )
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        fresh_turn = prompt.working and not inserted.prompt(agent).working
        needle = _rendered_text(first)
        fresh_text = needle and _rendered_text(after.plain).count(
            needle
        ) > _rendered_text(before.plain).count(needle)
        if prompt.shown and not prompt.typed and (fresh_turn or fresh_text):
            return True
        raise keys.KeysError(
            "submission unconfirmed; input may still arrive, do not resend"
        )

    def send_keys(self, names: tuple[str, ...]) -> None:
        for name in names:
            before = self.last or self.snapshot()
            self._write(encode(name, before), str(self.stamp[1]), before)
            self._pause(0.1)
            after = self.snapshot()
            if after.plain == before.plain and after.status == before.status:
                raise keys.KeysError(
                    "key effect unconfirmed; input may still arrive, do not resend"
                )


def resolve(
    member: dict[str, Any], current: Callable[[], dict[str, Any]]
) -> Tmux | Herdr | None:
    owner = int(member.get("owner_pid") or 0)
    if owner <= 1:
        return None
    pane = keys.find_pane(owner)
    if pane is not None:
        try:
            same_tty = process(owner).tty == process(pane.pid).tty
        except keys.KeysError:
            same_tty = True  # Preserve the existing tmux route on unsupported hosts.
        if same_tty:
            return Tmux(pane)
    binding = member.get("terminal_control")
    if not isinstance(binding, dict) or not binding:
        return None
    try:
        control = Herdr(member, binding, current)
        # Validate discovery now; no screen read or writable attachment for status.
        with Session(binding["socket"], control.deadline, binding["core"]) as session:
            control._check(session.request("terminal.get", terminal=binding["address"]))
        return control
    except (keys.KeysError, OSError, ValueError, KeyError, TypeError):
        return None
