"""The Podium: a loopback status page for the conductor's explicit work queues.

The conductor curates a work plan by hand: one ordered queue per member, each
item with an activity, a stage, a blocker or next action, and an ETA. publish()
validates that plan and writes work-status.json atomically; serve() shows it.
Nothing here infers ownership, progress, or an ETA from a PR link or a
heartbeat. A connected session does not prove that a task is running.

Given a membership, the server also answers orchestra.json: the roster and
open tasks from that member's monitor's last pass. It reads the monitor's
snapshot files and never dials the hub, because every request made with a
member's token refreshes that member's presence at the hub. A Podium polling
as the conductor would keep a dead conductor `connected`.

An external collector may also drop state.json beside work-status.json. The
page renders its sections when it is present; its shape belongs to that
collector, not to this module.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import signal
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .core import OrchestraError, atomic_write_json, now, read_json, runtime_dir, state_root

QUEUE_STATES = frozenset({"in_progress", "queued", "blocked", "waiting", "completed", "accepted", "review"})
DEFAULT_PORT = 4300
PAGE = Path(__file__).with_name("podium.html")
STATUS_FILE = "work-status.json"
# The only files the server reads from the data directory. A plan, logs, or a
# collector's scratch files may sit beside them and are never served.
SERVED = {STATUS_FILE: "application/json", "state.json": "application/json"}
ORCHESTRA_FILE = "orchestra.json"
TERMINAL_TASK_STATES = frozenset({"done", "cancelled"})
# Open tasks quiet this long are folded away on the page, not dropped.
RECENT_TASK_SECONDS = 24 * 3600


def default_dir() -> Path:
    return state_root() / "podium"


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _http_url(value: Any) -> bool:
    return isinstance(value, str) and urlparse(value).scheme in {"http", "https"}


def _deployments(report: dict[str, Any]) -> list[Any]:
    """`heap` is the single-deployment block of the first Podium; it still validates."""
    if "deployments" in report:
        rows = report["deployments"]
        if not isinstance(rows, list):
            raise OrchestraError("deployments must be a list")
        return rows
    heap = report.get("heap")
    return [] if heap is None else [heap]


def validate(report: Any) -> int:
    """Raise OrchestraError on the first problem; return the queue item count."""
    if not isinstance(report, dict) or not isinstance(report.get("members"), list):
        raise OrchestraError("report requires a members list")
    if not _text(report.get("summary")):
        raise OrchestraError("report requires a readable summary")
    seen: set[str] = set()
    count = 0
    for member in report["members"]:
        if not isinstance(member, dict):
            raise OrchestraError("each member must be an object")
        for field in ("id", "name", "role", "task"):
            if not _text(member.get(field)):
                raise OrchestraError(f"member requires {field}")
        name = member["name"]
        if member["id"] in seen:
            raise OrchestraError(f"duplicate member: {member['id']}")
        seen.add(member["id"])
        reported = member.get("reported_at")
        if (
            isinstance(reported, bool)
            or not isinstance(reported, (int, float))
            or not math.isfinite(reported)
            or reported <= 0
        ):
            raise OrchestraError(f"{name}: evidence reported_at is required")
        for link in member.get("links") or []:
            if not isinstance(link, dict) or not _text(link.get("label")) or not _http_url(link.get("url")):
                raise OrchestraError(f"{name}: links need a label and an http or https url")
        if not isinstance(member.get("queue"), list):
            raise OrchestraError(f"{name}: explicit queue required (may be empty)")
        for item in member["queue"]:
            if not isinstance(item, dict):
                raise OrchestraError(f"{name}: each queue item must be an object")
            for field in ("item", "activity", "stage", "eta"):
                if not _text(item.get(field)):
                    raise OrchestraError(f"{name}: queue item requires {field}")
            if item.get("status") not in QUEUE_STATES:
                raise OrchestraError(f"{name}: invalid queue status {item.get('status')!r}")
            if item["status"] == "blocked" and not _text(item.get("blocker")):
                raise OrchestraError(f"{name}: blocked item requires a reason")
            if item.get("url") and not _http_url(item["url"]):
                raise OrchestraError(f"{name}: queue links must use http or https")
            count += 1
    for deployment in _deployments(report):
        if not isinstance(deployment, dict) or not _text(deployment.get("summary")):
            raise OrchestraError("each deployment requires a summary")
    for note in report.get("notes") or []:
        if not isinstance(note, dict) or not _text(note.get("title")) or not _text(note.get("body")):
            raise OrchestraError("each note requires a title and a body")
    return count


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON a browser can parse")


def publish(source: Path, directory: Path, *, check: bool = False) -> dict[str, Any]:
    try:
        report = json.loads(Path(source).read_text(), parse_constant=_reject_constant)
    except (OSError, ValueError) as exc:
        raise OrchestraError(f"cannot read {source}: {exc}") from exc
    count = validate(report)
    result: dict[str, Any] = {"items": count, "members": len(report["members"]), "published": None}
    if check:
        return result
    # Publishing time is not evidence time: each member keeps its reported_at.
    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    output = Path(directory) / STATUS_FILE
    atomic_write_json(output, report)
    result["published"] = str(output)
    return result


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the bundled page and two named data files, never caching.

    A browser that revalidates by Last-Modified keeps rendering an old page
    after an upgrade while the data stays current, which looks like a layout
    bug. no-store and no Last-Modified stop that.
    """

    directory: Path
    member_id: str | None = None

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._respond(body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._respond(body=False)

    def _respond(self, *, body: bool) -> None:
        name = urlparse(self.path).path.lstrip("/") or "index.html"
        try:
            if name == "index.html":
                payload, kind = PAGE.read_bytes(), "text/html; charset=utf-8"
            elif name in SERVED:
                payload, kind = (self.directory / name).read_bytes(), SERVED[name]
            elif name == ORCHESTRA_FILE and self.member_id:
                view = orchestra_view(self.member_id)
                payload, kind = json.dumps(view, allow_nan=False).encode(), "application/json"
            else:
                raise FileNotFoundError(name)
        except (OSError, OrchestraError):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if not self.path.split("?")[0].endswith(".json"):  # the page polls every 15s
            super().log_message(format, *args)


def server(
    directory: Path, port: int = DEFAULT_PORT, member_id: str | None = None
) -> http.server.ThreadingHTTPServer:
    """Loopback only: the page shows task names, PR titles and member ids."""
    handler = type("PodiumHandler", (_Handler,), {"directory": Path(directory), "member_id": member_id})
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise OrchestraError(f"cannot listen on 127.0.0.1:{port}: {exc}") from exc


def serve(directory: Path, port: int = DEFAULT_PORT, member_id: str | None = None) -> None:
    with server(directory, port, member_id) as httpd:
        print(f"Podium serving {directory} on http://127.0.0.1:{httpd.server_address[1]}/", flush=True)
        httpd.serve_forever()


def _age(moment: Any, at: float) -> float | None:
    try:
        value = float(moment)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, at - value), 1) if value else None


def _last_activity(task: dict[str, Any]) -> float:
    moments = [task.get("created_at"), (task.get("latest") or {}).get("sent_at")]
    for owner in task.get("owners") or []:
        if isinstance(owner, dict):
            moments += [owner.get("state_at"), owner.get("last_report_at")]
    return max((float(m) for m in moments if isinstance(m, (int, float))), default=0.0)


def orchestra_view(member_id: str) -> dict[str, Any]:
    """Roster and open tasks as this member's monitor last saw them.

    Every age is computed now, from the hub's own timestamps, and the snapshot
    age is reported beside it: a monitor that stopped leaves an old snapshot,
    and the page says so instead of showing it as current.
    """
    from . import member as member_api

    member = member_api.load_member(member_id)
    at = now()
    snapshot = member_api.hub_snapshot(member)
    view: dict[str, Any] = {
        "member_id": member_id,
        "orchestra_id": member.get("orchestra_id"),
        "closed": bool(member.get("closed_at")),
        "monitor_alive": member_api.monitor_alive(member),
        "roster": None,
        "tasks": None,
    }
    roster = snapshot.get("roster")
    conductor_id = member.get("conductor_id")
    if isinstance(roster, dict):
        conductor_id = roster.get("conductor_id", conductor_id)
        rows = [member_api._presence_summary(row) for row in roster.get("members") or [] if isinstance(row, dict)]
        for row in rows:
            row["is_conductor"] = row.get("id") == conductor_id
        view["roster"] = {
            "fetched_at": roster.get("fetched_at"),
            "age": _age(roster.get("fetched_at"), at),
            "conductor_id": conductor_id,
            "members": rows,
        }
    tasks = snapshot.get("tasks")
    if isinstance(tasks, dict):
        rows = [row for row in tasks.get("tasks") or [] if isinstance(row, dict)]
        attention = member_api.task_attention({**member, "conductor_id": conductor_id}, rows, at=at)
        open_rows = []
        for row in rows:
            if row.get("state") in TERMINAL_TASK_STATES:
                continue
            last = _last_activity(row)
            open_rows.append({
                "task": row.get("task"),
                "state": row.get("state"),
                "sender": row.get("sender"),
                "created_at": row.get("created_at"),
                "last_activity_age": _age(last, at),
                "recent": at - last <= RECENT_TASK_SECONDS,
                "owners": [
                    {key: owner.get(key) for key in ("id", "state", "delivery", "state_at", "last_report_at")}
                    for owner in row.get("owners") or [] if isinstance(owner, dict)
                ],
            })
        open_rows.sort(key=lambda row: row["last_activity_age"] if row["last_activity_age"] is not None else math.inf)
        view["tasks"] = {
            "fetched_at": tasks.get("fetched_at"),
            "age": _age(tasks.get("fetched_at"), at),
            "open": open_rows,
            "closed_count": len(rows) - len(open_rows),
            "attention": attention,
            "lifecycle": "derived" if all(isinstance(row.get("owners"), list) for row in rows) else "unavailable",
        }
    return view


# ---- background server ------------------------------------------------------


def _record_path(member_id: str) -> Path:
    return runtime_dir() / f"{member_id}.podium.json"


def _pid_alive(pid: int) -> bool:
    from .member import _pid_alive as alive

    return alive(pid)


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def status(member_id: str) -> dict[str, Any]:
    try:
        record = read_json(_record_path(member_id))
    except OrchestraError:
        return {"running": False, "member_id": member_id}
    pid, port = int(record.get("pid") or 0), int(record.get("port") or 0)
    running = _pid_alive(pid) and _listening(port)
    return {
        **record,
        "running": running,
        # A server started before an upgrade keeps serving the old page.
        "stale_version": running and record.get("version") != __version__,
    }


def stop(member_id: str) -> dict[str, Any]:
    current = status(member_id)
    pid = int(current.get("pid") or 0)
    if current.get("running") or _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    _record_path(member_id).unlink(missing_ok=True)
    return {"member_id": member_id, "stopped": bool(pid), "pid": pid or None}


def start(member: dict[str, Any], directory: Path, port: int = DEFAULT_PORT, *, restart: bool = False) -> dict[str, Any]:
    """One background server per membership; a second start reports the first."""
    from . import member as member_api

    member_id = str(member["member_id"])
    current = status(member_id)
    if current["running"] and not restart:
        return {**current, "state": "already-running"}
    if restart or current.get("pid"):
        stop(member_id)
    if _listening(port):
        raise OrchestraError(f"127.0.0.1:{port} is already in use. Pass --port to choose another port.")
    log = runtime_dir() / f"{member_id}.podium.log"
    pid = member_api._spawn_module(
        ["podium", "serve", "--member-id", member_id, "--dir", str(directory), "--port", str(port)], log
    )
    deadline = time.monotonic() + 5
    while not _listening(port):
        if not _pid_alive(pid) or time.monotonic() >= deadline:
            tail = log.read_text(errors="replace").strip().splitlines()[-1:] if log.exists() else []
            raise OrchestraError(
                f"Podium did not start on 127.0.0.1:{port}"
                + (f": {tail[0]}" if tail else "")
                + ". Pass --port to choose another port."
            )
        time.sleep(0.05)
    record = {
        "member_id": member_id,
        "pid": pid,
        "port": port,
        "url": f"http://127.0.0.1:{port}/",
        "dir": str(directory),
        "started_at": now(),
        "version": __version__,
        "log": str(log),
    }
    atomic_write_json(_record_path(member_id), record)
    return {**record, "running": True, "state": "started"}
