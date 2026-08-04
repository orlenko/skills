from __future__ import annotations

import json
import os
import signal
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .remote import MAX_SNAPSHOT_BYTES, RemoteError, load_home_config, validate_snapshot
from .runtime import write_ingest_info
from .service import Observer, ObserverConfig


def _handler(config: ObserverConfig):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-observer-ingest/1"

        def _json(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
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
                        display_name = value.get("display_name")
                        provider = value.get("provider")
                        if not isinstance(secret, str) or not secret:
                            raise RemoteError("remote enrollment secret is required")
                        if (
                            not isinstance(display_name, str)
                            or not display_name.strip()
                        ):
                            raise RemoteError("remote display hostname is required")
                        display_name = "".join(
                            character
                            for character in display_name
                            if character.isprintable()
                        ).strip()
                        if not display_name:
                            raise RemoteError("remote display hostname is invalid")
                        if provider not in {"claude", "codex"}:
                            raise RemoteError("remote analyzer provider is invalid")
                        result = observer.db.accept_remote_invite(
                            secret,
                            display_name=display_name,
                            provider=provider,
                        )
                        result["display_name"] = display_name.strip()[:160]
                        self._json(200, result)
                        return
                    token = self._token()
                    if token is None:
                        return
                    if path == "/v1/claim":
                        self._json(200, observer.db.claim_remote_uploader(token))
                        return
                    if path == "/v1/snapshot":
                        canonical, digest = validate_snapshot(value)
                        result = observer.db.import_remote_snapshot(
                            token,
                            value,
                            canonical_json=canonical,
                            snapshot_hash=digest,
                        )
                        self._json(200, result)
                        return
                self._json(404, {"error": "not found"})
            except KeyError as exc:
                self._json(404, {"error": str(exc)})
            except (RemoteError, ValueError) as exc:
                message = str(exc)
                if "credential" in message or "revoked" in message:
                    status = 401
                elif (
                    "superseded" in message
                    or "stale" in message
                    or "conflicts" in message
                ):
                    status = 409
                elif "rate limit" in message:
                    status = 429
                else:
                    status = 400
                self._json(status, {"error": message})

        def do_GET(self) -> None:
            self._json(405, {"error": "method not allowed"})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def run_ingest(config: ObserverConfig, *, bind: str, port: int) -> int:
    home = load_home_config(config)
    if home is None:
        raise RemoteError("remote ingestion is not enabled for this Observer workspace")
    if bind != str(home["bind"]) or port != int(home["port"]):
        raise RemoteError(
            "remote ingest process does not match the enabled configuration"
        )
    server = ThreadingHTTPServer((bind, port), _handler(config))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(home["cert"]), str(home["key"]))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    actual_port = int(server.server_address[1])
    info_path = write_ingest_info(config, bind=bind, port=actual_port)

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
