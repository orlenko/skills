"""An HTTP server that binds without a reverse-DNS lookup."""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class QuickBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer minus `socket.getfqdn()` at bind.

    HTTPServer.server_bind() resolves the bind address with getfqdn(), whose
    reverse-DNS lookup blocks for seconds on macOS. On GitHub's macOS runners
    that pushed every sidecar past its 8-second ready deadline, and six tests
    failed from 2026-08-04 on. agent-orchestra's hub avoids it the same way.
    """

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)
