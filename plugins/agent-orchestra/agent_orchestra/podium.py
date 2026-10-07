"""The Podium: a loopback status page for the conductor's explicit work queues.

The conductor curates a work plan by hand: one ordered queue per member, each
item with an activity, a stage, a blocker or next action, and an ETA. publish()
validates that plan and writes work-status.json atomically; serve() shows it.
Nothing here infers ownership, progress, or an ETA from a PR link or a
heartbeat. A connected session does not prove that a task is running.

An external collector may also drop state.json beside work-status.json. The
page renders its sections when it is present; its shape belongs to that
collector, not to this module.
"""
from __future__ import annotations

import http.server
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .core import OrchestraError, atomic_write_json, state_root

QUEUE_STATES = frozenset({"in_progress", "queued", "blocked", "waiting", "completed", "accepted", "review"})
DEFAULT_PORT = 4300
PAGE = Path(__file__).with_name("podium.html")
STATUS_FILE = "work-status.json"
# The only files the server reads from the data directory. A plan, logs, or a
# collector's scratch files may sit beside them and are never served.
SERVED = {STATUS_FILE: "application/json", "state.json": "application/json"}


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
            else:
                raise FileNotFoundError(name)
        except OSError:
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


def server(directory: Path, port: int = DEFAULT_PORT) -> http.server.ThreadingHTTPServer:
    """Loopback only: the page shows task names, PR titles and member ids."""
    handler = type("PodiumHandler", (_Handler,), {"directory": Path(directory)})
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        raise OrchestraError(f"cannot listen on 127.0.0.1:{port}: {exc}") from exc


def serve(directory: Path, port: int = DEFAULT_PORT) -> None:
    with server(directory, port) as httpd:
        print(f"Podium serving {directory} on http://127.0.0.1:{httpd.server_address[1]}/", flush=True)
        httpd.serve_forever()
