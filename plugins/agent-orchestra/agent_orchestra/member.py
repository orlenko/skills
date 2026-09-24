from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, codex_wake, core, jev, jev_shadow, keys, monitor_lock
from .core import (
    APIError,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    OrchestraError,
    api_request,
    atomic_write_json,
    bucket_dir,
    decode_invite,
    ensure_private_dir,
    instance_key,
    member_path,
    new_message_id,
    normalize_provider,
    now,
    read_json,
    runtime_dir,
    state_root,
)
from .lifecycle import attention as lifecycle_attention, thresholds
from .protocol import (
    ProtocolError,
    TypeOptions,
    attention_rank,
    message_body,
    parse_message,
    reply_required,
)


PRESENCE_STALE_SECONDS = 120
MAX_EVENT_FILES = 200
MONITOR_WAIT_SECONDS = 25
MONITOR_LIMIT = 50
_MONITOR_STALE_SECONDS = 60
_HANDLED_RETRY_SECONDS = 60
_HANDLED_RETRIES_PER_PASS = 10
_SENT_CLOCK_SKEW_SECONDS = 300
# A local finish the hub answers with one of these will never be recorded there.
_UNSYNCABLE_STATUSES = frozenset({400, 403, 404, 410})
# A send the hub answers with one of these is wrong forever; retrying is noise.
_PERMANENT_SEND_STATUSES = frozenset({400, 403, 404, 409, 413})
_BACKGROUND_PROCESSES: list[subprocess.Popen[bytes]] = []
# The only reconnect line status_owed trusts. It matches the first line of an
# event body and nothing else, so a name carrying a newline cannot forge one.
_RECONNECT_RE = re.compile(r"^presence (\S+) (.*) connected absent_since=([0-9.]+)$")
_MISSING = object()


def _module_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    root = str(_module_root())
    old = env.get("PYTHONPATH")
    env["PYTHONPATH"] = root if not old else f"{root}{os.pathsep}{old}"
    return env


def _spawn_module(args: list[str], log_path: Path) -> int:
    _BACKGROUND_PROCESSES[:] = [
        process for process in _BACKGROUND_PROCESSES if process.poll() is None
    ]
    ensure_private_dir(log_path.parent)
    stream = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "agent_orchestra", *args],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            env=_child_environment(),
            start_new_session=True,
            close_fds=True,
        )
        _BACKGROUND_PROCESSES.append(process)
    finally:
        stream.close()
    return process.pid


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    for process in _BACKGROUND_PROCESSES:
        # A child of this process stays a zombie until it is reaped, and a
        # zombie still answers kill(pid, 0). poll() reaps it and tells the truth.
        if process.pid == pid:
            return process.poll() is None
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _as_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 1 else None


def acquire_pid_lock(path: Path) -> bool:
    """Take an exclusive lock file, breaking one a dead process left behind."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "started_at": now()}, stream)
            return True
        except FileExistsError:
            try:
                pid = int(read_json(path).get("pid", 0))
            except (OrchestraError, TypeError, ValueError):
                pid = 0
            if _pid_alive(pid):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return False


def release_pid_lock(path: Path) -> None:
    try:
        if int(read_json(path).get("pid", 0)) == os.getpid():
            path.unlink(missing_ok=True)
    except (OrchestraError, TypeError, ValueError):
        pass


def _reap_spawned_processes(pids: list[int], timeout: float = 5.0) -> None:
    """Test helper: wait for, then terminate, this process's selected children."""
    deadline = time.monotonic() + timeout
    selected = [process for process in _BACKGROUND_PROCESSES if process.pid in set(pids)]
    for process in selected:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
    _BACKGROUND_PROCESSES[:] = [
        process for process in _BACKGROUND_PROCESSES if process.poll() is None
    ]


def join(
    invite: str,
    *,
    provider: str,
    cwd: str,
    name: str,
    start_background_monitor: bool = True,
) -> dict[str, Any]:
    provider = normalize_provider(provider)
    decoded = decode_invite(invite)
    temporary = {
        "endpoints": decoded["endpoints"],
        "fingerprint": decoded["fingerprint"],
    }
    result = api_request(
        temporary,
        "POST",
        "/v1/join",
        {"secret": decoded["secret"], "name": name[:80], "provider": provider},
        auth=False,
    )
    member_id = str(result["member_id"])
    hub = result.get("hub") if isinstance(result.get("hub"), dict) else {}
    invite_hub = decoded.get("hub") if isinstance(decoded.get("hub"), dict) else {}
    endpoints = hub.get("endpoints") or decoded["endpoints"]
    member = {
        "protocol": PROTOCOL_VERSION,
        "member_id": member_id,
        "orchestra_id": str(result["orchestra_id"]),
        "role": result.get("role") or decoded.get("role"),
        "parent": result.get("parent"),
        "name": result.get("name") or name[:80],
        "provider": provider,
        "cwd": str(Path(cwd).expanduser().resolve()),
        "instance_key": instance_key(provider, cwd),
        "endpoints": list(endpoints),
        "fingerprint": hub.get("fingerprint") or decoded["fingerprint"],
        "token": result["token"],
        "conductor_id": result.get("conductor_id"),
        "hub_name": hub.get("name") or invite_hub.get("name"),
        "joined_at": now(),
        "closed_at": None,
        "closed_reason": None,
        # The agent session that owns this membership. Hooks claim a membership
        # only from the session whose agent ancestor matches, so a print-mode
        # child in the same directory cannot steal it.
        "owner_pid": core.agent_ancestor_pid(),
    }
    save_member(member)
    if provider == "codex":
        codex_wake.register(member_id, prefix="AGENT_ORCHESTRA")
        thread_id = codex_wake.target(member_id).get("thread_id")
        if thread_id:
            member["session_id"] = thread_id
            save_member(member)
    monitor_pid = start_monitor(member) if start_background_monitor else None
    return {
        "member_id": member_id,
        "orchestra_id": member["orchestra_id"],
        "role": member["role"],
        "parent": member["parent"],
        "conductor_id": member["conductor_id"],
        "hub_name": member["hub_name"],
        "monitor_pid": monitor_pid,
    }


def iter_members(*, provider: str, cwd: str) -> list[dict[str, Any]]:
    provider = normalize_provider(provider)
    root = state_root() / "members"
    if not root.exists():
        return []
    key = instance_key(provider, cwd)
    rows: list[dict[str, Any]] = []
    for path in root.glob("*/member.json"):
        try:
            item = read_json(path)
        except OrchestraError:
            continue
        if item.get("instance_key") == key and not item.get("closed_at"):
            rows.append(item)
    return sorted(rows, key=lambda item: float(item.get("joined_at", 0)), reverse=True)


def _bound_to_another_session(member_id: str, pid: int) -> bool:
    """True when a hook in a different live session already holds this seat."""
    for _, record in core.binding_records():
        if str(record.get("member_id") or "") != member_id:
            continue
        owner = _as_pid(record.get("owner_pid"))
        if owner is not None and owner != pid and _pid_alive(owner):
            return True
    return False


def claim_ownership(member: dict[str, Any]) -> dict[str, Any]:
    """Point a membership at the agent session that is running right now.

    Wake-up matches `owner_pid` against the hook's own agent ancestor, and
    nothing rewrote that pid after `join`. An agent process that is replaced —
    a resumed session, a quota migration — inherits the seat with a dead pid
    recorded, so no hook matches it again and only a typed prompt can adopt it
    back. Every command a session runs reaches here, so the seat repairs itself
    and an unattended session wakes on mail again with nobody at the keyboard.

    A live owner is never displaced, and a one-shot child is no owner at all:
    `agent_session_pid` answers None for it, so it cannot take the wake with it
    when it exits.
    """
    current = _as_pid(member.get("owner_pid"))
    # One signal, one syscall: a live owner settles it, and the healthy case
    # never pays for the `ps` walk that finds this session's own process.
    if current is not None and _pid_alive(current):
        return member
    pid = core.agent_session_pid()
    if pid is None or pid == current or _other_agent(member, pid):
        return member
    member_id = str(member["member_id"])
    lock_path = runtime_dir() / f"{member_id}.owner.lock"
    if not acquire_pid_lock(lock_path):
        # Another session is claiming the same orphan. First writer wins, and
        # this one reads the winner's live pid on its next command.
        return member
    try:
        # save_member replaces the whole file, so the decision is made again on
        # what is on disk now, under the lock.
        fresh = load_member(member_id)
        current = _as_pid(fresh.get("owner_pid"))
        if current == pid or (current is not None and _pid_alive(current)):
            return fresh
        if fresh.get("closed_at") or _bound_to_another_session(member_id, pid):
            return fresh
        fresh["owner_pid"] = pid
        save_member(fresh)
        return fresh
    except (OrchestraError, OSError):
        # A seat that cannot be repaired is not a command that should fail.
        return member
    finally:
        release_pid_lock(lock_path)


def _other_agent(member: dict[str, Any], pid: int) -> bool:
    """True when `pid` is a different agent than the one this seat was joined by.

    A Claude session that ran a command with `--provider codex` took Codex
    seats twice by 2026-09-24. Its Claude hooks then never saw the seat, the
    Codex wake queued notices for a thread that did not exist, and `type` read
    the wrong screen. Moving a seat to another agent is `adopt`, on purpose.
    """
    provider = str(member.get("provider") or "")
    if provider not in ("claude", "codex"):
        return False
    kind = core.agent_kind(pid)
    return kind is not None and kind != provider


def adopt(member_id: str, *, provider: str, cwd: str) -> dict[str, Any]:
    """Move a seat to the agent running this command, in this directory.

    For a session that replaced the agent a seat was joined by: a Claude session
    taking over a Codex seat, or the reverse. The hub's roster keeps the provider
    the seat joined with; locally the seat, its hooks, and its wake follow the
    new agent.
    """
    provider = normalize_provider(provider)
    caller = core.agent_session_pid()
    kind = core.agent_kind(caller) if caller else None
    if kind and provider in ("claude", "codex") and kind != provider:
        # 2026-09-24: Trumpet, a Claude session, adopted its seat back as codex.
        # No Claude process may own a Codex seat, so that unlinked the seat
        # from the session and hid two messages from its hooks.
        raise OrchestraError(f"This session is {kind}; run adopt with --provider {kind}")
    lock_path = runtime_dir() / f"{member_id}.owner.lock"
    if not acquire_pid_lock(lock_path):
        raise OrchestraError("Another command is changing this seat; try again")
    try:
        member = load_member(member_id)
        if member.get("closed_at"):
            raise OrchestraError(f"Membership is closed ({member.get('closed_reason') or 'closed'})")
        before = {"provider": member.get("provider"), "cwd": member.get("cwd")}
        member["provider"] = provider
        member["cwd"] = str(Path(cwd).expanduser().resolve())
        member["instance_key"] = instance_key(provider, cwd)
        member["owner_pid"] = None
        member["adopted"] = {**before, "at": now()}
        save_member(member)
    finally:
        release_pid_lock(lock_path)
    if provider != "codex":
        for suffix in (".codex-session.json", ".codex-wake.json"):
            (runtime_dir() / f"{member_id}{suffix}").unlink(missing_ok=True)
    member = claim_ownership(member)
    return {"member_id": member_id, "provider": provider, "cwd": member["cwd"],
            "owner_pid": member.get("owner_pid"), "was": before}


def select_member(*, provider: str, cwd: str, member_id: str | None = None) -> dict[str, Any]:
    if member_id:
        member = load_member(member_id)
        if member.get("instance_key") != instance_key(provider, cwd):
            raise OrchestraError("Membership belongs to a different provider session or directory")
        if member.get("closed_at"):
            reason = member.get("closed_reason") or "closed"
            raise OrchestraError(f"Membership is closed ({reason})")
        return claim_ownership(member)
    rows = iter_members(provider=provider, cwd=cwd)
    if not rows:
        raise OrchestraError(
            f"No open orchestra membership for {normalize_provider(provider)} in this directory"
        )
    return claim_ownership(rows[0])


def load_member(member_id: str) -> dict[str, Any]:
    return read_json(member_path(member_id))


def save_member(member: dict[str, Any]) -> None:
    atomic_write_json(member_path(str(member["member_id"])), member)


def ensure_hub_if_local(member: dict[str, Any]) -> None:
    """Restart the hub when this machine is the hub machine, so a member on the
    always-on host is never blocked by a hub process that died."""
    orchestra_id = str(member.get("orchestra_id") or "")
    if not orchestra_id:
        return
    try:
        from .hub import ensure_hub, hub_dir
    except ImportError:
        return
    try:
        path = hub_dir(orchestra_id) / "hub.json"
        if not path.exists():
            return
        if read_json(path).get("closed_at"):
            return
        ensure_hub(orchestra_id)
    except OrchestraError:
        return
    except OSError:
        return


def _monitor_state_path(member_id: str) -> Path:
    return runtime_dir() / f"{member_id}.monitor.json"


def _monitor_record(pid: int, started_at: float, **extra: Any) -> dict[str, Any]:
    """The monitor's state file.

    `module_root` says which plugin tree this monitor runs from. ensure_monitor
    adopts any live pid, so a monitor spawned from another tree — an external
    watcher resolving its own copy of the CLI — is otherwise invisible. The
    version and the source digest say whether a foreign tree matters: the same
    digest is a cosmetic difference, a different one is real version skew.
    """
    return {
        "pid": pid,
        "started_at": started_at,
        "updated_at": now(),
        "last_error": None,
        "module_root": str(_module_root()),
        "version": __version__,
        "sources_sha256": core.sources_digest(),
        **extra,
    }


def _monitor_record_alive(record: dict[str, Any]) -> bool:
    return (
        _pid_alive(int(record.get("pid", 0)))
        and float(record.get("updated_at", 0)) >= now() - _MONITOR_STALE_SECONDS
    )


def restart_monitor(member: dict[str, Any]) -> int:
    """Replace this mailbox's monitor after an installed-code update."""
    path = _monitor_state_path(str(member["member_id"]))
    try:
        record = read_json(path)
    except OrchestraError:
        record = {}
    pid = int(record.get("pid") or 0)
    if _pid_alive(pid):
        # A stale PID file can point at a reused PID. Verify the exact module
        # and mailbox argument before signalling anything.
        result = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                capture_output=True, text=True, timeout=2, check=False)
        argv = shlex.split(result.stdout)
        expected = ["-m", "agent_orchestra", "monitor-run", "--member-id", str(member["member_id"])]
        if not any(argv[i:i + len(expected)] == expected for i in range(len(argv))):
            raise OrchestraError("Cannot verify the recorded monitor process; refusing to stop it")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while monitor_lock.owner(str(member["member_id"])) == pid and pid:
        if time.monotonic() >= deadline:
            raise OrchestraError("The previous inbox monitor has not stopped yet")
        time.sleep(0.02)
    path.unlink(missing_ok=True)
    return start_monitor(member)


def start_monitor(member: dict[str, Any]) -> int:
    member_id = str(member["member_id"])
    if member.get("closed_at"):
        # A membership that left or whose orchestra closed has nothing to poll;
        # respawning here would resurrect a monitor that just exited on a 410.
        return 0
    state_path = _monitor_state_path(member_id)
    lock_path = runtime_dir() / f"{member_id}.monitor-start.lock"
    deadline = time.monotonic() + 3
    lock_fd: int | None = None
    while time.monotonic() < deadline:
        owner_pid = monitor_lock.owner(member_id)
        if owner_pid:
            return owner_pid
        try:
            current = read_json(state_path)
            pid = int(current.get("pid", 0))
            if _monitor_record_alive(current):
                return pid
        except OrchestraError:
            pass
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(lock_fd, str(os.getpid()).encode("ascii"))
            break
        except FileExistsError:
            try:
                if now() - lock_path.stat().st_mtime > 10:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            time.sleep(0.05)
    if lock_fd is None:
        raise OrchestraError("Timed out while starting the orchestra monitor")
    try:
        try:
            current = read_json(state_path)
            pid = int(current.get("pid", 0))
            if _monitor_record_alive(current):
                return pid
        except OrchestraError:
            pass
        pid = _spawn_module(
            ["monitor-run", "--member-id", member_id],
            runtime_dir() / f"{member_id}.monitor.log",
        )
        atomic_write_json(state_path, _monitor_record(pid, now()))
        return pid
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def ensure_monitor(member: dict[str, Any]) -> int:
    """Never resurrect a monitor for a membership that is already closed: the
    one that just exited on a 410 would be started again on the next hook."""
    if member.get("closed_at"):
        return 0
    try:
        current = load_member(str(member["member_id"]))
    except (OrchestraError, KeyError):
        return start_monitor(member)
    if current.get("closed_at"):
        member["closed_at"] = current.get("closed_at")
        member["closed_reason"] = current.get("closed_reason")
        return 0
    return start_monitor(current)


def monitor_alive(member: dict[str, Any]) -> bool:
    try:
        state = read_json(_monitor_state_path(str(member["member_id"])))
        return _monitor_record_alive(state)
    except OrchestraError:
        return False


def _message_exists(member_id: str, message_id: str) -> bool:
    return any(
        (bucket_dir(member_id, bucket) / f"{message_id}.json").exists()
        for bucket in ("pending", "claimed", "done")
    )


def _store_incoming(member: dict[str, Any], envelope: dict[str, Any]) -> bool:
    member_id = str(member["member_id"])
    message_id = str(envelope["id"])
    if _message_exists(member_id, message_id):
        return False
    record = {**envelope, "received_at": now(), "local_state": "pending"}
    atomic_write_json(bucket_dir(member_id, "pending") / f"{message_id}.json", record)
    return True


def _store_event(member: dict[str, Any], envelope: dict[str, Any]) -> None:
    member_id = str(member["member_id"])
    events = bucket_dir(member_id, "events")
    path = events / f"{str(envelope['id'])}.json"
    if path.exists():
        return
    atomic_write_json(path, {**envelope, "received_at": now()})
    _trim_events(events)


def _append_local_event(member: dict[str, Any], text: str) -> None:
    """Record something the hub can no longer tell this member, so `events` and
    the hook presence line still explain why the membership ended."""
    moment = now()
    _store_event(
        member,
        {
            "id": new_message_id(),
            "from": {"id": "sys", "name": "hub", "role": "sys"},
            "act": "tell",
            "re": None,
            "task": None,
            "need": "none",
            "refs": [],
            "sent_at": moment,
            "text": text,
        },
    )


def _closed_reason(error: str) -> str:
    """Map a hub 410 onto the local closed_reason vocabulary."""
    lowered = str(error).lower()
    if "kicked" in lowered:
        return "kicked"
    if "left" in lowered:
        return "left"
    return "closed"


def _trim_events(events: Path) -> None:
    paths = list(events.glob("*.json"))
    if len(paths) <= MAX_EVENT_FILES:
        return
    dated: list[tuple[float, Path]] = []
    for item in paths:
        try:
            dated.append((item.stat().st_mtime, item))
        except OSError:
            continue
    dated.sort(key=lambda entry: entry[0], reverse=True)
    for _, item in dated[MAX_EVENT_FILES:]:
        try:
            item.unlink()
        except OSError:
            pass


def _notify(member: dict[str, Any], count: int) -> None:
    orchestra = str(member.get("hub_name") or "your orchestra")
    message = f"{count} new message{'s' if count != 1 else ''} from {orchestra}"
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            safe = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe}" with title "Agent Orchestra"'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif shutil.which("notify-send"):
            subprocess.Popen(
                ["notify-send", "Agent Orchestra", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except OSError:
        pass


def _recipient_ids(value: Any) -> list[str]:
    ids: list[str] = []
    for item in value or []:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
        elif isinstance(item, str) and item:
            ids.append(item)
    return ids


def _sent_record(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "queued_locally_at": record.get("queued_locally_at"),
        "act": record.get("act"),
        "task": record.get("task"),
        "to": record.get("to"),
        "recipients": _recipient_ids(result.get("recipients")),
    }


def _rejected_record(record: dict[str, Any], status: int, error: str) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "state": "rejected",
        "status": status,
        "error": error,
        "queued_locally_at": record.get("queued_locally_at"),
        "sent_at": None,
        "act": record.get("act"),
        "task": record.get("task"),
        "to": record.get("to"),
        "recipients": [],
    }


def flush_outbox(member: dict[str, Any]) -> list[dict[str, Any]]:
    member_id = str(member["member_id"])
    outbox = bucket_dir(member_id, "outbox")
    queued: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(outbox.glob("*.json")):
        try:
            queued.append((path, read_json(path)))
        except OrchestraError:
            continue
    # Message ids are random, so file name order is not send order; drain the
    # queue in the order the agent wrote it.
    queued.sort(key=lambda entry: (float(entry[1].get("queued_locally_at") or 0), entry[0].name))

    results: list[dict[str, Any]] = []
    unreachable: str | None = None
    closed: dict[str, Any] | None = None
    for path, record in queued:
        message_id = str(record.get("id") or path.stem)
        if closed is not None:
            results.append({"id": message_id, **closed})
            continue
        if unreachable is not None:
            results.append({"id": message_id, "state": "queued-locally", "detail": unreachable})
            continue
        payload = {
            "id": message_id,
            "to": record.get("to") or [],
            "act": record.get("act"),
            "re": record.get("re"),
            "task": record.get("task"),
            "need": record.get("need") or "none",
            "refs": record.get("refs") or [],
            "lifecycle": record.get("lifecycle"),
            "text": record.get("text") or "",
        }
        try:
            result = api_request(member, "POST", "/v1/messages", payload, timeout=15)
        except APIError as exc:
            if exc.status == 410:
                # The membership is over: closed, kicked, or already left. Every
                # later attempt gets the same answer, so retrying would queue
                # this text for a session that no longer exists.
                reason = _closed_reason(str(exc))
                atomic_write_json(
                    bucket_dir(member_id, "sent") / path.name,
                    _rejected_record(record, exc.status, str(exc)),
                )
                path.unlink(missing_ok=True)
                stored = _close_locally(member_id, reason)
                member["closed_at"] = (stored or {}).get("closed_at") or now()
                member["closed_reason"] = (stored or {}).get("closed_reason") or reason
                closed = {
                    "state": "closed",
                    "reason": reason,
                    "status": exc.status,
                    "error": str(exc),
                }
                results.append({"id": message_id, **closed})
            elif exc.status in _PERMANENT_SEND_STATUSES:
                atomic_write_json(
                    bucket_dir(member_id, "sent") / path.name,
                    _rejected_record(record, exc.status, str(exc)),
                )
                path.unlink(missing_ok=True)
                results.append(
                    {
                        "id": message_id,
                        "state": "rejected",
                        "status": exc.status,
                        "error": str(exc),
                    }
                )
            else:
                results.append({"id": message_id, "state": "queued-locally", "detail": str(exc)})
            continue
        except OrchestraError as exc:
            unreachable = str(exc)
            results.append({"id": message_id, "state": "queued-locally", "detail": unreachable})
            continue
        # An idempotent resend answers with message_status, which carries no
        # state; the hub still accepted it.
        result.setdefault("state", "queued")
        atomic_write_json(bucket_dir(member_id, "sent") / path.name, _sent_record(record, result))
        path.unlink(missing_ok=True)
        results.append(result)
    return results


def _sent_result(member_id: str, message_id: str) -> dict[str, Any] | None:
    path = bucket_dir(member_id, "sent") / f"{message_id}.json"
    if not path.exists():
        return None
    try:
        return read_json(path)
    except OrchestraError:
        return None


def send(member: dict[str, Any], text: str, to: list[str] | None = None) -> dict[str, Any]:
    envelope = parse_message(text, to)
    if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise OrchestraError(f"Message exceeds {MAX_MESSAGE_BYTES} bytes")
    member_id = str(member["member_id"])
    message_id = _queue_outbox(member_id, envelope)
    _release_quiet_on(member_id, envelope)
    ensure_hub_if_local(member)
    try:
        ensure_monitor(member)
    except OrchestraError:
        pass
    try:
        results = flush_outbox(member)
    except OrchestraError as exc:
        result = {"id": message_id, "state": "queued-locally", "detail": str(exc)}
    else:
        found = next((item for item in results if str(item.get("id")) == message_id), None)
        result = found or {"id": message_id, "state": "queued-locally"}
    if result.get("state") not in {"rejected", "closed"}:
        # After the send, so delivery never waits on it; advice only.
        warning = jev.send_check(member_id, message_id, envelope.act, envelope.need, envelope.text)
        if warning:
            result["warning"] = warning
    return result


def _queue_outbox(member_id: str, envelope: Any) -> str:
    message_id = new_message_id()
    record = {
        "id": message_id,
        "text": envelope.text,
        "to": list(envelope.to),
        "act": envelope.act,
        "re": envelope.re,
        "task": envelope.task,
        "need": envelope.need,
        "refs": list(envelope.refs),
        "lifecycle": envelope.lifecycle,
        "queued_locally_at": now(),
    }
    atomic_write_json(bucket_dir(member_id, "outbox") / f"{message_id}.json", record)
    return message_id


def _inbox_order(row: dict[str, Any]) -> tuple[int, float]:
    return (attention_rank(row), float(row.get("sent_at") or 0))


def local_messages(member: dict[str, Any], *, claim: bool) -> list[dict[str, Any]]:
    member_id = str(member["member_id"])
    try:
        ensure_monitor(member)
    except OrchestraError:
        pass
    if claim:
        for source in sorted(bucket_dir(member_id, "pending").glob("*.json")):
            destination = bucket_dir(member_id, "claimed") / source.name
            try:
                os.replace(source, destination)
            except FileNotFoundError:
                pass
    buckets = ("claimed",) if claim else ("pending", "claimed")
    rows: list[dict[str, Any]] = []
    for bucket in buckets:
        for path in bucket_dir(member_id, bucket).glob("*.json"):
            try:
                item = read_json(path)
            except OrchestraError:
                continue
            item["local_state"] = bucket
            rows.append(item)
    if claim and member.get("provider") == "codex":
        codex_wake.observed(member_id, [str(row["id"]) for row in rows], label="Agent Orchestra")
    return sorted(rows, key=_inbox_order)


def pending_count(member: dict[str, Any]) -> int:
    member_id = str(member["member_id"])
    return sum(
        len(list(bucket_dir(member_id, bucket).glob("*.json")))
        for bucket in ("pending", "claimed")
    )


def _needs_sync(done: dict[str, Any]) -> bool:
    return done.get("sync_state") == "unsynced"


def _sync_handled(member: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
    member_id = str(member["member_id"])
    message_id = str(done["id"])
    done["sync_attempts"] = int(done.get("sync_attempts", 0)) + 1
    done["last_sync_attempt_at"] = now()
    try:
        remote = api_request(member, "POST", f"/v1/messages/{message_id}/handled", {})
    except APIError as exc:
        done["sync_state"] = "abandoned" if exc.status in _UNSYNCABLE_STATUSES else "unsynced"
        done["sync_error"] = str(exc)[:300]
    except OrchestraError as exc:
        done["sync_state"] = "unsynced"
        done["sync_error"] = str(exc)[:300]
    else:
        done["handled_at"] = remote.get("handled_at") or done.get("handled_at") or now()
        done["sync_state"] = "synced"
        done["sync_error"] = None
        done["synced_at"] = now()
    _write_done(member_id, done)
    return done


# A done record the hub has not recorded yet carries a `<id>.unsynced` marker
# beside it, so the monitor lists the markers instead of parsing every record.
# It used to parse all of done/ on every pass: 2,482 files for the conductor,
# 0.3-0.5 s of CPU and seconds of wall time per pass on a loaded machine.
_UNSYNCED = ".unsynced"
_MAIL_RETENTION_SECONDS = 14 * 86400
_PRUNE_EVERY_SECONDS = 3600
_PRUNE_BATCH = 2000


def _write_done(member_id: str, done: dict[str, Any]) -> None:
    folder = bucket_dir(member_id, "done")
    marker = folder / f"{done['id']}{_UNSYNCED}"
    pending = _needs_sync(done)
    if pending:
        # Marker first: a crash between the two writes leaves one extra read.
        marker.touch()
    atomic_write_json(folder / f"{done['id']}.json", done)
    if not pending:
        marker.unlink(missing_ok=True)


def _index_path(member_id: str) -> Path:
    return runtime_dir() / f"{member_id}.unsynced-index.json"


def _ensure_unsynced_index(member_id: str, folder: Path) -> None:
    """Once per member, mark the unsynced records an older version left unmarked."""
    stamp = _index_path(member_id)
    if stamp.exists():
        return
    for path in folder.glob("*.json"):
        try:
            if _needs_sync(read_json(path)):
                (folder / f"{path.stem}{_UNSYNCED}").touch()
        except OrchestraError:
            continue
    atomic_write_json(stamp, {"built_at": now()})


def _finish_result(done: dict[str, Any], prefix: str | None = None) -> dict[str, Any]:
    sync_state = str(done.get("sync_state", "synced"))
    result: dict[str, Any] = {
        "id": done["id"],
        "state": "handled" if sync_state == "synced" else "handled-locally",
        "handled_at": done.get("handled_at"),
        "act": done.get("act"),
        "task": done.get("task"),
        "sync": sync_state,
    }
    notes = [note for note in (prefix, _sync_note(sync_state, done.get("sync_error"))) if note]
    if notes:
        result["detail"] = "; ".join(notes)
    return result


def _sync_note(sync_state: str, error: str | None) -> str | None:
    if sync_state == "unsynced":
        return f"handled locally, could not notify the hub (will retry): {error}"
    if sync_state == "abandoned":
        return f"handled locally, the hub will never record it: {error}"
    return None


def unsynced_handled(member_id: str) -> list[dict[str, Any]]:
    folder = bucket_dir(member_id, "done")
    _ensure_unsynced_index(member_id, folder)
    rows: list[dict[str, Any]] = []
    for marker in sorted(folder.glob(f"*{_UNSYNCED}")):
        try:
            done = read_json(folder / f"{marker.name[:-len(_UNSYNCED)]}.json")
        except OrchestraError:
            marker.unlink(missing_ok=True)
            continue
        if _needs_sync(done):
            rows.append(done)
        else:
            marker.unlink(missing_ok=True)
    return rows


def prune_mail(member_id: str, *, older_than: float = _MAIL_RETENTION_SECONDS) -> int:
    """Delete handled and sent mail older than the retention window.

    Nothing pruned them before, so the conductor held 2,482 done and 1,354 sent
    records after three weeks. A done record still owed to the hub keeps its
    marker and is never deleted; neither is anything still in the inbox.
    """
    folder = bucket_dir(member_id, "done")
    if not _index_path(member_id).exists():
        return 0
    cutoff = now() - older_than
    removed = 0
    for bucket, keep in ((folder, lambda path: (folder / f"{path.stem}{_UNSYNCED}").exists()),
                         (bucket_dir(member_id, "sent"), lambda path: False)):
        for path in bucket.glob("*.json"):
            if removed >= _PRUNE_BATCH:
                return removed
            try:
                if path.stat().st_mtime >= cutoff or keep(path):
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def flush_handled(member: dict[str, Any]) -> list[dict[str, Any]]:
    """Retry the hub 'handled' notices that a local finish could not deliver."""
    member_id = str(member["member_id"])
    due = [
        done
        for done in unsynced_handled(member_id)
        if now() - float(done.get("last_sync_attempt_at", 0)) >= _HANDLED_RETRY_SECONDS
    ]
    return [_sync_handled(member, done) for done in due[:_HANDLED_RETRIES_PER_PASS]]


def finish_messages(member: dict[str, Any], message_ids: list[str]) -> list[dict[str, Any]]:
    member_id = str(member["member_id"])
    if member.get("provider") == "codex":
        codex_wake.observed(member_id, message_ids, label="Agent Orchestra")
    results: list[dict[str, Any]] = []
    for message_id in message_ids:
        source = None
        for bucket in ("claimed", "pending"):
            candidate = bucket_dir(member_id, bucket) / f"{message_id}.json"
            if candidate.exists():
                source = candidate
                break
        done_path = bucket_dir(member_id, "done") / f"{message_id}.json"
        if source is None:
            if not done_path.exists():
                raise OrchestraError(f"Local message not found: {message_id}")
            done = read_json(done_path)
            if _needs_sync(done):
                done = _sync_handled(member, done)
            results.append(_finish_result(done, "was already handled locally"))
            continue
        record = read_json(source)
        done = {
            "id": message_id,
            "from": record.get("from"),
            "act": record.get("act"),
            "task": record.get("task"),
            "sent_at": record.get("sent_at"),
            "received_at": record.get("received_at"),
            "handled_at": now(),
            "sync_state": "unsynced",
            "sync_error": None,
            "synced_at": None,
            "sync_attempts": 0,
            "last_sync_attempt_at": 0,
        }
        _write_done(member_id, done)
        source.unlink(missing_ok=True)
        results.append(_finish_result(_sync_handled(member, done)))
    if any(item.get("sync") == "synced" for item in results):
        flush_handled(member)
    return results


def wait_for_messages(member: dict[str, Any], timeout: float, *, claim: bool) -> list[dict[str, Any]]:
    try:
        ensure_monitor(member)
    except OrchestraError:
        pass
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        rows = local_messages(member, claim=claim)
        if rows or time.monotonic() >= deadline:
            return rows
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def message_status(member: dict[str, Any], message_id: str) -> dict[str, Any]:
    ensure_hub_if_local(member)
    return api_request(member, "GET", f"/v1/messages/{message_id}")


def _self_row(payload: dict[str, Any], member_id: str) -> dict[str, Any] | None:
    for row in payload.get("members") or []:
        if isinstance(row, dict) and str(row.get("id")) == member_id:
            return row
    return None


def _apply_self_row(
    member: dict[str, Any], row: Any, conductor_id: Any = _MISSING
) -> bool:
    """Copy role, parent, and conductor_id from the hub's view of this member.

    A promotion or a reparenting decided elsewhere would otherwise never reach
    member.json, and a promoted conductor would keep reporting itself a player.
    """
    changed = False
    if isinstance(row, dict):
        for key in ("role", "parent"):
            if key in row and row.get(key) != member.get(key):
                member[key] = row.get(key)
                changed = True
    if conductor_id is not _MISSING and conductor_id != member.get("conductor_id"):
        member["conductor_id"] = conductor_id
        changed = True
    if changed:
        try:
            save_member(member)
        except OrchestraError:
            return changed
    return changed


def members(member: dict[str, Any]) -> dict[str, Any]:
    ensure_hub_if_local(member)
    result = api_request(member, "GET", "/v1/members")
    _apply_self_row(
        member,
        _self_row(result, str(member["member_id"])),
        result.get("conductor_id", _MISSING),
    )
    # The hub keeps `presence` as the heartbeat state, so a revoked row still
    # reads `connected` there. Callers read finality from `presence`, so the
    # roster ships the same summary `status` does.
    result["members"] = [
        _presence_summary(row)
        for row in (result.get("members") or [])
        if isinstance(row, dict)
    ]
    return result


def tasks(
    member: dict[str, Any],
    *,
    response_within: float | None = None,
    stale_after: float | None = None,
) -> dict[str, Any]:
    ensure_hub_if_local(member)
    result = api_request(member, "GET", "/v1/tasks")
    rows = [row for row in (result.get("tasks") or []) if isinstance(row, dict)]
    _save_task_snapshot(member, rows)
    within, stale = thresholds(response_within, stale_after)
    result["attention"] = task_attention(
        member, rows, response_within=within, stale_after=stale
    )
    result["thresholds"] = {"response_within": within, "stale_after": stale}
    # A hub from before task lifecycle answers without `owners`. Say so, so
    # an empty attention list is never read as "nothing is late".
    result["lifecycle"] = (
        "derived" if all(isinstance(row.get("owners"), list) for row in rows) else "unavailable"
    )
    return result


def task_attention(
    member: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    at: float | None = None,
    response_within: float | None = None,
    stale_after: float | None = None,
) -> list[dict[str, Any]]:
    within, stale = thresholds(response_within, stale_after)
    return lifecycle_attention(
        rows,
        member_id=str(member["member_id"]),
        conductor_id=member.get("conductor_id"),
        at=now() if at is None else at,
        response_within=within,
        stale_after=stale,
    )


def _task_snapshot_path(member_id: str) -> Path:
    return runtime_dir() / f"{member_id}.tasks.json"


def _save_task_snapshot(member: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    atomic_write_json(
        _task_snapshot_path(str(member["member_id"])), {"fetched_at": now(), "tasks": rows}
    )


def snapshot_attention(member: dict[str, Any]) -> dict[str, Any] | None:
    """Attention from the monitor's last task read, for hooks that stay offline.

    None when no read ever landed. `as_of` is when the hub answered, so a
    reader can tell an old view from a quiet one.
    """
    try:
        snapshot = read_json(_task_snapshot_path(str(member["member_id"])))
    except OrchestraError:
        return None
    rows = [row for row in (snapshot.get("tasks") or []) if isinstance(row, dict)]
    return {
        "as_of": snapshot.get("fetched_at"),
        "items": task_attention(member, rows),
    }


def seat_state(member: dict[str, Any]) -> str:
    """Whether a session holds this membership, from this machine's records.

    The hub's presence says only that the monitor heartbeats. On 2026-09-14 a
    player's monitor stayed connected for hours while no session held his
    membership: no owner pid, no binding, nine messages on disk that no hook
    would surface. This is the view that tells those apart.
    """
    owner = _as_pid(member.get("owner_pid"))
    if owner is not None and _pid_alive(owner):
        return "held"
    member_id = str(member["member_id"])
    unverified = False
    for _, record in core.binding_records():
        if str(record.get("member_id") or "") != member_id:
            continue
        pid = _as_pid(record.get("owner_pid"))
        if pid is None:
            unverified = True
        elif _pid_alive(pid):
            return "held"
    return "unverified" if unverified else "empty"


def wake_capability(provider: Any, member_id: str = "") -> dict[str, Any]:
    """What can bring this session back to its mail, for what is installed."""
    name = str(provider or "")
    if name == "claude":
        return {
            "idle_reawaken": True,
            "via": "hook-wait (Stop, asyncRewake)",
            "surfaces_at": ["SessionStart", "UserPromptSubmit", "Stop"],
        }
    if name == "codex":
        return codex_wake.capability(member_id)
    return {"idle_reawaken": False, "via": "none", "surfaces_at": []}


def _event_sort_key(row: dict[str, Any]) -> tuple[float, float]:
    return (float(row.get("sent_at") or 0), float(row.get("received_at") or 0))


def recent_events(member: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Newest first, so a caller wanting the latest presence line reads row 0."""
    member_id = str(member["member_id"])
    rows: list[dict[str, Any]] = []
    for path in bucket_dir(member_id, "events").glob("*.json"):
        try:
            rows.append(read_json(path))
        except OrchestraError:
            continue
    rows.sort(key=_event_sort_key, reverse=True)
    return rows[: max(0, int(limit))]


def _reconnect_absent_since(text: Any, conductor_id: str) -> float | None:
    """The absent_since stamp of a `presence <conductor> ... connected` event.

    Only the first line counts. The hub sanitises names into one line, so a
    body whose later lines look like a reconnect was forged by a member and
    must not clear the status that member owes.
    """
    lines = str(text or "").splitlines()
    if not lines:
        return None
    match = _RECONNECT_RE.match(lines[0].strip())
    if match is None or match.group(1) != conductor_id:
        return None
    try:
        return float(match.group(3))
    except ValueError:
        return None


def status_owed(member: dict[str, Any]) -> dict[str, Any]:
    empty = {"owed": False, "conductor_id": None, "absent_since": None, "reconnected_at": None}
    member_id = str(member["member_id"])
    conductor_id = member.get("conductor_id")
    if not conductor_id or str(conductor_id) == member_id:
        return empty
    conductor_id = str(conductor_id)

    newest: tuple[dict[str, Any], float] | None = None
    for path in bucket_dir(member_id, "events").glob("*.json"):
        try:
            row = read_json(path)
        except OrchestraError:
            continue
        absent_since = _reconnect_absent_since(row.get("text"), conductor_id)
        if absent_since is None:
            continue
        if newest is None or _event_sort_key(row) > _event_sort_key(newest[0]):
            newest = (row, absent_since)
    if newest is None:
        return empty

    row, absent_since = newest
    answer = {
        "owed": True,
        "conductor_id": conductor_id,
        "absent_since": absent_since,
        "reconnected_at": float(row.get("sent_at") or row.get("received_at") or 0) or None,
    }
    for path in bucket_dir(member_id, "sent").glob("*.json"):
        # A send after absent_since was written after it too. Every prompt's
        # hook reaches here, and parsing all 1,354 records a player had sent
        # took 3.7 s; a stat skips the old ones. absent_since is the hub's
        # clock, so the cut-off leaves room for skew.
        try:
            if path.stat().st_mtime < absent_since - _SENT_CLOCK_SKEW_SECONDS:
                continue
            sent = read_json(path)
        except (OSError, OrchestraError):
            continue
        if str(sent.get("state")) == "rejected":
            continue
        if conductor_id not in (sent.get("recipients") or []):
            continue
        if float(sent.get("sent_at") or 0) > absent_since:
            answer["owed"] = False
            break
    return answer


def _presence_summary(row: dict[str, Any]) -> dict[str, Any]:
    last_seen = float(row.get("last_seen_at") or 0)
    age = round(max(0.0, now() - last_seen), 1) if last_seen else None
    reported = str(row.get("presence") or "")
    if row.get("revoked_at"):
        reason = str(row.get("revoked_reason") or "left")
        presence = reason if reason in {"left", "kicked"} else "left"
    elif age is None:
        presence = "unknown"
    elif reported == "connected":
        presence = "connected" if age <= PRESENCE_STALE_SECONDS else "stale"
    elif reported == "stale":
        presence = "stale"
    else:
        presence = "unknown"
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "provider": row.get("provider"),
        "role": row.get("role"),
        "parent": row.get("parent"),
        "presence": presence,
        "last_seen_age": age,
        "revoked_reason": row.get("revoked_reason"),
        # Presence is the transport. `seat` says whether a session holds the
        # membership, and `unhandled` whether anyone is reading its mail.
        "seat": row.get("seat") or "unknown",
        "unhandled": int(row.get("unhandled") or 0),
        "oldest_unhandled_age": _age(row.get("oldest_unhandled_at")),
        # Set while the conductor holds this member's mail at the hub.
        "hold_until": row.get("hold_until") if float(row.get("hold_until") or 0) > now() else None,
        "hold_task": row.get("hold_task"),
        "hold_ends_on": row.get("hold_state"),
    }


def _age(moment: Any) -> float | None:
    try:
        value = float(moment)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, now() - value), 1) if value else None


def status(member: dict[str, Any]) -> dict[str, Any]:
    member_id = str(member["member_id"])
    ensure_hub_if_local(member)
    try:
        ensure_monitor(member)
    except OrchestraError:
        pass
    remote: dict[str, Any] | None = None
    error: str | None = None
    try:
        remote = api_request(member, "GET", "/v1/status")
    except OrchestraError as exc:
        error = str(exc)
    if remote is not None:
        flush_handled(member)
        # A role, parent, or conductor change made while this session was away
        # must not leave the local record stale; status_owed reads conductor_id
        # and the caller reads role.
        _apply_self_row(
            member, remote.get("self"), remote.get("conductor_id", _MISSING)
        )

    rows = local_messages(member, claim=False)
    inbox_by_act: dict[str, int] = {}
    for row in rows:
        act = str(row.get("act") or "tell")
        inbox_by_act[act] = inbox_by_act.get(act, 0) + 1

    summaries = [
        _presence_summary(row)
        for row in ((remote or {}).get("members") or [])
        if isinstance(row, dict)
    ]
    conductor_id = (remote or {}).get("conductor_id") or member.get("conductor_id")
    conductor = next(
        (item for item in summaries if conductor_id and item.get("id") == conductor_id), None
    )

    monitor_state: dict[str, Any] = {}
    try:
        monitor_state = read_json(_monitor_state_path(member_id))
    except OrchestraError:
        pass

    return {
        "orchestra_id": member.get("orchestra_id"),
        "member_id": member_id,
        "role": member.get("role"),
        "parent": member.get("parent"),
        "name": member.get("name"),
        "conductor": conductor,
        "hub": {
            "reachable": remote is not None,
            "error": error,
            "name": member.get("hub_name"),
        },
        "monitor": {
            "running": monitor_alive(member),
            "pid": monitor_state.get("pid"),
            "last_error": monitor_state.get("last_error"),
        },
        "local": {
            "pending": len(list(bucket_dir(member_id, "pending").glob("*.json"))),
            "claimed": len(list(bucket_dir(member_id, "claimed").glob("*.json"))),
            "outbox": len(list(bucket_dir(member_id, "outbox").glob("*.json"))),
            "unsynced_handled": len(unsynced_handled(member_id)),
            "events": len(list(bucket_dir(member_id, "events").glob("*.json"))),
        },
        "inbox_by_act": inbox_by_act,
        "reply_required": sum(1 for row in rows if reply_required(row)),
        "members": summaries,
        "status_owed": status_owed(member),
        "attention": snapshot_attention(member),
        "seat": seat_state(member),
        "wake": wake_capability(member.get("provider"), member_id),
        "remote": remote,
    }


def invite(
    member: dict[str, Any],
    *,
    role: str = "player",
    parent: str | None = "self",
    name: str | None = None,
    ttl: int = 3600,
) -> dict[str, Any]:
    ensure_hub_if_local(member)
    payload: dict[str, Any] = {"role": role, "parent": parent, "ttl": int(ttl)}
    if name:
        payload["name"] = name[:80]
    return api_request(member, "POST", "/v1/invite", payload)


def set_conductor(member: dict[str, Any], member_id: str) -> dict[str, Any]:
    ensure_hub_if_local(member)
    result = api_request(member, "POST", "/v1/conductor", {"member_id": member_id})
    conductor_id = result.get("conductor_id") or member_id
    member["conductor_id"] = conductor_id
    if str(conductor_id) == str(member["member_id"]):
        member["role"] = "conductor"
    elif member.get("role") == "conductor":
        member["role"] = "player"
    save_member(member)
    return result


def kick(member: dict[str, Any], member_id: str, reason: str | None = None) -> dict[str, Any]:
    ensure_hub_if_local(member)
    payload: dict[str, Any] = {"member_id": member_id}
    if reason:
        payload["reason"] = reason[:200]
    return api_request(member, "POST", "/v1/kick", payload)


def _close_locally(member_id: str, reason: str) -> dict[str, Any] | None:
    """End the membership on this machine after a hub 410, once, and leave a
    local event behind so the session can see why its mail stopped."""
    try:
        member = load_member(member_id)
    except OrchestraError:
        return None
    if member.get("closed_at"):
        return member
    member["closed_at"] = now()
    member["closed_reason"] = str(reason)[:300]
    save_member(member)
    _append_local_event(member, member["closed_reason"])
    return member


def leave(member: dict[str, Any]) -> dict[str, Any]:
    """Leave locally whatever the hub does; a hub that never comes back must not
    keep this session on the roster forever."""
    ensure_hub_if_local(member)
    left_at = now()
    member["closed_at"] = left_at
    member["closed_reason"] = "left"
    save_member(member)

    remote: dict[str, Any] | None = None
    detail: str | None = None
    try:
        remote = api_request(member, "POST", "/v1/leave", {})
    except APIError as exc:
        if exc.status == 410:
            remote = {"ok": True, "left_at": None}
        else:
            detail = str(exc)
    except OrchestraError as exc:
        detail = str(exc)

    result: dict[str, Any] = {
        "ok": True,
        "member_id": member["member_id"],
        "left_at": (remote or {}).get("left_at") or left_at,
        "state": "left" if remote is not None else "left-locally",
    }
    if detail:
        result["detail"] = f"left locally, could not notify the hub: {detail}"
    return result


def close(member: dict[str, Any]) -> dict[str, Any]:
    """A player may not close the orchestra, and only the hub knows that, so the
    hub answers before this session gives up its own membership."""
    ensure_hub_if_local(member)
    remote: dict[str, Any] | None = None
    detail: str | None = None
    try:
        remote = api_request(member, "POST", "/v1/close", {})
    except APIError as exc:
        if exc.status == 403:
            return {
                "ok": False,
                "member_id": member["member_id"],
                "state": "rejected",
                "status": exc.status,
                "error": str(exc),
            }
        if exc.status == 410:
            remote = {"ok": True, "closed_at": None}
        else:
            detail = str(exc)
    except OrchestraError as exc:
        detail = str(exc)

    closed_at = (remote or {}).get("closed_at") or now()
    member["closed_at"] = closed_at
    member["closed_reason"] = "closed"
    save_member(member)
    result: dict[str, Any] = {
        "ok": True,
        "member_id": member["member_id"],
        "closed_at": closed_at,
        "state": "closed" if remote is not None else "closed-locally",
    }
    if detail:
        result["detail"] = f"closed locally, could not notify the hub: {detail}"
    return result


# ---- ACT type: the conductor types into this member's session --------------


def _quiet_path(member_id: str) -> Path:
    return runtime_dir() / f"{member_id}.quiet.json"


def quiet_until(member_id: str) -> float | None:
    """When the orchestra may speak in this member's session again, or None.

    Set just before conductor text is typed. develop's /qc and /aprs relay the
    session's latest user message as the authoritative request, so the typed
    prompt has to stay latest until the work it starts is running: hook
    context, a hook-wait wake, a Codex queue notice or an agent-nudge line
    after it would replace it.
    """
    try:
        until = float(read_json(_quiet_path(member_id)).get("until") or 0)
    except (OrchestraError, TypeError, ValueError):
        return None
    return until if until > now() else None


def clear_quiet(member_id: str, task: str | None = None) -> None:
    """STATE started ends the quiet: the typed work is running.

    A quiet tied to a task ends only on that task's STATE started.
    """
    path = _quiet_path(member_id)
    try:
        record = read_json(path)
    except OrchestraError:
        return
    if task and record.get("task") and record.get("task") != task:
        return
    path.unlink(missing_ok=True)


def _release_quiet_on(member_id: str, envelope: Any) -> None:
    """This member's own report ends its quiet, on the rules the hub uses.

    STATE started ends a quiet set `until=started`; done or block on the task
    ends either kind. A multi-PR /qc reports started at its first launch and
    still has later launches to relay.
    """
    path = _quiet_path(member_id)
    try:
        record = read_json(path)
    except OrchestraError:
        return
    if record.get("task") and record.get("task") != envelope.task:
        return
    until = record.get("until_state") or "started"
    if envelope.act in ("done", "block") or (envelope.lifecycle == "started" and until == "started"):
        path.unlink(missing_ok=True)


def _set_quiet(member_id: str, options: TypeOptions, task: str | None, message_id: str) -> None:
    atomic_write_json(_quiet_path(member_id), {
        "until": now() + options.quiet, "task": task, "until_state": options.until,
        "message_id": message_id, "set_at": now(),
    })


def _typed_log(member_id: str) -> Path:
    return core.member_dir(member_id) / "typed.jsonl"


def _typed_before(member_id: str, message_id: str) -> bool:
    try:
        with _typed_log(member_id).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    if json.loads(line).get("id") == message_id:
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


def _log_typed(member_id: str, row: dict[str, Any]) -> None:
    """Append-only audit of every ACT type this member received."""
    path = _typed_log(member_id)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _type_into_pane(
    member: dict[str, Any], sender_id: str, body: str, options: TypeOptions, task: str | None,
    message_id: str,
) -> tuple[str, str, str | None]:
    """(result, detail, pane id) for one ACT type."""
    member_id = str(member["member_id"])
    if not sender_id or sender_id != str(member.get("conductor_id") or ""):
        # The hub checks this too; a stale or forged sender still stops here.
        return "not-allowed", "only the current conductor types into a session", None
    if options.hold_only:
        # The hub holds this member's mail already; this mirrors it here for
        # the hooks, the Codex wake and agent-nudge.
        if options.quiet:
            _set_quiet(member_id, options, task, message_id)
            until = time.strftime("%H:%M:%SZ", time.gmtime(now() + options.quiet))
            ends = "STATE started" if options.until == "started" else "done or block"
            scope = f" on {task}" if task else ""
            return "held", f"quiet until {until} or its {ends}{scope}", None
        clear_quiet(member_id)
        return "released", "quiet lifted; held mail flows now", None
    owner = _as_pid(member.get("owner_pid"))
    if owner is None or not _pid_alive(owner):
        return "no-pane", "no live agent session holds this seat", None
    pane = keys.find_pane(owner)
    if pane is None:
        return "no-pane", f"agent pid {owner} is not inside a tmux pane", None
    agent = str(member.get("provider") or "claude")
    agent = agent if agent in keys.PROMPT_GLYPHS else "claude"
    try:
        if not options.anytime:
            shown = keys.prompt(pane.id, agent)
            if not shown.shown:
                return "refused-busy", "no input box on screen", pane.id
            if shown.working:
                return "refused-busy", "a turn is running", pane.id
            # Keys alone (an Enter to submit what the box holds) may meet text.
            if shown.typed and body:
                return "refused-busy", "the input box already holds text", pane.id
        if options.quiet:
            _set_quiet(member_id, options, task, message_id)
        submitted = True
        if body:
            submitted = keys.type_text(
                pane.id, agent, body, submit=options.submit and not options.keys
            )
        keys.send_keys(pane.id, options.keys)
    except keys.KeysError as exc:
        clear_quiet(member_id)
        return "tmux-error", str(exc)[:300], pane.id
    if options.keys:
        return "typed", f"pressed {' '.join(options.keys)} in {pane.session}", pane.id
    if not options.submit:
        return "typed", "left in the input box, not submitted", pane.id
    if not submitted:
        return "not-submitted", "the text still sits in the input box", pane.id
    return "typed", f"submitted in {pane.session}", pane.id


def _handle_type(member: dict[str, Any], envelope: dict[str, Any]) -> str:
    """Type an ACT type body into this member's pane and tell the sender how it went."""
    member_id = str(member["member_id"])
    message_id = str(envelope["id"])
    if _typed_before(member_id, message_id):
        return "duplicate"
    sender = envelope.get("from") if isinstance(envelope.get("from"), dict) else {}
    sender_id = str(sender.get("id") or "")
    text = str(envelope.get("text") or "")
    body = message_body(text)
    task = envelope.get("task")
    row = {"id": message_id, "from": sender_id, "from_name": sender.get("name"),
           "text": body, "task": task, "received_at": now(), "result": "typing"}
    # Logged before any key is sent: a redelivery after a crash mid-typing
    # finds it and does not type twice.
    _log_typed(member_id, row)
    try:
        options = parse_message(text).typing or TypeOptions()
    except ProtocolError as exc:
        options = None
        result, detail, pane_id = "malformed", str(exc)[:300], None
    else:
        result, detail, pane_id = _type_into_pane(
            member, sender_id, body, options, task, message_id
        )
    _log_typed(member_id, {**row, "result": result, "detail": detail, "pane": pane_id,
                           "keys": list(options.keys) if options else [],
                           "finished_at": now()})
    if sender_id and sender_id != "sys":
        reply = (
            f"ACT tell\nTO {sender_id}\nRE {message_id}\nNEED none\n\n"
            f"type {result}: {detail}\n"
            f"member {member.get('name') or member_id}, pane {pane_id or 'none'}"
        )
        try:
            _queue_outbox(member_id, parse_message(reply))
        except (OrchestraError, OSError):
            pass
    return result


def close_tasks(
    member: dict[str, Any],
    *,
    reason: str,
    tasks: list[str] | None = None,
    before: float | None = None,
    owners: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Conductor: cancel open tasks at the hub without mailing their owners."""
    ensure_hub_if_local(member)
    return api_request(member, "POST", "/v1/tasks/close", {
        "reason": reason, "tasks": tasks or [], "before": before, "owners": owners or [],
        "dry_run": dry_run,
    }, timeout=60)


def type_into(
    member: dict[str, Any],
    target: str,
    text: str,
    *,
    options: TypeOptions,
    task: str | None = None,
    wait: float = 0.0,
) -> dict[str, Any]:
    """Conductor: have `target`'s monitor type `text` into its session's pane.

    `target` is a member id or a roster name. With `wait`, poll this inbox for
    the target's reply and finish it, so the answer is the command's output.
    """
    if not text.strip() and not options.keys and not options.hold_only:
        raise OrchestraError("Nothing to type: give text, --key NAME, or both")
    target_id = target
    if not re.fullmatch(core.MEMBER_ID_RE, target):
        roster = members(member)["members"]
        matches = [row for row in roster if row.get("name") == target
                   and row.get("presence") not in ("left", "kicked")]
        if len(matches) != 1:
            raise OrchestraError(f"No single active member named {target!r}; pass a member id")
        target_id = str(matches[0]["id"])
    headers = ["ACT type", f"TO {target_id}", f"TYPE {options.header()}"]
    if task:
        headers.append(f"TASK {task}")
    result = send(member, "\n".join(headers) + "\n\n" + text)
    message_id = str(result.get("id") or "")
    if wait <= 0 or result.get("state") in ("rejected", "closed", "queued-locally"):
        return result
    deadline = time.monotonic() + wait
    member_id = str(member["member_id"])
    while time.monotonic() < deadline:
        for row in local_messages(member, claim=False):
            if row.get("re") == message_id:
                finish_messages(member, [str(row["id"])])
                body = message_body(str(row.get("text") or "")).strip()
                return {**result, "outcome": body.split(":", 1)[0].removeprefix("type ").strip(),
                        "reply": body}
        time.sleep(0.5)
    return {**result, "outcome": "no-reply-yet",
            "reply": f"No reply within {wait:g}s; it will arrive as mail RE {message_id}"}


def _wake_codex(member: dict[str, Any]) -> None:
    if member.get("provider") != "codex" or member.get("closed_at"):
        return
    if quiet_until(str(member["member_id"])):
        return
    member_id = str(member["member_id"])
    codex_wake.wake(
        member_id, buckets=[bucket_dir(member_id, b) for b in ("pending", "claimed")],
        executable=_module_root() / "bin" / "agent-orchestra",
        id_flag="--member-id", label="Agent Orchestra",
    )


def monitor_loop(member_id: str) -> None:
    with monitor_lock.hold(member_id) as acquired:
        if acquired:
            _monitor_loop(member_id)


def _monitor_loop(member_id: str) -> None:
    member = load_member(member_id)
    state_path = _monitor_state_path(member_id)
    started_at = now()
    atomic_write_json(state_path, _monitor_record(os.getpid(), started_at))
    delay = 0.25
    next_prune = 0.0
    while not member.get("closed_at"):
        try:
            _wake_codex(member)
            ensure_hub_if_local(member)
            flush_handled(member)
            flush_outbox(member)
            if time.monotonic() >= next_prune:
                next_prune = time.monotonic() + _PRUNE_EVERY_SECONDS
                prune_mail(member_id)
            result = api_request(
                member,
                "GET",
                "/v1/messages/pending",
                query={"wait": MONITOR_WAIT_SECONDS, "limit": MONITOR_LIMIT},
                timeout=MONITOR_WAIT_SECONDS + 10,
            )
            new_count = 0
            for envelope in result.get("messages", []):
                message_id = str(envelope.get("id") or "")
                if not message_id:
                    continue
                sender = envelope.get("from") if isinstance(envelope.get("from"), dict) else {}
                if str(sender.get("id")) == "sys":
                    _store_event(member, envelope)
                    api_request(member, "POST", f"/v1/messages/{message_id}/ack", {})
                    api_request(member, "POST", f"/v1/messages/{message_id}/handled", {})
                    continue
                if envelope.get("act") == "type":
                    # Keystrokes for this session's pane, not mail for its inbox.
                    _handle_type(member, envelope)
                    api_request(member, "POST", f"/v1/messages/{message_id}/ack", {})
                    api_request(member, "POST", f"/v1/messages/{message_id}/handled", {})
                    flush_outbox(member)
                    continue
                if _store_incoming(member, envelope):
                    new_count += 1
                api_request(member, "POST", f"/v1/messages/{message_id}/ack", {})
            _wake_codex(member)
            seat = seat_state(member)
            api_request(member, "POST", "/v1/heartbeat", {"seat": seat}, timeout=5)
            roster = api_request(member, "GET", "/v1/members", timeout=10)
            _apply_self_row(member, _self_row(roster, member_id), roster.get("conductor_id", _MISSING))
            if new_count:
                _notify(member, new_count)
            try:
                # Hooks read attention from this file; a hook that dialed the
                # hub would add a round trip to every prompt.
                tasks_view = api_request(member, "GET", "/v1/tasks", timeout=10)
                _save_task_snapshot(
                    member,
                    [row for row in (tasks_view.get("tasks") or []) if isinstance(row, dict)],
                )
            except OrchestraError:
                pass
            # Log only: its answers reach nothing the monitor or hooks decide on.
            jev_shadow.tick(member, seat)
            delay = 0.25
            atomic_write_json(state_path, _monitor_record(os.getpid(), started_at))
        except APIError as exc:
            if exc.status == 410:
                _close_locally(member_id, _closed_reason(str(exc)))
                return
            _record_monitor_error(state_path, started_at, exc)
            time.sleep(delay)
            delay = min(delay * 2, 15)
        except OrchestraError as exc:
            _record_monitor_error(state_path, started_at, exc)
            time.sleep(delay)
            delay = min(delay * 2, 15)
        except Exception as exc:  # noqa: BLE001 - the monitor must outlive one bad pass
            _record_monitor_error(state_path, started_at, exc)
            time.sleep(delay)
            delay = min(delay * 2, 15)
        try:
            member = load_member(member_id)
        except OrchestraError:
            return


_LOGGED_ERROR: dict[str, float] = {}
_ERROR_LOG_REPEAT_SECONDS = 600


def _record_monitor_error(path: Path, started_at: float, error: Exception) -> None:
    # monitor.log is this process's stderr. It stayed empty for every monitor:
    # an error only overwrote last_error, so no history said what failed or
    # since when. A repeat of the same error is logged every ten minutes.
    text = f"{type(error).__name__}: {str(error)[:500]}"
    if now() - _LOGGED_ERROR.get(text, 0.0) >= _ERROR_LOG_REPEAT_SECONDS:
        _LOGGED_ERROR.clear()
        _LOGGED_ERROR[text] = now()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        print(f"{stamp} monitor error: {text}", file=sys.stderr, flush=True)
    atomic_write_json(
        path,
        _monitor_record(
            os.getpid(), started_at, last_error=str(error)[:500], last_error_at=now()
        ),
    )
