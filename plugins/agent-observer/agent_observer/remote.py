from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .service import Observer, ObserverConfig


REMOTE_PROTOCOL = 1
SNAPSHOT_SCHEMA = "agent-observer-remote-snapshot-v1"
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
DEFAULT_INVITE_TTL = 60 * 60


class RemoteError(ValueError):
    pass


class RemoteHTTPError(RemoteError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def remote_dir(config: ObserverConfig) -> Path:
    path = config.state_dir / "remote"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def home_config_path(config: ObserverConfig) -> Path:
    return remote_dir(config) / "home.json"


def connection_path(config: ObserverConfig) -> Path:
    return remote_dir(config) / "connection.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def load_home_config(config: ObserverConfig) -> dict[str, Any] | None:
    value = _read_json(home_config_path(config))
    return value if value and value.get("enabled") is True else None


def load_connection(config: ObserverConfig) -> dict[str, Any] | None:
    return _read_json(connection_path(config))


def _certificate_fingerprint(path: Path) -> str:
    try:
        pem = path.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise RemoteError(
            f"could not read remote-ingest TLS certificate: {exc}"
        ) from exc
    return hashlib.sha256(der).hexdigest()


def _ensure_certificate(directory: Path) -> tuple[Path, Path, str]:
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    if not cert.is_file() or not key.is_file():
        openssl = shutil.which("openssl")
        if not openssl:
            raise RemoteError("openssl is required to enable remote Observer ingestion")
        result = subprocess.run(
            [
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
                "3650",
                "-subj",
                "/CN=agent-observer",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise RemoteError(
                f"openssl could not create the ingest certificate: {detail}"
            )
    cert.chmod(0o600)
    key.chmod(0o600)
    return cert, key, _certificate_fingerprint(cert)


def discover_addresses(bind: str, advertised: list[str] | None = None) -> list[str]:
    if advertised:
        values = advertised
    elif bind not in {"0.0.0.0", "::", ""}:
        values = [bind]
    else:
        values: list[str] = []
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
                for item in socket.getaddrinfo(
                    socket.gethostname(), None, socket.AF_INET
                )
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
        raise RemoteError("no LAN or Tailscale address is available to advertise")
    return result


def _allocate_port(bind: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((bind, 0))
        return int(listener.getsockname()[1])


def enable_home_remote(
    config: ObserverConfig,
    *,
    bind: str = "0.0.0.0",
    port: int | None = None,
    advertise: list[str] | None = None,
) -> dict[str, Any]:
    if not 0 <= (port or 0) <= 65535:
        raise RemoteError("remote ingest port must be between 0 and 65535")
    directory = remote_dir(config) / "home"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key, fingerprint = _ensure_certificate(directory)
    existing = load_home_config(config) or {}
    chosen_port = int(port or existing.get("port") or _allocate_port(bind))
    addresses = discover_addresses(bind, advertise or existing.get("advertise"))
    value = {
        "enabled": True,
        "bind": bind,
        "port": chosen_port,
        "advertise": addresses,
        "cert": str(cert),
        "key": str(key),
        "fingerprint": fingerprint,
        "updated_at": time.time(),
    }
    _atomic_json(home_config_path(config), value)
    return value


def encode_invite(value: dict[str, Any]) -> str:
    document = {"v": REMOTE_PROTOCOL, **value}
    raw = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "ao1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_invite(invite: str) -> dict[str, Any]:
    value = invite.strip()
    if not value.startswith("ao1."):
        raise RemoteError("remote enrollment key must start with ao1.")
    try:
        encoded = value[4:]
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteError("remote enrollment key is malformed") from exc
    required = {"v", "endpoints", "fingerprint", "secret", "expires_at"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise RemoteError("remote enrollment key is missing required fields")
    if payload.get("v") != REMOTE_PROTOCOL:
        raise RemoteError("remote enrollment protocol version is unsupported")
    if not isinstance(payload["endpoints"], list) or not payload["endpoints"]:
        raise RemoteError("remote enrollment key has no endpoints")
    if float(payload["expires_at"]) <= time.time():
        raise RemoteError("remote enrollment key has expired")
    fingerprint = payload["fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(
            character not in "0123456789abcdefABCDEF" for character in fingerprint
        )
    ):
        raise RemoteError("remote enrollment key has an invalid TLS fingerprint")
    if not isinstance(payload["secret"], str) or len(payload["secret"]) < 32:
        raise RemoteError("remote enrollment key has an invalid secret")
    for endpoint in payload["endpoints"]:
        parsed = urlsplit(str(endpoint))
        if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
            raise RemoteError(
                "remote enrollment key contains an invalid HTTPS endpoint"
            )
    return payload


def issue_invite(
    observer: Observer,
    home: dict[str, Any],
    *,
    ttl_seconds: int = DEFAULT_INVITE_TTL,
) -> dict[str, Any]:
    if not 300 <= ttl_seconds <= 24 * 60 * 60:
        raise RemoteError(
            "remote enrollment key TTL must be between 5 minutes and 24 hours"
        )
    secret = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl_seconds
    observer.db.create_remote_invite(secret, expires_at)
    endpoints = [
        f"https://{address}:{int(home['port'])}" for address in home["advertise"]
    ]
    invite = encode_invite(
        {
            "endpoints": endpoints,
            "fingerprint": home["fingerprint"],
            "secret": secret,
            "expires_at": expires_at,
            "home": {"name": socket.gethostname()},
        }
    )
    return {"invite": invite, "endpoints": endpoints, "expires_at": expires_at}


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, fingerprint: str, timeout: float):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._fingerprint = fingerprint.lower().replace(":", "")

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        cert = self.sock.getpeercert(binary_form=True)
        actual = hashlib.sha256(cert).hexdigest()
        if not hmac.compare_digest(actual, self._fingerprint):
            self.close()
            raise RemoteError("remote ingest TLS fingerprint does not match the key")


def _request(
    connection: dict[str, Any],
    path: str,
    payload: dict[str, Any],
    *,
    authenticated: bool = True,
    timeout: float = 10,
) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(body) > MAX_SNAPSHOT_BYTES:
        raise RemoteError("remote Observer payload exceeds the 2 MiB limit")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if authenticated:
        token = connection.get("token")
        if not isinstance(token, str) or not token:
            raise RemoteError("remote Observer connection has no credential")
        headers["Authorization"] = f"Bearer {token}"
    failures: list[str] = []
    for endpoint in connection.get("endpoints") or []:
        parsed = urlsplit(str(endpoint))
        if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
            failures.append(f"{endpoint}: invalid HTTPS endpoint")
            continue
        client = _PinnedHTTPSConnection(
            parsed.hostname, parsed.port, str(connection["fingerprint"]), timeout
        )
        try:
            client.request("POST", path, body=body, headers=headers)
            response = client.getresponse()
            raw = response.read(256 * 1024)
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RemoteError(
                    f"remote ingest returned invalid JSON (HTTP {response.status})"
                ) from exc
            if not isinstance(result, dict):
                raise RemoteError("remote ingest returned an invalid response")
            if 200 <= response.status < 300:
                return result
            raise RemoteHTTPError(
                response.status,
                str(
                    result.get("error")
                    or f"remote ingest returned HTTP {response.status}"
                ),
            )
        except RemoteHTTPError:
            raise
        except (
            OSError,
            TimeoutError,
            http.client.HTTPException,
            ssl.SSLError,
            RemoteError,
        ) as exc:
            failures.append(f"{endpoint}: {exc}")
        finally:
            client.close()
    detail = "; ".join(failures) if failures else "no configured endpoint"
    raise RemoteError(f"could not reach home Observer over LAN/Tailscale: {detail}")


def accept_invite(
    config: ObserverConfig,
    invite: str,
    *,
    provider: str,
    display_name: str | None = None,
) -> dict[str, Any]:
    decoded = decode_invite(invite)
    temporary = {
        "endpoints": decoded["endpoints"],
        "fingerprint": decoded["fingerprint"],
    }
    accepted = _request(
        temporary,
        "/v1/accept",
        {
            "secret": decoded["secret"],
            "display_name": display_name or socket.gethostname(),
            "provider": provider,
        },
        authenticated=False,
    )
    connection = {
        "protocol": REMOTE_PROTOCOL,
        "node_id": accepted["node_id"],
        "display_name": accepted.get("display_name")
        or display_name
        or socket.gethostname(),
        "provider": provider,
        "endpoints": decoded["endpoints"],
        "fingerprint": decoded["fingerprint"],
        "token": accepted["token"],
        "upload_epoch": int(accepted["upload_epoch"]),
        "last_revision": 0,
        "accepted_at": time.time(),
        "last_uploaded_at": None,
        "last_error": None,
    }
    _atomic_json(connection_path(config), connection)
    return connection


def claim_uploader(config: ObserverConfig) -> dict[str, Any]:
    connection = load_connection(config)
    if connection is None:
        raise RemoteError(
            "this Observer workspace is not enrolled with a home dashboard"
        )
    claimed = _request(connection, "/v1/claim", {})
    connection["upload_epoch"] = int(claimed["upload_epoch"])
    connection["last_error"] = None
    connection["claimed_at"] = time.time()
    _atomic_json(connection_path(config), connection)
    return connection


PROJECT_FIELDS = {
    "project_id",
    "display_path",
    "resolved_path",
    "worktree_root",
    "added_at",
    "current_branch",
    "branch_sampled_at",
}
SESSION_FIELDS = {
    "project_id",
    "provider",
    "session_id",
    "source_id",
    "title",
    "current_cwd",
    "last_activity_at",
    "last_source_at",
    "last_kind",
    "last_message_role",
    "last_message_excerpt",
    "last_turn_state",
    "awaiting_completion",
    "awaiting_since",
}
SOURCE_FIELDS = {
    "source_id",
    "provider",
    "session_id",
    "generation",
    "committed_offset",
    "current_cwd",
    "current_project_id",
    "message_mode",
    "monitoring",
    "health",
    "health_detail",
    "malformed_count",
    "unknown_count",
    "last_observation_at",
    "last_reconciled_at",
}
FINDING_FIELDS = {
    "finding_id",
    "project_id",
    "provider",
    "session_id",
    "kind",
    "state",
    "seen",
    "created_at",
    "updated_at",
    "evidence_observation_id",
    "summary",
    "details",
}
CHANGE_FIELDS = {
    "change_id",
    "project_id",
    "kind",
    "old_value",
    "new_value",
    "observed_at",
}
REVIEW_FIELDS = {
    "job_id",
    "project_id",
    "analyzer_provider",
    "analyzer_model",
    "status",
    "created_at",
    "submitted_at",
    "summary",
    "items",
    "limitations",
    "error",
    "source_id",
    "source_generation",
    "source_offset",
    "input_hash",
    "lease_epoch",
    "packet_meta",
}
ANALYZER_FIELDS = {
    "singleton",
    "epoch",
    "provider",
    "model",
    "session_id",
    "state",
    "acquired_at",
    "heartbeat_at",
    "current_job_id",
    "stop_reason",
    "allow_cross_provider",
    "lease_epoch",
    "queued_jobs",
    "queued_sessions",
    "coalesced_sessions",
    "last_accepted_review_at",
}


def _pick(value: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {key: value.get(key) for key in fields if key in value}


def snapshot_projection(observer: Observer) -> dict[str, Any]:
    raw = observer.db.status(time.time())
    projects: list[dict[str, Any]] = []
    for raw_project in raw["projects"][:200]:
        project = _pick(raw_project, PROJECT_FIELDS)
        project["sessions"] = [
            _pick(item, SESSION_FIELDS)
            for item in raw_project.get("sessions", [])[:200]
            if isinstance(item, dict)
        ]
        project["findings"] = [
            _pick(item, FINDING_FIELDS)
            for item in raw_project.get("findings", [])[:500]
            if isinstance(item, dict)
        ]
        project["sources"] = [
            _pick(item, SOURCE_FIELDS)
            for item in raw_project.get("sources", [])[:200]
            if isinstance(item, dict)
        ]
        project["changes"] = [
            _pick(item, CHANGE_FIELDS)
            for item in raw_project.get("changes", [])[:20]
            if isinstance(item, dict)
        ]
        review = raw_project.get("review")
        project["review"] = (
            _pick(review, REVIEW_FIELDS) if isinstance(review, dict) else None
        )
        projects.append(project)
    return {"generated_at": raw["generated_at"], "projects": projects}


def build_snapshot(observer: Observer, connection: dict[str, Any]) -> dict[str, Any]:
    supervisor = observer.db.supervisor_status()
    return {
        "schema_version": SNAPSHOT_SCHEMA,
        "node_id": connection["node_id"],
        "display_name": connection.get("display_name") or socket.gethostname(),
        "provider": supervisor.get("provider") or connection.get("provider"),
        "upload_epoch": int(connection["upload_epoch"]),
        "revision": int(connection.get("last_revision") or 0) + 1,
        "generated_at": time.time(),
        "projection": snapshot_projection(observer),
        "analyzer": supervisor,
    }


def _snapshot_hash(snapshot: dict[str, Any]) -> str:
    content = {key: value for key, value in snapshot.items() if key != "snapshot_hash"}
    raw = json.dumps(content, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_snapshot(value: dict[str, Any]) -> tuple[str, str]:
    required = {
        "schema_version",
        "node_id",
        "display_name",
        "upload_epoch",
        "revision",
        "generated_at",
        "projection",
        "analyzer",
        "snapshot_hash",
    }
    if not required.issubset(value):
        raise RemoteError("remote snapshot is missing required fields")
    if set(value) - (required | {"provider"}):
        raise RemoteError("remote snapshot contains unsupported top-level fields")
    if value["schema_version"] != SNAPSHOT_SCHEMA:
        raise RemoteError("remote snapshot schema version is unsupported")
    if not isinstance(value["node_id"], str) or not value["node_id"].startswith(
        "node_"
    ):
        raise RemoteError("remote snapshot has an invalid node identity")
    if not isinstance(value["display_name"], str) or not value["display_name"].strip():
        raise RemoteError("remote snapshot has no display hostname")
    if len(value["display_name"]) > 160:
        raise RemoteError("remote snapshot hostname is too long")
    if any(not character.isprintable() for character in value["display_name"]):
        raise RemoteError("remote snapshot hostname contains control characters")
    if not isinstance(value["upload_epoch"], int) or value["upload_epoch"] < 1:
        raise RemoteError("remote snapshot has an invalid uploader epoch")
    if not isinstance(value["revision"], int) or value["revision"] < 1:
        raise RemoteError("remote snapshot has an invalid revision")
    if not isinstance(value["generated_at"], (int, float)):
        raise RemoteError("remote snapshot has an invalid generation time")
    projection = value["projection"]
    if not isinstance(projection, dict) or not isinstance(
        projection.get("projects"), list
    ):
        raise RemoteError("remote snapshot projection is invalid")
    if set(projection) - {"generated_at", "projects"}:
        raise RemoteError("remote snapshot projection contains unsupported fields")
    analyzer = value["analyzer"]
    if not isinstance(analyzer, dict) or set(analyzer) - ANALYZER_FIELDS:
        raise RemoteError("remote snapshot analyzer state is invalid")
    if value.get("provider") not in {None, "claude", "codex"}:
        raise RemoteError("remote snapshot analyzer provider is invalid")
    if len(projection["projects"]) > 200:
        raise RemoteError("remote snapshot contains too many projects")
    total_sessions = 0
    total_findings = 0
    total_sources = 0
    for project in projection["projects"]:
        if not isinstance(project, dict) or not isinstance(
            project.get("project_id"), str
        ):
            raise RemoteError("remote snapshot contains an invalid project")
        allowed_project = PROJECT_FIELDS | {
            "sessions",
            "findings",
            "sources",
            "changes",
            "review",
        }
        if set(project) - allowed_project:
            raise RemoteError("remote snapshot project contains unsupported fields")
        for field in ("sessions", "findings", "sources", "changes"):
            if not isinstance(project.get(field), list):
                raise RemoteError(f"remote project {field} must be a list")
        total_sessions += len(project["sessions"])
        total_findings += len(project["findings"])
        total_sources += len(project["sources"])
        for session in project["sessions"]:
            if not isinstance(session, dict) or set(session) - SESSION_FIELDS:
                raise RemoteError("remote snapshot session contains unsupported fields")
        for finding in project["findings"]:
            if not isinstance(finding, dict) or set(finding) - FINDING_FIELDS:
                raise RemoteError("remote snapshot finding contains unsupported fields")
        for source in project["sources"]:
            if not isinstance(source, dict) or set(source) - SOURCE_FIELDS:
                raise RemoteError("remote snapshot source contains unsupported fields")
        for change in project["changes"]:
            if not isinstance(change, dict) or set(change) - CHANGE_FIELDS:
                raise RemoteError("remote snapshot change contains unsupported fields")
        review = project.get("review")
        if review is not None:
            if not isinstance(review, dict):
                raise RemoteError("remote project review must be an object")
            if set(review) - REVIEW_FIELDS:
                raise RemoteError("remote snapshot review contains unsupported fields")
            items = review.get("items") or []
            if not isinstance(items, list) or len(items) > 3:
                raise RemoteError("remote semantic review exceeds the three-item limit")
            for item in items:
                if not isinstance(item, dict):
                    raise RemoteError("remote semantic review item is invalid")
                if item.get("message_ref") and not item.get("evidence_excerpt"):
                    raise RemoteError(
                        "remote semantic citation has no evidence excerpt"
                    )
    if total_sessions > 2000 or total_findings > 5000 or total_sources > 2000:
        raise RemoteError("remote snapshot exceeds node cardinality limits")
    expected = _snapshot_hash(value)
    supplied = str(value["snapshot_hash"])
    if not hmac.compare_digest(expected, supplied):
        raise RemoteError("remote snapshot content hash is invalid")
    canonical = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(canonical.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise RemoteError("remote snapshot exceeds the 2 MiB limit")
    return canonical, expected


def push_snapshot(config: ObserverConfig) -> dict[str, Any]:
    connection = load_connection(config)
    if connection is None:
        return {"state": "not_enrolled"}
    try:
        with Observer(config) as observer:
            snapshot = build_snapshot(observer, connection)
        snapshot["snapshot_hash"] = _snapshot_hash(snapshot)
        result = _request(connection, "/v1/snapshot", snapshot)
        connection["last_revision"] = int(result["revision"])
        connection["last_uploaded_at"] = float(result["received_at"])
        connection["last_error"] = None
        _atomic_json(connection_path(config), connection)
        return {"state": "uploaded", **result}
    except (OSError, RemoteError, ValueError) as exc:
        connection["last_error"] = str(exc)
        connection["last_attempt_at"] = time.time()
        _atomic_json(connection_path(config), connection)
        return {"state": "disconnected", "error": str(exc)}


def remote_connection_status(config: ObserverConfig) -> dict[str, Any]:
    connection = load_connection(config)
    if connection is None:
        return {"state": "not_enrolled"}
    result = {
        key: connection.get(key)
        for key in (
            "node_id",
            "display_name",
            "provider",
            "endpoints",
            "upload_epoch",
            "last_revision",
            "accepted_at",
            "claimed_at",
            "last_uploaded_at",
            "last_attempt_at",
            "last_error",
        )
    }
    result["state"] = (
        "connected" if not connection.get("last_error") else "disconnected"
    )
    return result
