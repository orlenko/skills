"""One live inbox monitor per mailbox, including across plugin upgrades."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator, TextIO

from .core import runtime_dir


def _open(mailbox_id: str):
    path = runtime_dir() / f"{mailbox_id}.monitor-run.lock"
    return _open_path(path)


def _open_path(path: Path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+")


@contextmanager
def hold_path(path: Path) -> Iterator[TextIO | None]:
    """Hold a persistent coordination inode; a fresh descriptor owns each try."""
    # Never unlink: a contender can already have this same inode open.
    # Closing releases ownership on normal exit, exceptions and process death.
    with _open_path(path) as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield stream


def owner(mailbox_id: str) -> int:
    """An old heartbeat must not spawn a second still-running monitor."""
    with _open(mailbox_id) as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return int(stream.read().strip())
            except ValueError:
                return 0
        return 0


@contextmanager
def hold(mailbox_id: str):
    # Never unlink this file: another process may already have the inode open.
    # The kernel releases the lock even if the daemon crashes or is terminated.
    with hold_path(runtime_dir() / f"{mailbox_id}.monitor-run.lock") as stream:
        if stream is None:
            yield False
            return
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        yield True
