from __future__ import annotations

import hmac
import json
import os
import signal
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .presentation import dashboard_projection
from .runtime import (
    consume_bootstrap_token,
    ensure_auth_token,
    service_status,
    write_server_info,
)
from .service import Observer, ObserverConfig


MAX_BODY = 64 * 1024
ASSETS = Path(__file__).resolve().parent / "web_assets"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def _handler(config: ObserverConfig, token: str, port: int):
    allowed_origins = {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-observer/0.4.2"

        def _host_allowed(self) -> bool:
            host = self.headers.get("Host") or ""
            allowed = {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
            }
            if host not in allowed:
                self._json(403, {"error": "local host required"})
                return False
            return True

        def _authenticated(self) -> bool:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except cookies.CookieError:
                return False
            morsel = jar.get("ao_session")
            return bool(morsel and hmac.compare_digest(morsel.value, token))

        def _headers(self, code: int, content_type: str, length: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
            self.end_headers()

        def _json(self, code: int, value: Any) -> None:
            body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
            self._headers(code, "application/json; charset=utf-8", len(body))
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._json(413 if length > MAX_BODY else 400, {"error": "bad body"})
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

        def _post_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            if origin not in allowed_origins:
                self._json(403, {"error": "same origin required"})
                return False
            if self.headers.get("X-Agent-Observer") != "1":
                self._json(403, {"error": "observer request header required"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._host_allowed():
                return
            parsed = urlparse(self.path)
            supplied = (parse_qs(parsed.query).get("bootstrap") or [""])[0]
            if consume_bootstrap_token(config, supplied):
                self.send_response(303)
                self.send_header("Location", parsed.path or "/")
                self.send_header(
                    "Set-Cookie",
                    f"ao_session={token}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not self._authenticated():
                self._json(401, {"error": "open the authenticated dashboard URL"})
                return
            if parsed.path == "/api/status":
                with Observer(config) as observer:
                    result = dashboard_projection(observer.status())
                    result["analyzer"] = observer.db.supervisor_status()
                result["services"] = service_status(config)
                self._json(200, result)
                return
            if parsed.path == "/api/project-candidates":
                with Observer(config) as observer:
                    result = {"candidates": observer.project_candidates()}
                self._json(200, result)
                return
            if parsed.path == "/api/services":
                with Observer(config) as observer:
                    result = service_status(config)
                    result["analyzer"] = observer.db.supervisor_status()
                self._json(200, result)
                return
            asset = STATIC.get(parsed.path)
            if asset:
                filename, content_type = asset
                try:
                    body = (ASSETS / filename).read_bytes()
                except OSError:
                    self._json(500, {"error": "dashboard asset unavailable"})
                    return
                self._headers(200, content_type, len(body))
                self.wfile.write(body)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._host_allowed():
                return
            if not self._authenticated():
                self._json(401, {"error": "authentication required"})
                return
            if not self._post_allowed():
                return
            value = self._read_json()
            if value is None:
                return
            parsed = urlparse(self.path)
            try:
                with Observer(config) as observer:
                    if parsed.path == "/api/projects":
                        path = value.get("path")
                        if not isinstance(path, str) or not path.strip():
                            raise ValueError("path is required")
                        observer.add_project(path, allow_existing=False)
                    elif parsed.path == "/api/projects/remove":
                        project = value.get("project")
                        if not isinstance(project, str):
                            raise ValueError("project is required")
                        observer.remove_project(project)
                    elif parsed.path == "/api/rescan":
                        project = value.get("project")
                        if not isinstance(project, str):
                            raise ValueError("project is required")
                        observer.rescan_project(project)
                    elif parsed.path == "/api/findings/seen":
                        finding_id = value.get("finding_id")
                        if not isinstance(finding_id, str):
                            raise ValueError("finding_id is required")
                        if not observer.db.mark_finding_seen(finding_id):
                            raise KeyError("finding not found")
                    elif parsed.path == "/api/projects/dismiss-attention":
                        project = value.get("project")
                        if not isinstance(project, str):
                            raise ValueError("project is required")
                        if not observer.dismiss_project_attention(project):
                            raise KeyError("project not found")
                    else:
                        self._json(404, {"error": "not found"})
                        return
                    result = dashboard_projection(observer.status())
                    result["analyzer"] = observer.db.supervisor_status()
                result["services"] = service_status(config)
                self._json(200, result)
            except (KeyError, OSError, ValueError) as exc:
                self._json(400, {"error": str(exc)})

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


def run_server(config: ObserverConfig, *, port: int) -> int:
    token = ensure_auth_token(config)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(config, token, port))
    actual_port = int(server.server_address[1])
    info_path = write_server_info(config, port=actual_port)

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
