from __future__ import annotations

import json
import os
import re
import socketserver
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .core import (
    APIError,
    MAX_MESSAGE_BYTES,
    AgentPairError,
    atomic_write_json,
    now,
    pair_dir,
    read_json,
    secret_hash,
    secret_matches,
    token,
)


class PairStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.state = read_json(path)

    def _save(self) -> None:
        atomic_write_json(self.path, self.state)

    def _live(self) -> None:
        if self.state.get("closed_at"):
            raise APIError(410, "Pair is closed")
        if float(self.state.get("expires_at", 0)) <= now():
            raise APIError(410, "Pair has expired")

    def accept(self, secret: str, name: str, provider: str) -> dict[str, Any]:
        with self.changed:
            self._live()
            if not secret_matches(secret, self.state.get("invite_secret_hash")):
                raise APIError(403, "Invite secret is invalid or already used")
            if self.state.get("guest_peer_id"):
                raise APIError(409, "This pair already has two participants")
            guest_id = f"p_{uuid.uuid4().hex[:16]}"
            guest_token = token(32)
            self.state["guest_peer_id"] = guest_id
            self.state["invite_secret_hash"] = None
            self.state["peers"][guest_id] = {
                "id": guest_id,
                "name": name[:80] or "peer",
                "provider": provider[:40] or "cli",
                "token_hash": secret_hash(guest_token),
                "joined_at": now(),
                "last_seen_at": now(),
            }
            self._save()
            self.changed.notify_all()
            host = self.state["peers"][self.state["host_peer_id"]]
            return {
                "pair_id": self.state["pair_id"],
                "peer_id": guest_id,
                "token": guest_token,
                "peer": public_peer(host),
                "expires_at": self.state["expires_at"],
            }

    def authenticate(self, header: str | None) -> dict[str, Any]:
        if not header or not header.startswith("Bearer "):
            raise APIError(401, "Missing bearer token")
        value = header[7:]
        with self.lock:
            self._live()
            for peer in self.state["peers"].values():
                if secret_matches(value, peer.get("token_hash")):
                    peer["last_seen_at"] = now()
                    return peer
        raise APIError(401, "Invalid bearer token")

    def send(self, peer: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        message_id = str(payload.get("id") or f"m_{uuid.uuid4().hex}")
        if not isinstance(text, str) or not text.strip():
            raise APIError(400, "Message text must not be empty")
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise APIError(413, f"Message exceeds {MAX_MESSAGE_BYTES} bytes")
        if not re.fullmatch(r"m_[A-Za-z0-9_-]{8,96}", message_id):
            raise APIError(400, "Invalid message id")
        with self.changed:
            self._live()
            recipient_id = self._other_peer_id(peer["id"])
            body_digest = secret_hash(text)
            existing = self.state["messages"].get(message_id)
            if existing:
                if existing["from"] != peer["id"] or existing["body_sha256"] != body_digest:
                    raise APIError(409, "Message id is already in use")
                return public_message_status(existing)
            record = {
                "id": message_id,
                "from": peer["id"],
                "to": recipient_id,
                "text": text,
                "body_sha256": body_digest,
                "state": "queued",
                "sent_at": now(),
                "delivered_at": None,
                "handled_at": None,
            }
            self.state["messages"][message_id] = record
            self._save()
            self.changed.notify_all()
            return public_message_status(record)

    def pending(self, peer: dict[str, Any], wait_seconds: float, limit: int) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, min(wait_seconds, 30.0))
        with self.changed:
            while True:
                self._live()
                rows = [
                    message_envelope(item, self.state["peers"][item["from"]])
                    for item in self.state["messages"].values()
                    if item["to"] == peer["id"] and item["state"] == "queued"
                ][: max(1, min(limit, 100))]
                if rows or time.monotonic() >= deadline:
                    return rows
                self.changed.wait(timeout=min(1.0, deadline - time.monotonic()))

    def mark(self, peer: dict[str, Any], message_id: str, target: str) -> dict[str, Any]:
        with self.changed:
            record = self.state["messages"].get(message_id)
            if not record:
                raise APIError(404, "Message not found")
            if record["to"] != peer["id"]:
                raise APIError(403, "Only the recipient can acknowledge this message")
            if target == "delivered" and record["state"] == "queued":
                record["state"] = "delivered"
                record["delivered_at"] = now()
                record["text"] = None
            elif target == "handled" and record["state"] in {"queued", "delivered"}:
                if record["state"] == "queued":
                    record["delivered_at"] = now()
                record["state"] = "handled"
                record["handled_at"] = now()
                record["text"] = None
            self._save()
            self.changed.notify_all()
            return public_message_status(record)

    def status(self, peer: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            messages = [
                public_message_status(item)
                for item in self.state["messages"].values()
                if item["from"] == peer["id"]
            ]
            other = None
            try:
                other = public_peer(self.state["peers"][self._other_peer_id(peer["id"])])
            except APIError:
                pass
            return {
                "pair_id": self.state["pair_id"],
                "expires_at": self.state["expires_at"],
                "closed_at": self.state.get("closed_at"),
                "self": public_peer(peer),
                "peer": other,
                "messages": messages[-100:],
            }

    def heartbeat(self, peer: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            peer["last_seen_at"] = now()
            self._save()
            return {"ok": True, "at": peer["last_seen_at"]}

    def close(self, peer: dict[str, Any]) -> dict[str, Any]:
        with self.changed:
            if not self.state.get("closed_at"):
                self.state["closed_at"] = now()
                self.state["closed_by"] = peer["id"]
                self._save()
                self.changed.notify_all()
            return {"ok": True, "closed_at": self.state["closed_at"]}

    def _other_peer_id(self, peer_id: str) -> str:
        others = [item for item in self.state["peers"] if item != peer_id]
        if not others:
            raise APIError(409, "The other participant has not joined yet")
        return others[0]


def public_peer(peer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": peer["id"],
        "name": peer["name"],
        "provider": peer["provider"],
        "joined_at": peer["joined_at"],
        "last_seen_at": peer["last_seen_at"],
    }


def public_message_status(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "state": record["state"],
        "sent_at": record["sent_at"],
        "delivered_at": record.get("delivered_at"),
        "handled_at": record.get("handled_at"),
    }


def message_envelope(record: dict[str, Any], sender: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "from": public_peer(sender),
        "sent_at": record["sent_at"],
        "text": record["text"],
    }


class PairHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: PairStore):
        super().__init__(address, PairHandler)
        self.store = store

    def server_bind(self) -> None:
        # HTTPServer.server_bind() resolves the bind address with
        # socket.getfqdn(), whose reverse-DNS lookup can block for tens of
        # seconds on macOS and stall startup past the client's ready deadline.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


class PairHandler(BaseHTTPRequestHandler):
    server: PairHTTPServer
    server_version = "agent-pair/0.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if self.command == "POST" and parsed.path == "/v1/accept":
                body = self._read_body()
                result = self.server.store.accept(
                    str(body.get("secret", "")),
                    str(body.get("name", "peer")),
                    str(body.get("provider", "cli")),
                )
                self._respond(200, result)
                return

            peer = self.server.store.authenticate(self.headers.get("Authorization"))
            if self.command == "GET" and parsed.path == "/v1/status":
                self._respond(200, self.server.store.status(peer))
                return
            if self.command == "GET" and parsed.path == "/v1/messages/pending":
                query = parse_qs(parsed.query)
                wait = float(query.get("wait", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                rows = self.server.store.pending(peer, wait, limit)
                self._respond(200, {"messages": rows})
                return
            if self.command == "POST" and parsed.path == "/v1/messages":
                self._respond(200, self.server.store.send(peer, self._read_body()))
                return
            match = re.fullmatch(r"/v1/messages/(m_[A-Za-z0-9_-]+)/(?P<action>ack|handled)", parsed.path)
            if self.command == "POST" and match:
                target = "delivered" if match.group("action") == "ack" else "handled"
                self._respond(200, self.server.store.mark(peer, match.group(1), target))
                return
            if self.command == "POST" and parsed.path == "/v1/heartbeat":
                self._read_body()
                self._respond(200, self.server.store.heartbeat(peer))
                return
            if self.command == "POST" and parsed.path == "/v1/close":
                self._read_body()
                self._respond(200, self.server.store.close(peer))
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            raise APIError(404, "Endpoint not found")
        except APIError as exc:
            self._respond(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._respond(400, {"error": f"Invalid request: {exc}"})
        except Exception:
            self._respond(500, {"error": "Internal server error"})

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_MESSAGE_BYTES + 4096:
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


def serve(pair_id: str) -> None:
    directory = pair_dir(pair_id)
    state_path = directory / "server.json"
    store = PairStore(state_path)
    bind = str(store.state.get("bind", "0.0.0.0"))
    port = int(store.state.get("port", 0))
    server = PairHTTPServer((bind, port), store)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "cert.pem", directory / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    actual_port = int(server.server_address[1])
    atomic_write_json(directory / "ready.json", {"pid": os.getpid(), "port": actual_port})

    def stop_at_expiry() -> None:
        while True:
            with store.lock:
                if store.state.get("closed_at"):
                    break
                remaining = float(store.state.get("expires_at", 0)) - now()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 5.0))
        server.shutdown()

    threading.Thread(target=stop_at_expiry, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
