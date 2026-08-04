from __future__ import annotations

import json
import os
import signal
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .remote import (
    MAX_SNAPSHOT_BYTES,
    RemoteError,
    _snapshot_hash,
    build_snapshot,
    load_listener_config,
    validate_snapshot,
)
from .runtime import write_listener_info
from .service import Observer, ObserverConfig


def _handler(config: ObserverConfig, listener: dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-observer-export/1"

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
            if len(body) > MAX_SNAPSHOT_BYTES:
                status = 413
                body = b'{"error":"remote snapshot exceeds the 2 MiB limit"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_SNAPSHOT_BYTES:
                self._json(
                    413 if length > MAX_SNAPSHOT_BYTES else 400, {"error": "bad body"}
                )
                return None
            try:
                value = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "body is not valid JSON"})
                return None
            if not isinstance(value, dict):
                self._json(400, {"error": "body must be an object"})
                return None
            return value

        def _token(self) -> str | None:
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Bearer ") or not header[7:]:
                self._json(401, {"error": "remote node credential is required"})
                return None
            return header[7:]

        def do_POST(self) -> None:
            value = self._read_json()
            if value is None:
                return
            path = urlparse(self.path).path
            try:
                with Observer(config) as observer:
                    if path == "/v1/accept":
                        secret = value.get("secret")
                        if not isinstance(secret, str) or not secret:
                            raise RemoteError("remote enrollment secret is required")
                        supervisor = observer.db.supervisor_status()
                        provider = supervisor.get("provider")
                        result = observer.db.accept_remote_invite(
                            secret,
                            display_name=str(listener["display_name"]),
                            provider=provider if provider in {"claude", "codex"} else None,
                        )
                        result["display_name"] = str(listener["display_name"])
                        result["provider"] = provider
                        self._json(200, result)
                        return
                    token = self._token()
                    if token is None:
                        return
                    if path == "/v1/claim":
                        self._json(200, observer.db.claim_remote_uploader(token))
                        return
                    if path == "/v1/snapshot":
                        after = value.get("after_revision", 0)
                        if not isinstance(after, int) or after < 0:
                            raise RemoteError("snapshot cursor is invalid")
                        reserved = observer.db.reserve_remote_snapshot(token)
                        snapshot = build_snapshot(
                            observer,
                            {
                                "node_id": reserved["node_id"],
                                "display_name": listener["display_name"],
                                "provider": reserved.get("provider"),
                                "upload_epoch": reserved["upload_epoch"],
                                "last_revision": int(reserved["last_revision"]) - 1,
                            },
                            revision=int(reserved["last_revision"]),
                        )
                        snapshot["snapshot_hash"] = _snapshot_hash(snapshot)
                        validate_snapshot(snapshot)
                        self._json(200, snapshot)
                        return
                    if path == "/v1/revoke":
                        node = observer.db.authenticate_remote_node(token)
                        self._json(
                            200,
                            observer.db.revoke_remote_node(str(node["node_id"])),
                        )
                        return
                self._json(404, {"error": "not found"})
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (RemoteError, ValueError) as exc:
                message = str(exc)
                status = 401 if "credential" in message or "revoked" in message else 400
                self._json(status, {"error": message})

        def do_GET(self) -> None:
            self._json(405, {"error": "method not allowed"})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def run_exporter(config: ObserverConfig, *, bind: str, port: int) -> int:
    listener = load_listener_config(config)
    if listener is None:
        raise RemoteError("remote snapshot listener is not enabled")
    if bind != str(listener["bind"]) or port != int(listener["port"]):
        raise RemoteError("remote export process does not match its enabled configuration")
    server = ThreadingHTTPServer((bind, port), _handler(config, listener))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(listener["cert"]), str(listener["key"]))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    actual_port = int(server.server_address[1])
    info_path = write_listener_info(config, bind=bind, port=actual_port)

    def stop(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            current = json.loads(info_path.read_text(encoding="utf-8"))
            if int(current.get("pid", 0)) == os.getpid():
                info_path.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return 0
