from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .core import (
    APIError,
    MAX_MESSAGE_BYTES,
    MESSAGE_ID_RE,
    OrchestraError,
    atomic_write_json,
    certificate_fingerprint,
    encode_invite,
    hub_dir,
    new_member_id,
    new_message_id,
    new_orchestra_id,
    now,
    read_json,
    runtime_dir,
    safe_id,
    secret_hash,
    secret_matches,
    state_root,
    token,
)
from .protocol import ProtocolError, validate_fields


PRESENCE_INTERVAL_SECONDS = 10.0
PRESENCE_STALE_SECONDS = 120.0
PRUNE_INTERVAL_SECONDS = 600.0
PRUNE_MESSAGE_SECONDS = 7 * 24 * 60 * 60
PRUNE_SYSTEM_MESSAGE_SECONDS = 24 * 60 * 60
PRUNE_INVITE_SECONDS = 24 * 60 * 60
DEFAULT_INVITE_TTL_SECONDS = 3600
MIN_INVITE_TTL_SECONDS = 60
MAX_INVITE_TTL_SECONDS = 86400
MAX_WAIT_SECONDS = 30.0
READY_TIMEOUT_SECONDS = 10.0
# A closed hub keeps serving 410s so every member learns the orchestra ended.
CLOSE_GRACE_SECONDS = 3600.0
CLOSE_CHECK_INTERVAL_SECONDS = 10.0
CERTIFICATE_DAYS = 365
# One long poll may hold 50 rows of MAX_MESSAGE_BYTES each; cap the response so
# a member with a full queue never has to read 12 MiB in one answer.
PENDING_RESPONSE_BYTES = 1024 * 1024
# A JSON body carrying a MAX_MESSAGE_BYTES text escapes to more than its own
# size, so the request cap has to leave room for the escaping.
MAX_REQUEST_BYTES = 3 * MAX_MESSAGE_BYTES + 4096
# The handler, not the accept loop, waits for a ClientHello.
HANDSHAKE_TIMEOUT_SECONDS = 10.0
LIVENESS_PROBE_SECONDS = 1.0
MAX_NAME_CHARACTERS = 80

SYSTEM_SENDER = "sys"

# The production entry point for the detached hub process. Tests may point this
# at an in-tree runner while the CLI module is still being written.
_SPAWN_ENTRY: tuple[str, ...] = ("-m", "agent_orchestra")
_BACKGROUND_PROCESSES: list[subprocess.Popen[bytes]] = []

_MESSAGE_ID = re.compile(MESSAGE_ID_RE)
_MESSAGE_PATH = re.compile(r"/v1/messages/(m_[A-Za-z0-9_-]+)")
_MESSAGE_MARK_PATH = re.compile(r"/v1/messages/(m_[A-Za-z0-9_-]+)/(ack|handled)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestra (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS members (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
  role TEXT NOT NULL,
  parent TEXT,
  token_hash TEXT NOT NULL,
  joined_at REAL NOT NULL, last_seen_at REAL NOT NULL,
  presence TEXT NOT NULL DEFAULT 'connected',
  presence_changed_at REAL NOT NULL,
  revoked_at REAL, revoked_reason TEXT);
CREATE TABLE IF NOT EXISTS invites (
  secret_hash TEXT PRIMARY KEY, role TEXT NOT NULL, parent TEXT, name TEXT,
  issued_by TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  used_at REAL, used_by TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, sender TEXT NOT NULL,
  act TEXT NOT NULL, re TEXT, task TEXT, need TEXT NOT NULL, refs TEXT NOT NULL,
  sent_at REAL NOT NULL, body TEXT, body_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (
  message_id TEXT NOT NULL, recipient TEXT NOT NULL,
  state TEXT NOT NULL,
  queued_at REAL NOT NULL, delivered_at REAL, handled_at REAL,
  PRIMARY KEY (message_id, recipient));
CREATE INDEX IF NOT EXISTS deliveries_by_recipient ON deliveries (recipient, state);
CREATE TABLE IF NOT EXISTS tasks (
  task TEXT PRIMARY KEY, message_id TEXT NOT NULL, sender TEXT NOT NULL,
  recipients TEXT NOT NULL,
  created_at REAL NOT NULL);
"""


def public_member(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "role": row["role"],
        "parent": row["parent"],
        "joined_at": row["joined_at"],
        "last_seen_at": row["last_seen_at"],
        "presence": row["presence"],
        "presence_changed_at": row["presence_changed_at"],
        "revoked_at": row["revoked_at"],
        "revoked_reason": row["revoked_reason"],
    }


def system_sender() -> dict[str, Any]:
    return {"id": SYSTEM_SENDER, "name": "hub", "role": "sys"}


def _principal_id(principal: dict[str, Any]) -> str | None:
    return principal.get("id") if principal.get("kind") == "member" else None


def sanitize_name(value: Any, fallback: str = "member") -> str:
    """One printable line, at most MAX_NAME_CHARACTERS. A name reaches other
    members inside a one-line system event, so a newline in it would forge a
    second event line for everyone."""
    raw = "" if value is None else str(value)
    kept = "".join(char for char in raw if char.isprintable() or char.isspace())
    collapsed = " ".join(kept.split())
    return collapsed[:MAX_NAME_CHARACTERS].strip() or fallback


def clamp_ttl(ttl: Any) -> int:
    try:
        value = int(ttl)
    except (TypeError, ValueError):
        value = DEFAULT_INVITE_TTL_SECONDS
    return max(MIN_INVITE_TTL_SECONDS, min(value, MAX_INVITE_TTL_SECONDS))


class HubStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.executescript(SCHEMA)
            self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def disconnect(self) -> None:
        """Close the SQLite handle. The orchestra-level close is close()."""
        with self.lock:
            self.db.close()

    # ---- orchestra metadata -------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM orchestra WHERE key=?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def _set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO orchestra (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    def initialize(
        self,
        *,
        orchestra_id: str,
        name: str,
        admin_token_hash: str,
        endpoints: list[str],
        fingerprint: str,
    ) -> None:
        with self.lock:
            created = self._get("created_at")
            self._set("orchestra_id", orchestra_id)
            self._set("name", name)
            self._set("created_at", created if created else now())
            self._set("admin_token_hash", admin_token_hash)
            self._set("endpoints", list(endpoints))
            self._set("fingerprint", fingerprint)
            if self._get("closed_at", None) is None:
                self._set("closed_at", None)
            if self._get("conductor_id", None) is None:
                self._set("conductor_id", None)
            self.db.commit()

    def refresh_hub(self, *, name: str, endpoints: list[str], fingerprint: str) -> None:
        with self.lock:
            self._set("name", name)
            self._set("endpoints", list(endpoints))
            self._set("fingerprint", fingerprint)
            self.db.commit()

    def hub_info(self) -> dict[str, Any]:
        with self.lock:
            return {
                "name": self._get("name", "orchestra"),
                "endpoints": list(self._get("endpoints", []) or []),
                "fingerprint": self._get("fingerprint", ""),
            }

    def _live(self, member_id: str | None = None) -> None:
        if self._get("closed_at") is None:
            return
        if member_id:
            # A request that authenticated before the close (a parked long poll)
            # still counts as this member having been told, so record it here as
            # authenticate() does for every later call.
            self.db.execute("UPDATE members SET last_seen_at=? WHERE id=?", (now(), member_id))
            self.db.commit()
        raise APIError(410, "Orchestra is closed")

    # ---- member helpers -----------------------------------------------------

    def _members(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM members"
        if active_only:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY joined_at ASC"
        return [dict(row) for row in self.db.execute(sql)]

    def _member(self, member_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
        return dict(row) if row else None

    def _active_member(self, member_id: str) -> dict[str, Any] | None:
        row = self._member(member_id)
        if row and row["revoked_at"] is None:
            return row
        return None

    def _conductor_id(self) -> str | None:
        conductor_id = self._get("conductor_id")
        if not conductor_id:
            return None
        return conductor_id if self._active_member(conductor_id) else None

    # ---- system events ------------------------------------------------------

    def _emit(
        self, body: str, subject: str | None, *, include_subject: bool = False
    ) -> str | None:
        recipients = [
            row["id"]
            for row in self._members(active_only=True)
            if include_subject or row["id"] != subject
        ]
        if not recipients:
            return None
        message_id = new_message_id()
        moment = now()
        self.db.execute(
            "INSERT INTO messages (id, sender, act, re, task, need, refs, sent_at, body, body_sha256)"
            " VALUES (?, ?, 'tell', NULL, NULL, 'none', '[]', ?, ?, ?)",
            (message_id, SYSTEM_SENDER, moment, body, secret_hash(body)),
        )
        self.db.executemany(
            "INSERT INTO deliveries (message_id, recipient, state, queued_at) VALUES (?, ?, 'queued', ?)",
            [(message_id, recipient, moment) for recipient in recipients],
        )
        return message_id

    # ---- principals ---------------------------------------------------------

    def authenticate(self, header: str | None) -> dict[str, Any]:
        if not header or not header.startswith("Bearer "):
            raise APIError(401, "Missing bearer token")
        value = header[7:].strip()
        with self.changed:
            if secret_matches(value, self._get("admin_token_hash")):
                return {"kind": "admin"}
            row = None
            for candidate in self._members():
                if secret_matches(value, candidate["token_hash"]):
                    row = candidate
                    break
            if row is None:
                raise APIError(401, "Invalid bearer token")
            if self._get("closed_at") is not None:
                # The closed hub records who has been told before refusing, so
                # the shutdown thread knows when every member has seen the 410.
                self.db.execute(
                    "UPDATE members SET last_seen_at=? WHERE id=?", (now(), row["id"])
                )
                self.db.commit()
                raise APIError(410, "Orchestra is closed")
            if row["revoked_at"] is not None:
                reason = row["revoked_reason"] or "left"
                raise APIError(410, f"Membership revoked ({reason})")
            moment = now()
            if row["presence"] != "connected":
                absent_since = row["last_seen_at"]
                self.db.execute(
                    "UPDATE members SET last_seen_at=?, presence='connected', presence_changed_at=? WHERE id=?",
                    (moment, moment, row["id"]),
                )
                self._emit(
                    f"presence {row['id']} {row['name']} connected absent_since={absent_since}",
                    row["id"],
                    # The returning member is told too, so a conductor coming
                    # back reads its own absent_since.
                    include_subject=True,
                )
                self.db.commit()
                self.changed.notify_all()
            else:
                self.db.execute(
                    "UPDATE members SET last_seen_at=? WHERE id=?", (moment, row["id"])
                )
                self.db.commit()
            fresh = self._member(row["id"]) or row
            return {"kind": "member", **fresh}

    # ---- join ---------------------------------------------------------------

    def join(self, secret: str, name: str, provider: str) -> dict[str, Any]:
        clean_name = sanitize_name(name)
        clean_provider = (str(provider).strip()[:40]) or "cli"
        with self.changed:
            self._live()
            digest = secret_hash(str(secret))
            row = self.db.execute(
                "SELECT * FROM invites WHERE secret_hash=?", (digest,)
            ).fetchone()
            if row is None or row["used_at"] is not None or float(row["expires_at"]) <= now():
                raise APIError(403, "Invite secret is invalid, used, or expired")
            role = row["role"]
            if role == "conductor" and self._conductor_id():
                raise APIError(409, "Orchestra already has a conductor")
            member_id = new_member_id()
            member_token = token(32)
            moment = now()
            # A conductor has no parent, so an invite that carries one (minted
            # by the member who then handed the seat over) never stores it.
            parent = None if role == "conductor" else row["parent"]
            self.db.execute(
                "INSERT INTO members (id, name, provider, role, parent, token_hash, joined_at,"
                " last_seen_at, presence, presence_changed_at, revoked_at, revoked_reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'connected', ?, NULL, NULL)",
                (
                    member_id,
                    sanitize_name(row["name"] or clean_name),
                    clean_provider,
                    role,
                    parent,
                    secret_hash(member_token),
                    moment,
                    moment,
                    moment,
                ),
            )
            self.db.execute(
                "UPDATE invites SET used_at=?, used_by=? WHERE secret_hash=?",
                (moment, member_id, digest),
            )
            if role == "conductor":
                self._set("conductor_id", member_id)
            member = self._member(member_id) or {}
            self._emit(f"presence {member_id} {member['name']} joined", member_id)
            self.db.commit()
            self.changed.notify_all()
            info = self.hub_info()
            return {
                "orchestra_id": self._get("orchestra_id"),
                "member_id": member_id,
                "token": member_token,
                "role": role,
                "parent": member["parent"],
                "conductor_id": self._conductor_id(),
                "name": member["name"],
                "hub": info,
            }

    # ---- read surfaces ------------------------------------------------------

    def status(self, principal: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            is_member = principal.get("kind") == "member"
            member_id = principal.get("id") if is_member else None
            queued = 0
            sent: list[dict[str, Any]] = []
            if member_id:
                queued = int(
                    self.db.execute(
                        "SELECT COUNT(*) AS total FROM deliveries"
                        " JOIN messages ON messages.id = deliveries.message_id"
                        " WHERE deliveries.recipient=? AND deliveries.state='queued'"
                        " AND messages.sender<>?",
                        (member_id, SYSTEM_SENDER),
                    ).fetchone()["total"]
                )
                rows = self.db.execute(
                    "SELECT id FROM messages WHERE sender=? ORDER BY sent_at DESC LIMIT 100",
                    (member_id,),
                ).fetchall()
                sent = [self._message_status(row["id"]) for row in reversed(rows)]
            current = self._member(member_id) if member_id else None
            return {
                "orchestra_id": self._get("orchestra_id"),
                "name": self._get("name"),
                "closed_at": self._get("closed_at"),
                "conductor_id": self._conductor_id(),
                "self": public_member(current) if current else None,
                "members": [public_member(row) for row in self._members()],
                "queued_for_me": queued,
                "sent": sent,
            }

    def members(self) -> dict[str, Any]:
        with self.lock:
            return {
                "conductor_id": self._conductor_id(),
                "members": [public_member(row) for row in self._members()],
            }

    def tasks(self) -> dict[str, Any]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM tasks ORDER BY created_at ASC").fetchall()
            result = []
            for row in rows:
                latest = self.db.execute(
                    "SELECT id, act, sender, sent_at FROM messages WHERE task=? AND id<>?"
                    " ORDER BY sent_at DESC LIMIT 1",
                    (row["task"], row["message_id"]),
                ).fetchone()
                result.append(
                    {
                        "task": row["task"],
                        "message_id": row["message_id"],
                        "sender": row["sender"],
                        "recipients": json.loads(row["recipients"]),
                        "created_at": row["created_at"],
                        "latest": dict(latest) if latest else None,
                    }
                )
            return {"tasks": result}

    # ---- invites ------------------------------------------------------------

    def invite(self, principal: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        role = str(payload.get("role") or "player")
        if role not in {"player", "conductor"}:
            raise APIError(400, "Role must be player or conductor")
        parent = payload.get("parent")
        name = payload.get("name")
        ttl = clamp_ttl(payload.get("ttl", DEFAULT_INVITE_TTL_SECONDS))
        is_admin = principal.get("kind") == "admin"
        with self.changed:
            self._live(_principal_id(principal))
            if role == "conductor":
                # A conductor has no parent, so a conductor invite never carries
                # one, whatever the caller asked for.
                parent = None
            if parent == "self":
                if is_admin:
                    raise APIError(400, "The hub admin has no self to use as a parent")
                parent = principal["id"]
            if parent is not None:
                parent = str(parent)
                if not self._active_member(parent):
                    raise APIError(400, f"Unknown parent: {parent}")
            if not is_admin:
                is_conductor = principal.get("role") == "conductor"
                if not is_conductor and (role != "player" or parent != principal["id"]):
                    raise APIError(403, "A player may only invite its own child")
            if role == "player" and parent is None:
                parent = self._conductor_id()
            secret = token(32)
            moment = now()
            expires_at = moment + ttl
            issued_by = "admin" if is_admin else principal["id"]
            self.db.execute(
                "INSERT INTO invites (secret_hash, role, parent, name, issued_by, created_at,"
                " expires_at, used_at, used_by) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    secret_hash(secret),
                    role,
                    parent,
                    sanitize_name(name, fallback="") or None,
                    issued_by,
                    moment,
                    expires_at,
                ),
            )
            self.db.commit()
            info = self.hub_info()
            invite = encode_invite(
                {
                    "orchestra_id": self._get("orchestra_id"),
                    "endpoints": info["endpoints"],
                    "fingerprint": info["fingerprint"],
                    "secret": secret,
                    "expires_at": expires_at,
                    "role": role,
                    "parent": parent,
                    "hub": {"name": info["name"]},
                }
            )
            return {
                "invite": invite,
                "role": role,
                "parent": parent,
                "expires_at": expires_at,
            }

    # ---- messages -----------------------------------------------------------

    def send(self, member: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        act = payload.get("act")
        to = payload.get("to")
        reference = payload.get("re")
        task = payload.get("task")
        need = payload.get("need") or "none"
        refs = payload.get("refs")
        refs = [] if refs is None else refs
        try:
            validate_fields(act, to, reference, task, need, refs)
        except ProtocolError as exc:
            raise APIError(400, str(exc)) from exc
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise APIError(400, "Message text must not be empty")
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise APIError(413, f"Message exceeds {MAX_MESSAGE_BYTES} bytes")
        message_id = str(payload.get("id") or new_message_id())
        if not _MESSAGE_ID.fullmatch(message_id):
            raise APIError(400, "Invalid message id")

        digest = secret_hash(text)
        with self.changed:
            self._live(member.get("id"))
            existing = self.db.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing is not None:
                if existing["sender"] != member["id"] or existing["body_sha256"] != digest:
                    raise APIError(409, "Message id is already in use")
                return self._message_status(message_id)
            recipients = self._resolve(member, list(to))
            moment = now()
            if act == "assign":
                try:
                    self.db.execute(
                        "INSERT INTO tasks (task, message_id, sender, recipients, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (task, message_id, member["id"], json.dumps(recipients), moment),
                    )
                except sqlite3.IntegrityError as exc:
                    self.db.rollback()
                    raise APIError(409, f"Task already assigned: {task}") from exc
            self.db.execute(
                "INSERT INTO messages (id, sender, act, re, task, need, refs, sent_at, body, body_sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    member["id"],
                    act,
                    reference,
                    task,
                    need,
                    json.dumps(list(refs)),
                    moment,
                    text,
                    digest,
                ),
            )
            self.db.executemany(
                "INSERT INTO deliveries (message_id, recipient, state, queued_at)"
                " VALUES (?, ?, 'queued', ?)",
                [(message_id, recipient, moment) for recipient in recipients],
            )
            self.db.commit()
            self.changed.notify_all()
            return {
                "id": message_id,
                "state": "queued",
                "sent_at": moment,
                "recipients": [{"id": recipient, "state": "queued"} for recipient in recipients],
            }

    def _effective_parent(
        self,
        member: dict[str, Any],
        active: dict[str, dict[str, Any]],
        conductor_id: str | None,
    ) -> str | None:
        """The member that `parent`, `children`, and `siblings` all treat as
        this member's parent. A stored parent counts only while it is active;
        otherwise a player falls back to the conductor, so a handover never
        strands anyone under the member who left. The conductor has no parent."""
        if member["id"] == conductor_id:
            return None
        parent = member.get("parent")
        if parent and parent in active:
            return parent
        return conductor_id

    def _resolve(self, sender: dict[str, Any], tokens: list[str]) -> list[str]:
        active = {row["id"]: row for row in self._members(active_only=True)}
        sender_id = sender["id"]
        conductor_id = self._conductor_id()
        sender_parent = self._effective_parent(sender, active, conductor_id)
        resolved: list[str] = []

        def add(member_id: str) -> None:
            if member_id and member_id not in resolved:
                resolved.append(member_id)

        for raw in tokens:
            entry = str(raw).strip()
            if entry == "conductor":
                if not conductor_id:
                    raise APIError(409, "No conductor")
                if conductor_id == sender_id:
                    raise APIError(400, "You are the conductor")
                add(conductor_id)
            elif entry == "parent":
                if not sender_parent:
                    raise APIError(409, "No parent")
                add(sender_parent)
            elif entry == "children":
                for member_id, row in active.items():
                    if member_id == sender_id:
                        continue
                    if self._effective_parent(row, active, conductor_id) == sender_id:
                        add(member_id)
            elif entry == "siblings":
                for member_id, row in active.items():
                    if member_id == sender_id:
                        continue
                    if self._effective_parent(row, active, conductor_id) == sender_parent:
                        add(member_id)
            elif entry == "all":
                for member_id in active:
                    if member_id != sender_id:
                        add(member_id)
            else:
                if entry == sender_id or entry not in active:
                    raise APIError(400, f"Unknown recipient: {entry}")
                add(entry)
        if not resolved:
            raise APIError(400, "No recipients resolved")
        return resolved

    def pending(self, member: dict[str, Any], wait_seconds: float, limit: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        capped = max(1, min(int(limit), 100))
        with self.changed:
            while True:
                self._live(member["id"])
                rows = self.db.execute(
                    "SELECT m.* FROM deliveries d JOIN messages m ON m.id = d.message_id"
                    " WHERE d.recipient=? AND d.state='queued' ORDER BY m.sent_at ASC LIMIT ?",
                    (member["id"], capped),
                ).fetchall()
                if rows:
                    return self._page(rows)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.changed.wait(timeout=min(1.0, remaining))

    def _page(self, rows: list[Any]) -> list[dict[str, Any]]:
        """As many envelopes as fit in PENDING_RESPONSE_BYTES, and always at
        least one, so a single oversized row still reaches its member."""
        envelopes: list[dict[str, Any]] = []
        total = 2  # the enclosing JSON list
        for row in rows:
            envelope = self._envelope(dict(row))
            size = len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) + 1
            if envelopes and total + size > PENDING_RESPONSE_BYTES:
                break
            envelopes.append(envelope)
            total += size
        return envelopes

    def _envelope(self, message: dict[str, Any]) -> dict[str, Any]:
        if message["sender"] == SYSTEM_SENDER:
            sender: dict[str, Any] = system_sender()
        else:
            row = self._member(message["sender"])
            sender = public_member(row) if row else {"id": message["sender"], "name": message["sender"]}
        return {
            "id": message["id"],
            "from": sender,
            "act": message["act"],
            "re": message["re"],
            "task": message["task"],
            "need": message["need"],
            "refs": json.loads(message["refs"]),
            "sent_at": message["sent_at"],
            "text": message["body"],
        }

    def mark(self, member: dict[str, Any], message_id: str, target: str) -> dict[str, Any]:
        with self.changed:
            self._live(member.get("id"))
            message = self.db.execute(
                "SELECT id FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if message is None:
                raise APIError(404, "Message not found")
            row = self.db.execute(
                "SELECT * FROM deliveries WHERE message_id=? AND recipient=?",
                (message_id, member["id"]),
            ).fetchone()
            if row is None:
                raise APIError(403, "Only a recipient can acknowledge this message")
            moment = now()
            if target == "delivered" and row["state"] == "queued":
                self.db.execute(
                    "UPDATE deliveries SET state='delivered', delivered_at=?"
                    " WHERE message_id=? AND recipient=?",
                    (moment, message_id, member["id"]),
                )
            elif target == "handled" and row["state"] in {"queued", "delivered"}:
                self.db.execute(
                    "UPDATE deliveries SET state='handled', handled_at=?,"
                    " delivered_at=COALESCE(delivered_at, ?) WHERE message_id=? AND recipient=?",
                    (moment, moment, message_id, member["id"]),
                )
            outstanding = self.db.execute(
                "SELECT COUNT(*) AS total FROM deliveries WHERE message_id=? AND state='queued'",
                (message_id,),
            ).fetchone()["total"]
            if not outstanding:
                self.db.execute("UPDATE messages SET body=NULL WHERE id=?", (message_id,))
            self.db.commit()
            self.changed.notify_all()
            updated = self.db.execute(
                "SELECT * FROM deliveries WHERE message_id=? AND recipient=?",
                (message_id, member["id"]),
            ).fetchone()
            return {
                "id": message_id,
                "recipient": member["id"],
                "state": updated["state"],
                "delivered_at": updated["delivered_at"],
                "handled_at": updated["handled_at"],
            }

    def message_status(self, principal: dict[str, Any], message_id: str) -> dict[str, Any]:
        with self.lock:
            message = self.db.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if message is None:
                raise APIError(404, "Message not found")
            if principal.get("kind") != "admin":
                member_id = principal["id"]
                allowed = message["sender"] == member_id
                if not allowed:
                    allowed = (
                        self.db.execute(
                            "SELECT 1 FROM deliveries WHERE message_id=? AND recipient=?",
                            (message_id, member_id),
                        ).fetchone()
                        is not None
                    )
                if not allowed:
                    raise APIError(403, "Only the sender or a recipient may read this message")
            return self._message_status(message_id)

    def _message_status(self, message_id: str) -> dict[str, Any]:
        message = self.db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if message is None:
            raise APIError(404, "Message not found")
        rows = self.db.execute(
            "SELECT * FROM deliveries WHERE message_id=? ORDER BY recipient ASC",
            (message_id,),
        ).fetchall()
        recipients = [
            {
                "id": row["recipient"],
                "state": row["state"],
                "delivered_at": row["delivered_at"],
                "handled_at": row["handled_at"],
            }
            for row in rows
        ]
        states = {row["state"] for row in recipients}
        state = "queued" if "queued" in states else ("delivered" if "delivered" in states else "handled")
        return {
            "id": message["id"],
            "act": message["act"],
            "task": message["task"],
            "re": message["re"],
            "need": message["need"],
            "sent_at": message["sent_at"],
            "state": state,
            "recipients": recipients,
        }

    # ---- membership changes -------------------------------------------------

    def heartbeat(self, member: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            moment = now()
            self.db.execute("UPDATE members SET last_seen_at=? WHERE id=?", (moment, member["id"]))
            self.db.commit()
            return {"ok": True, "at": moment}

    def leave(self, member: dict[str, Any]) -> dict[str, Any]:
        with self.changed:
            self._live(member.get("id"))
            moment = now()
            self._revoke(member["id"], "left", moment)
            self.db.commit()
            self.changed.notify_all()
            return {"ok": True, "left_at": moment}

    def kick(self, principal: dict[str, Any], member_id: str, reason: Any = None) -> dict[str, Any]:
        with self.changed:
            self._live(_principal_id(principal))
            self._require_command(principal)
            if principal.get("kind") == "member" and principal["id"] == member_id:
                raise APIError(400, "Use leave to remove yourself")
            if not self._active_member(member_id):
                raise APIError(400, f"Unknown or inactive member: {member_id}")
            self._revoke(member_id, "kicked", now())
            self.db.commit()
            self.changed.notify_all()
            return {"ok": True, "member_id": member_id, "reason": str(reason)[:200] if reason else None}

    def _revoke(self, member_id: str, reason: str, moment: float) -> None:
        row = self._active_member(member_id)
        if row is None:
            return
        self.db.execute(
            "UPDATE members SET revoked_at=?, revoked_reason=?, presence_changed_at=? WHERE id=?",
            (moment, reason, moment, member_id),
        )
        if self._get("conductor_id") == member_id:
            self._set("conductor_id", None)
        self._emit(f"presence {member_id} {row['name']} {reason}", member_id)

    def set_conductor(self, principal: dict[str, Any], member_id: str) -> dict[str, Any]:
        with self.changed:
            self._live(_principal_id(principal))
            self._require_command(principal)
            target = self._active_member(str(member_id))
            if target is None:
                raise APIError(400, f"Unknown or inactive member: {member_id}")
            previous = self._conductor_id()
            if previous and previous != target["id"]:
                self.db.execute("UPDATE members SET role='player' WHERE id=?", (previous,))
            self.db.execute("UPDATE members SET role='conductor' WHERE id=?", (target["id"],))
            self._set("conductor_id", target["id"])
            self._emit(
                f"conductor {target['id']} {target['name']}",
                target["id"],
                # The promoted member learns its new role from the same event.
                include_subject=True,
            )
            self.db.commit()
            self.changed.notify_all()
            return {"conductor_id": target["id"]}

    def close(self, principal: dict[str, Any]) -> dict[str, Any]:
        with self.changed:
            self._require_command(principal)
            closed_at = self._get("closed_at")
            if closed_at is None:
                closed_at = now()
                self._emit("closed", None)
                self._set("closed_at", closed_at)
                self._set(
                    "closed_by",
                    "admin" if principal.get("kind") == "admin" else principal["id"],
                )
                self.db.commit()
                self.changed.notify_all()
                self._mark_hub_json_closed(closed_at)
            return {"ok": True, "closed_at": closed_at}

    def closed_at(self) -> float | None:
        with self.lock:
            closed_at = self._get("closed_at")
            return None if closed_at is None else float(closed_at)

    def close_shutdown_delay(self) -> float | None:
        """None while the orchestra is open. Once closed, the seconds the hub
        must keep serving 410s: 0 as soon as every unrevoked member has called
        since `closed_at`, and never more than the remaining grace."""
        with self.lock:
            closed_at = self._get("closed_at")
            if closed_at is None:
                return None
            closed_at = float(closed_at)
            waiting = int(
                self.db.execute(
                    "SELECT COUNT(*) AS total FROM members"
                    " WHERE revoked_at IS NULL AND last_seen_at < ?",
                    (closed_at,),
                ).fetchone()["total"]
            )
            if waiting == 0:
                return 0.0
            return max(0.0, closed_at + CLOSE_GRACE_SECONDS - now())

    def wait_for_change(self, timeout: float) -> None:
        """Park on the store condition so a close wakes the watcher at once."""
        with self.changed:
            self.changed.wait(timeout)

    def _mark_hub_json_closed(self, closed_at: float) -> None:
        config_path = self.path.parent / "hub.json"
        if not config_path.exists():
            return
        try:
            config = read_json(config_path)
        except OrchestraError:
            return
        config["closed_at"] = closed_at
        atomic_write_json(config_path, config)

    def _require_command(self, principal: dict[str, Any]) -> None:
        if principal.get("kind") == "admin":
            return
        if principal.get("role") == "conductor":
            return
        raise APIError(403, "Only the conductor or the hub admin may do this")

    # ---- background sweeps --------------------------------------------------

    def sweep_presence(self, threshold: float | None = None) -> list[str]:
        limit = PRESENCE_STALE_SECONDS if threshold is None else float(threshold)
        with self.changed:
            if self._get("closed_at") is not None:
                return []
            moment = now()
            rows = self.db.execute(
                "SELECT * FROM members WHERE revoked_at IS NULL AND presence='connected'"
                " AND last_seen_at < ?",
                (moment - limit,),
            ).fetchall()
            flipped: list[str] = []
            for row in rows:
                self.db.execute(
                    "UPDATE members SET presence='stale', presence_changed_at=? WHERE id=?",
                    (moment, row["id"]),
                )
                self._emit(
                    f"presence {row['id']} {row['name']} stale since={row['last_seen_at']}",
                    row["id"],
                )
                flipped.append(row["id"])
            if flipped:
                self.db.commit()
                self.changed.notify_all()
            return flipped

    def prune(self) -> dict[str, int]:
        with self.lock:
            moment = now()
            cursor = self.db.execute(
                "DELETE FROM messages WHERE id IN ("
                "  SELECT m.id FROM messages m WHERE"
                "    ((m.sender = 'sys' AND m.sent_at < ?) OR (m.sender <> 'sys' AND m.sent_at < ?))"
                "    AND NOT EXISTS ("
                "      SELECT 1 FROM deliveries d WHERE d.message_id = m.id AND d.state <> 'handled')"
                ")",
                (
                    moment - PRUNE_SYSTEM_MESSAGE_SECONDS,
                    moment - PRUNE_MESSAGE_SECONDS,
                ),
            )
            messages = cursor.rowcount or 0
            self.db.execute(
                "DELETE FROM deliveries WHERE message_id NOT IN (SELECT id FROM messages)"
            )
            cursor = self.db.execute(
                "DELETE FROM invites WHERE created_at < ? AND (used_at IS NOT NULL OR expires_at < ?)",
                (moment - PRUNE_INVITE_SECONDS, moment),
            )
            invites = cursor.rowcount or 0
            self.db.commit()
            return {"messages": messages, "invites": invites}


class OrchestraHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: HubStore):
        super().__init__(address, OrchestraHandler)
        self.store = store

    def server_bind(self) -> None:
        # HTTPServer.server_bind() resolves the bind address with
        # socket.getfqdn(), whose reverse-DNS lookup can block for tens of
        # seconds on macOS and stall startup past the client's ready deadline.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class OrchestraHandler(BaseHTTPRequestHandler):
    server: OrchestraHTTPServer
    server_version = "agent-orchestra/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def setup(self) -> None:
        # The listener hands over an unhandshaked socket, so a client that
        # connects and never sends a ClientHello stalls this one handler thread
        # for HANDSHAKE_TIMEOUT_SECONDS and never the accept loop.
        handshake = getattr(self.request, "do_handshake", None)
        if handshake is not None:
            self.request.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            try:
                handshake()
            except (OSError, ValueError):
                self.connection = self.request
                # No TLS session, so no readable stream: hand handle_one_request
                # an empty one and let it close the connection quietly.
                self.rfile = io.BytesIO(b"")
                self.wfile = io.BytesIO()
                return
            # A long poll may hold the socket for MAX_WAIT_SECONDS, so the
            # handshake deadline must not outlive the handshake.
            self.request.settimeout(None)
        super().setup()

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            parsed = urlsplit(self.path)
            store = self.server.store
            if self.command == "POST" and parsed.path == "/v1/join":
                body = self._read_body()
                self._respond(
                    200,
                    store.join(
                        str(body.get("secret", "")),
                        str(body.get("name", "member")),
                        str(body.get("provider", "cli")),
                    ),
                )
                return

            principal = store.authenticate(self.headers.get("Authorization"))
            is_member = principal.get("kind") == "member"

            if self.command == "GET" and parsed.path == "/v1/status":
                self._respond(200, store.status(principal))
                return
            if self.command == "GET" and parsed.path == "/v1/members":
                self._respond(200, store.members())
                return
            if self.command == "GET" and parsed.path == "/v1/tasks":
                self._respond(200, store.tasks())
                return
            if self.command == "POST" and parsed.path == "/v1/invite":
                self._respond(200, store.invite(principal, self._read_body()))
                return
            if self.command == "GET" and parsed.path == "/v1/messages/pending":
                self._require_member(principal)
                query = parse_qs(parsed.query)
                wait = float(query.get("wait", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                self._respond(200, {"messages": store.pending(principal, wait, limit)})
                return
            if self.command == "POST" and parsed.path == "/v1/messages":
                self._require_member(principal)
                self._respond(200, store.send(principal, self._read_body()))
                return
            mark = _MESSAGE_MARK_PATH.fullmatch(parsed.path)
            if self.command == "POST" and mark:
                self._require_member(principal)
                self._read_body()
                target = "delivered" if mark.group(2) == "ack" else "handled"
                self._respond(200, store.mark(principal, mark.group(1), target))
                return
            single = _MESSAGE_PATH.fullmatch(parsed.path)
            if self.command == "GET" and single:
                self._respond(200, store.message_status(principal, single.group(1)))
                return
            if self.command == "POST" and parsed.path == "/v1/heartbeat":
                self._require_member(principal)
                self._read_body()
                self._respond(200, store.heartbeat(principal))
                return
            if self.command == "POST" and parsed.path == "/v1/leave":
                self._require_member(principal)
                self._read_body()
                self._respond(200, store.leave(principal))
                return
            if self.command == "POST" and parsed.path == "/v1/conductor":
                body = self._read_body()
                self._respond(200, store.set_conductor(principal, str(body.get("member_id", ""))))
                return
            if self.command == "POST" and parsed.path == "/v1/kick":
                body = self._read_body()
                self._respond(
                    200,
                    store.kick(principal, str(body.get("member_id", "")), body.get("reason")),
                )
                return
            if self.command == "POST" and parsed.path == "/v1/close":
                self._read_body()
                # The process stays up; the close watcher in serve() decides
                # when every member has been told and shuts it down then.
                self._respond(200, store.close(principal))
                return
            raise APIError(404, "Endpoint not found")
        except APIError as exc:
            self._respond(exc.status, {"error": str(exc)})
        except ProtocolError as exc:
            self._respond(400, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": f"Invalid request: {exc}"})
        except Exception:
            self._respond(500, {"error": "Internal server error"})

    def _require_member(self, principal: dict[str, Any]) -> None:
        if principal.get("kind") != "member":
            raise APIError(403, "This endpoint needs a member token")

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_REQUEST_BYTES:
            raise APIError(413, "Request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise APIError(400, "Request body must be a JSON object")
        return value

    def _respond(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


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
            [sys.executable, *_SPAWN_ENTRY, *args],
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


def _generate_certificate(directory: Path) -> str:
    openssl = shutil.which("openssl")
    if not openssl:
        raise OrchestraError("openssl is required to create the hub TLS certificate")
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    command = [
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-sha256",
        "-nodes",
        "-keyout",
        str(key),
        "-out",
        str(cert),
        "-days",
        str(CERTIFICATE_DAYS),
        "-subj",
        "/CN=agent-orchestra",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OrchestraError(f"openssl could not create a TLS certificate: {detail}")
    try:
        key.chmod(0o600)
        cert.chmod(0o600)
    except OSError:
        pass
    return certificate_fingerprint(cert)


def discover_addresses(bind: str, advertised: list[str] | None = None) -> list[str]:
    if advertised:
        values = advertised
    elif bind not in {"0.0.0.0", "::", ""}:
        values = [bind]
    else:
        values = []
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("192.0.2.1", 9))
                values.append(probe.getsockname()[0])
            finally:
                probe.close()
        except OSError:
            pass
        try:
            values.extend(
                item[4][0]
                for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            )
        except OSError:
            pass
        values.append("127.0.0.1")
    result: list[str] = []
    for value in values:
        candidate = str(value).strip()
        if candidate and candidate not in {"0.0.0.0", "::"} and candidate not in result:
            result.append(candidate)
    if not result:
        result.append("127.0.0.1")
    return result


def _allocate_port(bind: str, port: int) -> int:
    if port:
        return int(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind if bind else "0.0.0.0", 0))
        return int(probe.getsockname()[1])
    except OSError as exc:
        raise OrchestraError(f"Could not allocate a port on {bind}: {exc}") from exc
    finally:
        probe.close()


def _hub_log(orchestra_id: str) -> Path:
    return runtime_dir() / f"{orchestra_id}.hub.log"


def _hubs_root() -> Path:
    return state_root() / "hubs"


def _hub_config_path(orchestra_id: str) -> Path:
    return _hubs_root() / safe_id(orchestra_id) / "hub.json"


def _wait_for_ready(directory: Path, pid: int) -> dict[str, Any]:
    ready_path = directory / "ready.json"
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        try:
            ready = read_json(ready_path)
        except OrchestraError:
            time.sleep(0.05)
            continue
        if int(ready.get("pid", 0)) == pid:
            return ready
        time.sleep(0.05)
    _reap_spawned_processes([pid], timeout=0)
    raise OrchestraError(
        f"Hub process did not start; inspect {directory.parent.parent / 'runtime'}"
    )


def create_hub(
    *,
    name: str,
    bind: str = "0.0.0.0",
    port: int = 0,
    advertise: list[str] | None = None,
    invite_ttl: int = DEFAULT_INVITE_TTL_SECONDS,
) -> dict[str, Any]:
    if not 0 <= int(port) <= 65535:
        raise OrchestraError("Port must be between 0 and 65535")
    orchestra_id = new_orchestra_id()
    directory = hub_dir(orchestra_id)
    config_path = directory / "hub.json"
    if config_path.exists():
        raise OrchestraError(f"Hub {orchestra_id} already exists")
    hub_name = sanitize_name(name, fallback="") or sanitize_name(
        socket.gethostname(), fallback="orchestra"
    )
    actual_port = _allocate_port(bind, int(port))
    fingerprint = _generate_certificate(directory)
    endpoints = [
        f"https://{address}:{actual_port}" for address in discover_addresses(bind, advertise)
    ]
    admin_token = token(32)
    created_at = now()
    config = {
        "protocol": 1,
        "orchestra_id": orchestra_id,
        "name": hub_name,
        "bind": bind,
        "port": actual_port,
        "advertise": list(advertise or []),
        "endpoints": endpoints,
        "fingerprint": fingerprint,
        "admin_token": admin_token,
        "created_at": created_at,
        "closed_at": None,
    }
    atomic_write_json(config_path, config)

    ttl = clamp_ttl(invite_ttl)
    expires_at = created_at + ttl
    secret = token(32)
    store = HubStore(directory / "hub.sqlite")
    try:
        store.initialize(
            orchestra_id=orchestra_id,
            name=hub_name,
            admin_token_hash=secret_hash(admin_token),
            endpoints=endpoints,
            fingerprint=fingerprint,
        )
        with store.lock:
            store.db.execute(
                "INSERT INTO invites (secret_hash, role, parent, name, issued_by, created_at,"
                " expires_at, used_at, used_by) VALUES (?, 'conductor', NULL, NULL, 'admin', ?, ?, NULL, NULL)",
                (secret_hash(secret), created_at, expires_at),
            )
            store.db.commit()
    finally:
        store.disconnect()

    conductor_invite = encode_invite(
        {
            "orchestra_id": orchestra_id,
            "endpoints": endpoints,
            "fingerprint": fingerprint,
            "secret": secret,
            "expires_at": expires_at,
            "role": "conductor",
            "parent": None,
            "hub": {"name": hub_name},
        }
    )
    hub_pid = _spawn_module(
        ["serve", "--orchestra-id", orchestra_id], _hub_log(orchestra_id)
    )
    _wait_for_ready(directory, hub_pid)
    return {
        "orchestra_id": orchestra_id,
        "name": hub_name,
        "endpoints": endpoints,
        "fingerprint": fingerprint,
        "hub_pid": hub_pid,
        "port": actual_port,
        "conductor_invite": conductor_invite,
        "invite_expires_at": expires_at,
    }


def connect_host(bind: Any) -> str:
    """The address a client on this machine dials for a hub bound to `bind`."""
    address = str(bind or "").strip()
    return "127.0.0.1" if address in {"0.0.0.0", "::", ""} else address


def _url_host(address: str) -> str:
    return f"[{address}]" if ":" in address else address


def _port_answers(host: str, port: int, timeout: float = LIVENESS_PROBE_SECONDS) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _running_pid(orchestra_id: str, config: dict[str, Any] | None = None) -> int | None:
    """The pid of a hub that both exists and answers on its port. A pid alone
    lies after a reboot hands the number to something else."""
    try:
        ready = read_json(hub_dir(orchestra_id) / "ready.json")
    except OrchestraError:
        return None
    try:
        pid = int(ready.get("pid", 0))
        port = int(ready.get("port", 0) or (config or {}).get("port", 0) or 0)
    except (TypeError, ValueError):
        return None
    if not _pid_alive(pid):
        return None
    host = connect_host((config or {}).get("bind"))
    return pid if _port_answers(host, port) else None


def ensure_hub(orchestra_id: str) -> int:
    config = read_json(_hub_config_path(orchestra_id))
    if config.get("closed_at"):
        raise OrchestraError(f"Orchestra {orchestra_id} is closed")
    directory = hub_dir(orchestra_id)
    pid = _running_pid(orchestra_id, config)
    if pid:
        return pid
    hub_pid = _spawn_module(["serve", "--orchestra-id", orchestra_id], _hub_log(orchestra_id))
    _wait_for_ready(directory, hub_pid)
    return hub_pid


def hub_alive(orchestra_id: str) -> bool:
    config: dict[str, Any] = {}
    try:
        config = read_json(_hub_config_path(orchestra_id))
    except OrchestraError:
        pass
    # A closed hub may still be serving 410s through its grace window; it is
    # not a hub anyone may use or restart, so it never reads as alive.
    if config.get("closed_at"):
        return False
    return _running_pid(orchestra_id, config) is not None


def local_hubs() -> list[dict[str, Any]]:
    root = _hubs_root()
    if not root.is_dir():
        return []
    hubs: list[dict[str, Any]] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        config_path = entry / "hub.json"
        if not config_path.is_file():
            continue
        try:
            config = read_json(config_path)
        except OrchestraError:
            continue
        if config.get("closed_at"):
            continue
        hubs.append(config)
    hubs.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
    return hubs


def select_hub(orchestra_id: str | None = None) -> dict[str, Any]:
    if orchestra_id:
        config_path = _hub_config_path(orchestra_id)
        if not config_path.is_file():
            raise OrchestraError(f"No hub named {orchestra_id} on this machine")
        return read_json(config_path)
    hubs = local_hubs()
    if not hubs:
        raise OrchestraError(
            "No orchestra hub on this machine; run: agent-orchestra hub start"
        )
    if len(hubs) > 1:
        names = ", ".join(str(item.get("orchestra_id")) for item in hubs)
        raise OrchestraError(f"Several hubs on this machine; pass --orchestra-id: {names}")
    return hubs[0]


def admin_connection(orchestra_id: str) -> dict[str, Any]:
    config = select_hub(orchestra_id)
    # A hub bound to one address does not answer on the loopback, so dial the
    # bind address itself unless it is a wildcard.
    host = _url_host(connect_host(config.get("bind")))
    return {
        "endpoints": [f"https://{host}:{int(config['port'])}"],
        "fingerprint": config["fingerprint"],
        "token": config["admin_token"],
    }


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def hub_unit(orchestra_id: str) -> str:
    config = select_hub(orchestra_id)
    identifier = str(config["orchestra_id"])
    python = sys.executable
    root = str(_module_root())
    log = str(_hub_log(identifier))
    # The supervisor starts with a bare environment, so the state root has to be
    # pinned to the one this hub was created under.
    home = str(state_root().resolve())
    if sys.platform == "darwin":
        environment = [("PYTHONPATH", root), ("AGENT_ORCHESTRA_HOME", home)]
        rows = "\n".join(
            f"      <key>{_xml_escape(key)}</key><string>{_xml_escape(value)}</string>"
            for key, value in environment
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
            ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            f"    <key>Label</key><string>com.agent-orchestra.{_xml_escape(identifier)}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"      <string>{_xml_escape(python)}</string>\n"
            "      <string>-m</string>\n"
            "      <string>agent_orchestra</string>\n"
            "      <string>serve</string>\n"
            "      <string>--orchestra-id</string>\n"
            f"      <string>{_xml_escape(identifier)}</string>\n"
            "    </array>\n"
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"{rows}\n"
            "    </dict>\n"
            "    <key>RunAtLoad</key><true/>\n"
            # A closed orchestra exits 0, and SuccessfulExit false keeps launchd
            # from restarting it into the same clean exit forever.
            "    <key>KeepAlive</key>\n"
            "    <dict>\n"
            "      <key>SuccessfulExit</key><false/>\n"
            "    </dict>\n"
            f"    <key>StandardOutPath</key><string>{_xml_escape(log)}</string>\n"
            f"    <key>StandardErrorPath</key><string>{_xml_escape(log)}</string>\n"
            "</dict>\n"
            "</plist>\n"
        )
    lines = [
        "[Unit]",
        f"Description=Agent Orchestra hub {identifier}",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"Environment=PYTHONPATH={root}",
        f"Environment=AGENT_ORCHESTRA_HOME={home}",
    ]
    lines.extend(
        [
            f"ExecStart={python} -m agent_orchestra serve --orchestra-id {identifier}",
            "Restart=on-failure",
            "RestartSec=5",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    return "\n".join(lines)


def _interval_thread(stop: threading.Event, interval: float, action: Callable[[], Any]) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(interval):
            try:
                action()
            except Exception as exc:  # the hub keeps serving through a sweep failure
                print(f"agent-orchestra: background sweep failed: {exc}", file=sys.stderr)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def _close_watch_thread(
    stop: threading.Event, server: "OrchestraHTTPServer", store: HubStore
) -> threading.Thread:
    """Exit the process once every unrevoked member has seen the closure, or
    once CLOSE_GRACE_SECONDS have passed since the close."""

    def loop() -> None:
        while not stop.is_set():
            try:
                delay = store.close_shutdown_delay()
            except Exception as exc:  # a bad read must not strand the process
                print(f"agent-orchestra: close watch failed: {exc}", file=sys.stderr)
                delay = None
            if delay is not None and delay <= 0:
                break
            if delay is None:
                store.wait_for_change(CLOSE_CHECK_INTERVAL_SECONDS)
            elif stop.wait(min(CLOSE_CHECK_INTERVAL_SECONDS, delay)):
                return
        if not stop.is_set():
            threading.Thread(target=server.shutdown, daemon=True).start()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def serve(orchestra_id: str) -> None:
    directory = hub_dir(orchestra_id)
    config = read_json(directory / "hub.json")
    if config.get("closed_at"):
        # A supervisor restarts on failure only, so a closed orchestra must be a
        # clean exit or launchd and systemd would restart it forever.
        print(f"agent-orchestra: orchestra {orchestra_id} is closed", file=sys.stderr)
        return
    store = HubStore(directory / "hub.sqlite")
    if store.closed_at() is not None:
        store.disconnect()
        print(f"agent-orchestra: orchestra {orchestra_id} is closed", file=sys.stderr)
        return
    store.refresh_hub(
        name=str(config.get("name") or orchestra_id),
        endpoints=list(config.get("endpoints") or []),
        fingerprint=str(config.get("fingerprint") or ""),
    )
    bind = str(config.get("bind") or "0.0.0.0")
    port = int(config.get("port") or 0)
    server = OrchestraHTTPServer((bind, port), store)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    # accept() must not run the TLS handshake: OrchestraHandler.setup() does it
    # on the handler thread, under a deadline.
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    ready_path = directory / "ready.json"
    atomic_write_json(
        ready_path,
        {"pid": os.getpid(), "port": int(server.server_address[1]), "started_at": now()},
    )
    stop = threading.Event()
    _interval_thread(stop, PRESENCE_INTERVAL_SECONDS, store.sweep_presence)
    _interval_thread(stop, PRUNE_INTERVAL_SECONDS, store.prune)
    _close_watch_thread(stop, server, store)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        stop.set()
        server.server_close()
        store.disconnect()
        try:
            ready_path.unlink()
        except OSError:
            pass


def _serve_main(argv: list[str]) -> int:
    """Minimal `serve --orchestra-id ID` entry point for the detached process."""
    args = list(argv)
    if args and args[0] == "serve":
        args = args[1:]
    if len(args) == 2 and args[0] == "--orchestra-id":
        serve(args[1])
        return 0
    raise OrchestraError("usage: serve --orchestra-id ORCHESTRA_ID")
