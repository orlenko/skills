from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import core
from .core import (
    APIError,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    OrchestraError,
    api_request,
    atomic_write_json,
    bucket_dir,
    decode_invite,
    instance_key,
    member_path,
    new_message_id,
    normalize_provider,
    now,
    read_json,
    runtime_dir,
    state_root,
)
from .protocol import parse_message, reply_required


PRESENCE_STALE_SECONDS = 120
MAX_EVENT_FILES = 200
MONITOR_WAIT_SECONDS = 25
MONITOR_LIMIT = 50
_MONITOR_STALE_SECONDS = 60
_HANDLED_RETRY_SECONDS = 60
_HANDLED_RETRIES_PER_PASS = 10
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
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    if pid is None or pid == current:
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


def _monitor_record_alive(record: dict[str, Any]) -> bool:
    return (
        _pid_alive(int(record.get("pid", 0)))
        and float(record.get("updated_at", 0)) >= now() - _MONITOR_STALE_SECONDS
    )


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
        atomic_write_json(
            state_path,
            {"pid": pid, "started_at": now(), "updated_at": now(), "last_error": None},
        )
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
        "queued_locally_at": now(),
    }
    atomic_write_json(bucket_dir(member_id, "outbox") / f"{message_id}.json", record)
    ensure_hub_if_local(member)
    try:
        ensure_monitor(member)
    except OrchestraError:
        pass
    try:
        results = flush_outbox(member)
    except OrchestraError as exc:
        return {"id": message_id, "state": "queued-locally", "detail": str(exc)}
    result = next((item for item in results if str(item.get("id")) == message_id), None)
    return result or {"id": message_id, "state": "queued-locally"}


def _inbox_order(row: dict[str, Any]) -> tuple[int, float]:
    return (0 if reply_required(row) else 1, float(row.get("sent_at") or 0))


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
    atomic_write_json(bucket_dir(member_id, "done") / f"{message_id}.json", done)
    return done


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
    rows: list[dict[str, Any]] = []
    for path in sorted(bucket_dir(member_id, "done").glob("*.json")):
        try:
            done = read_json(path)
        except OrchestraError:
            continue
        if _needs_sync(done):
            rows.append(done)
    return rows


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
        atomic_write_json(done_path, done)
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


def tasks(member: dict[str, Any]) -> dict[str, Any]:
    ensure_hub_if_local(member)
    return api_request(member, "GET", "/v1/tasks")


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
        try:
            sent = read_json(path)
        except OrchestraError:
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
    }


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


def monitor_loop(member_id: str) -> None:
    member = load_member(member_id)
    state_path = _monitor_state_path(member_id)
    started_at = now()
    atomic_write_json(
        state_path,
        {"pid": os.getpid(), "started_at": started_at, "updated_at": now(), "last_error": None},
    )
    delay = 0.25
    while not member.get("closed_at"):
        try:
            ensure_hub_if_local(member)
            flush_handled(member)
            flush_outbox(member)
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
                if _store_incoming(member, envelope):
                    new_count += 1
                api_request(member, "POST", f"/v1/messages/{message_id}/ack", {})
            api_request(member, "POST", "/v1/heartbeat", {}, timeout=5)
            roster = api_request(member, "GET", "/v1/members", timeout=10)
            _apply_self_row(member, _self_row(roster, member_id), roster.get("conductor_id", _MISSING))
            if new_count:
                _notify(member, new_count)
            delay = 0.25
            atomic_write_json(
                state_path,
                {
                    "pid": os.getpid(),
                    "started_at": started_at,
                    "updated_at": now(),
                    "last_error": None,
                },
            )
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


def _record_monitor_error(path: Path, started_at: float, error: Exception) -> None:
    atomic_write_json(
        path,
        {
            "pid": os.getpid(),
            "started_at": started_at,
            "updated_at": now(),
            "last_error": str(error)[:500],
            "last_error_at": now(),
        },
    )
