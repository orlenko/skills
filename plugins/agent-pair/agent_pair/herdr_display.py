"""Optional, display-only Herdr labels. Kept identical in Pair and Orchestra.

Each publication owns a disjoint key. A monitor can renew an enrolled binding,
never discover a terminal or establish ownership. Nothing here sends input.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import socket
import stat
import struct
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

LEASE = 120
MAX_REPLY = 256 * 1024
SECONDS = 1.5
_ADDRESS = re.compile(r"t[1-9][0-9bcdfghjkmnpqrstvwxyz]*\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _process(pid: int) -> tuple[int, int, tuple[int, ...]]:
    """Parent, controlling tty, kernel start time; reject exited/reused owners."""
    if pid <= 1:
        raise ValueError("invalid process")
    if sys.platform == "darwin":
        raw = ctypes.create_string_buffer(136)
        libc = ctypes.CDLL(None)
        if libc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), raw, 136) != 136:
            raise ValueError("unavailable process")
        data = raw.raw
        if struct.unpack_from("I", data, 20)[0] != os.getuid() or struct.unpack_from("I", data, 4)[0] == 5:
            raise ValueError("unavailable process")
        return (struct.unpack_from("I", data, 16)[0],
                struct.unpack_from("I", data, 108)[0], struct.unpack_from("QQ", data, 120))
    if sys.platform.startswith("linux"):
        path = Path("/proc") / str(pid)
        if path.stat().st_uid != os.getuid():
            raise ValueError("unavailable process")
        raw = (path / "stat").read_text()
        fields = raw[raw.rindex(")") + 2:].split()
        if fields[0] == "Z":
            raise ValueError("exited process")
        return int(fields[1]), int(fields[4]), (int(fields[19]),)
    raise ValueError("unsupported platform")


_NO_TTY = (0, -1, 0xFFFFFFFF)
# Agents whose session can be owned by a detached server process: Codex runs
# its app-server daemon without a controlling terminal, under the frontend.
_DAEMON_AGENTS = frozenset({"codex"})


def _command(pid: int) -> str:
    """The process's short command name (`comm`), for daemon-chain checks only."""
    if sys.platform == "darwin":
        raw = ctypes.create_string_buffer(136)
        if ctypes.CDLL(None).proc_pidinfo(pid, 3, ctypes.c_uint64(0), raw, 136) != 136:
            raise ValueError("unavailable process")
        return raw.raw[48:64].split(b"\0", 1)[0].decode("utf-8", "replace")
    if sys.platform.startswith("linux"):
        raw = (Path("/proc") / str(pid) / "stat").read_text()
        return raw[raw.index("(") + 1:raw.rindex(")")]
    raise ValueError("unsupported platform")


def _pids() -> list[int]:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None)
        count = libc.proc_listallpids(None, 0)
        if count <= 0:
            raise ValueError("unavailable process list")
        buffer = (ctypes.c_int * (count + 64))()
        count = libc.proc_listallpids(buffer, ctypes.sizeof(buffer))
        if count <= 0:
            raise ValueError("unavailable process list")
        return [int(pid) for pid in buffer[:count]]
    if sys.platform.startswith("linux"):
        return [int(entry.name) for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    raise ValueError("unsupported platform")


def _other_frontend(name: str, chain: set[int]) -> bool:
    """Another live frontend of this agent holding a terminal, outside the chain."""
    for pid in _pids():
        if pid <= 1 or pid in chain:
            continue
        try:
            if _process(pid)[1] not in _NO_TTY and _command(pid) == name:
                return True
        except (OSError, ValueError):
            continue
    return False


def _frontend_on(owner: int, tty: int) -> bool:
    """A tty-less owner belongs to the terminal only through an unbroken chain of
    the same agent program up to a frontend on that terminal's tty. A Codex
    daemon can serve several frontends and hands them all its first frontend's
    environment, so a second frontend anywhere makes the owner unprovable."""
    name = _command(owner)
    if name not in _DAEMON_AGENTS:
        return False
    current, chain = owner, {owner}
    for _ in range(8):
        current = _process(current)[0]
        if current <= 1 or _command(current) != name:
            return False
        chain.add(current)
        here = _process(current)[1]
        if here == tty:
            return not _other_frontend(name, chain)
        if here not in _NO_TTY:
            return False
    return False


def _proof(owner: int, terminal: dict[str, Any]) -> dict[str, Any]:
    root = int(terminal["pid"])
    agent, shell = _process(owner), _process(root)
    if shell[1] in _NO_TTY:
        raise ValueError("different terminal")
    if agent[1] != shell[1] and not (agent[1] in _NO_TTY and _frontend_on(owner, shell[1])):
        raise ValueError("different terminal")
    current = owner
    for _ in range(32):
        if current == root:
            return {"owner": owner, "owner_start": list(agent[2]),
                    "pid": root, "root_start": list(shell[2])}
        current = _process(current)[0]
        if current <= 1:
            break
    raise ValueError("different process tree")


class _Core:
    def __init__(self, binding: dict[str, Any]):
        self.sock: socket.socket | None = None
        self.deadline = time.monotonic() + SECONDS
        self.seq = 0
        endpoint = binding["socket"]
        info = os.lstat(endpoint)
        if not os.path.isabs(endpoint) or not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("not an owned local socket")
        try:
            self.sock = socket.socket(socket.AF_UNIX)
            self._timeout()
            self.sock.connect(endpoint)
            if sys.platform == "darwin":
                peer, group = ctypes.c_uint(), ctypes.c_uint()
                if ctypes.CDLL(None).getpeereid(self.sock.fileno(), ctypes.byref(peer), ctypes.byref(group)):
                    raise ValueError("unavailable peer identity")
                uid = peer.value
            elif sys.platform.startswith("linux"):
                uid = struct.unpack("3i", self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            else:
                raise ValueError("unsupported peer authentication")
            if uid != os.getuid():
                raise ValueError("different user")
            hello = self.request("hello", generation=1)
            self.identity = str(hello.get("core", {}).get("id") or "")
            if hello.get("generation") != 1 or not self.identity:
                raise ValueError("unsupported core")
            if binding.get("core") and binding["core"] != self.identity:
                raise ValueError("core changed")
        except BaseException:
            self.close()
            raise

    def _timeout(self) -> None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        self.sock.settimeout(left)

    def request(self, method: str, **params: Any) -> dict[str, Any]:
        # Deliberately not a general transport for input, files or execution.
        if method not in {"hello", "terminal.get", "terminal.label"}:
            raise ValueError("display operation only")
        self.seq += 1
        self._timeout()
        self.sock.sendall(json.dumps({"jsonrpc": "2.0", "id": self.seq,
                                     "method": method, "params": params}).encode() + b"\n")
        out = bytearray()
        while len(out) <= MAX_REPLY:
            self._timeout()
            chunk = self.sock.recv(1)
            if not chunk:
                raise ValueError("closed core")
            if chunk == b"\n":
                break
            out.extend(chunk)
        else:
            raise ValueError("oversized core reply")
        result = json.loads(out)
        if not isinstance(result, dict) or result.get("id") != self.seq or "error" in result:
            raise ValueError("invalid core reply")
        value = result.get("result")
        if not isinstance(value, dict):
            raise ValueError("invalid core result")
        return value

    def terminal(self, binding: dict[str, Any], *, owner: bool = True) -> dict[str, Any]:
        terminal = self.request("terminal.get", terminal=binding["address"])
        if terminal.get("address") != binding["address"] or terminal.get("state") != "live":
            raise ValueError("terminal unavailable")
        for field in ("pid", "created_ms"):
            if field in binding and terminal.get(field) != binding[field]:
                raise ValueError("terminal replaced")
        if owner:
            proof = _proof(int(binding["owner"]), terminal)
            for field, value in proof.items():
                if field in binding and binding[field] != value:
                    raise ValueError("process replaced")
        return terminal

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()

    def __enter__(self) -> _Core:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _read(path: Path) -> dict[str, Any]:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("invalid display state")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("oversized display state")
    record = json.loads(raw)
    if not isinstance(record, dict) or record.get("v") != 1:
        raise ValueError("unsupported display state")
    return record


def _save(path: Path, record: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".herdr-display-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _lock(path: Path) -> Iterator[bool]:
    # No waiting behind a stalled publisher on a CLI/hook's critical path.
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("invalid display lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _remove(record: dict[str, Any]) -> None:
    binding, key = record.get("binding"), record.get("key")
    if not binding or not key:
        return
    try:
        with _Core(binding) as core:
            core.terminal(binding, owner=False)
            core.request("terminal.label", terminal=binding["address"], labels={key: ""})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error):
        pass  # A disjoint publication that cannot be removed expires naturally.


def bind(path: Path, *, kind: str, participant: str, owner: int | None,
         enabled: bool | None = None, current: Callable[[], bool] | None = None,
         replace: bool = False) -> bool:
    """Enroll only from an owning lifecycle action, never from the monitor."""
    try:
        flag = os.environ.get("ORLENKO_HERDR_DISPLAY")
        explicit = enabled if enabled is not None else {"1": True, "0": False}.get(flag)
        if explicit is None and not path.exists():
            return False  # No writes, sockets or state directories by default.
        if kind not in {"orchestra", "pair"} or not _ID.fullmatch(participant):
            return False
        with _lock(path) as acquired:
            if not acquired:
                return False
            record = _read(path)
            if current is not None and not current():
                return False
            if enabled is None and record.get("enabled") is False and explicit is True:
                return False  # A persisted explicit disable beats the launch environment.
            old = record.get("binding") or {}
            if old and old.get("owner") != owner and not (replace and current is not None):
                try:
                    if list(_process(int(old["owner"]))[2]) == old.get("owner_start"):
                        return False  # A different live session still owns this publication.
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            if explicit is False:
                _remove(record)
                _save(path, {"v": 1, "enabled": False})
                return True
            if explicit is not True and not record.get("enabled"):
                return False
            # Failed re-capture (including adoption outside Herdr) retires the old
            # binding before any delayed monitor can renew it.
            endpoint = os.environ.get("HC_TERMINAL_SOCKET", "")
            address = os.environ.get("HC_TERMINAL_ID", "")
            if not owner or not endpoint or not _ADDRESS.fullmatch(address):
                _remove(record)
                _save(path, {"v": 1, "enabled": True})
                return False
            binding = {"socket": endpoint, "address": address, "owner": owner}
            try:
                with _Core(binding) as core:
                    terminal = core.terminal(binding)
                    binding.update(_proof(owner, terminal))
                    binding.update(core=core.identity, created_ms=terminal["created_ms"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error):
                _remove(record)
                _save(path, {"v": 1, "enabled": True})
                return False
            if binding == old and record.get("enabled") and record.get("kind") == kind and record.get("participant") == participant:
                return True
            _remove(record)
            publication = uuid.uuid4().hex
            key = f"orlenko.{kind}.{hashlib.sha256(participant.encode()).hexdigest()[:16]}.{publication}"
            _save(path, {"v": 1, "enabled": True, "kind": kind, "participant": participant,
                         "binding": binding, "key": key})
            return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, struct.error):
        return False


def _text(value: Any) -> str:
    return "".join(c for c in str(value or "") if unicodedata.category(c)[0] != "C" and c not in "\u2028\u2029")[:80]


def refresh(path: Path, current: Callable[[], dict[str, Any] | None]) -> None:
    """Renew from fresh authoritative state under the same fence as invalidation."""
    try:
        if not path.exists():
            return
        with _lock(path) as acquired:
            if not acquired:
                return
            record = _read(path)
            binding = record.get("binding")
            if not record.get("enabled") or not binding:
                return
            metadata = current()
            if metadata is None:
                _remove(record)
                _save(path, {"v": 1, "enabled": False})
                return
            if metadata.get("_owner_pid", binding["owner"]) != binding["owner"]:
                return
            group = metadata.get("group")
            if (not isinstance(group, str) or not _ID.fullmatch(group)
                    or metadata.get("participant") != record["participant"]):
                return
            stamp = time.time()
            payload = {"v": 1, "group": group, "participant": record["participant"],
                       "name": _text(metadata.get("name")), "group_name": _text(metadata.get("group_name")),
                       "role": _text(metadata.get("role")), "issued_at": stamp,
                       "expires_at": stamp + LEASE, "created_ms": binding["created_ms"],
                       "owner_pid": binding["owner"]}
            with _Core(binding) as core:
                core.terminal(binding)
                core.request("terminal.label", terminal=binding["address"],
                             labels={record["key"]: json.dumps(payload, separators=(",", ":"), ensure_ascii=False)})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, struct.error):
        pass


def invalidate(path: Path, *, disable: bool = False) -> None:
    """Fence renewal before closing/adopting; only remove this unique key."""
    try:
        if not path.exists():
            return
        with _lock(path) as acquired:
            if not acquired:
                return
            record = _read(path)
            # Persist retirement before network I/O, even when cleanup fails.
            _save(path, {"v": 1, "enabled": bool(record.get("enabled")) and not disable})
            _remove(record)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, struct.error):
        pass


def owned_by(path: Path, owner: int | None) -> bool:
    """Read-only provenance for an owning hook; cwd selection is not enrollment."""
    try:
        record = _read(path)
        binding = record.get("binding") or {}
        return bool(record.get("enabled") and owner and binding.get("owner") == owner
                    and binding.get("owner_start") == list(_process(owner)[2]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error):
        return False
