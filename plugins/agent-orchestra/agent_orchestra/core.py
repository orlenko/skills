from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 256 * 1024
# A pending page carries up to PENDING_RESPONSE_BYTES of envelopes, so a reply
# read cap of one message would truncate a legitimate inbox page into "invalid
# JSON" forever. Only error bodies keep the small cap.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = MAX_MESSAGE_BYTES * 2
INVITE_PREFIX = "or1."
AGENT_ANCESTOR_LEVELS = 20
AGENT_COMMAND_MARKERS = ("claude", "codex")
# A tool call runs its command in a shell whose own command line carries the
# agent's plugin, state, and snapshot paths, so "claude" appears in the args of
# a process that exits with the command. Anchoring ownership there records a pid
# that is dead a second later, so a shell is never the agent whatever it quotes.
AGENT_SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash", "busybox"}
)
# `claude -p` and `codex exec` run one task and exit. codex spells `-p` for
# `--profile`, so its own name buys it the narrower reading; a wrapper name
# says nothing, and there both vocabularies apply.
AGENT_ONE_SHOT_CLAUDE = frozenset({"-p", "--print"})
AGENT_ONE_SHOT_CODEX = frozenset({"exec"})
# A process with a deadline is a bad owner while it is still alive: the seat it
# takes dies when the clock runs out. `timeout 5400 aiq run claude -- -p …` is
# how one fleet runs its producer, and every agent process inside that tree
# inherits the same deadline whatever its own arguments say.
AGENT_DEADLINE_COMMANDS = frozenset({"timeout", "gtimeout"})

BUCKETS = ("pending", "claimed", "done", "outbox", "sent", "events")

MESSAGE_ID_RE = r"m_[A-Za-z0-9_-]{8,96}"
MEMBER_ID_RE = r"mb_[A-Za-z0-9_-]{4,40}"
TASK_ID_RE = r"t_[A-Za-z0-9._-]{1,64}"


class OrchestraError(RuntimeError):
    """A user-actionable agent-orchestra error."""


class APIError(OrchestraError):
    def __init__(self, status: int, message: str, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


def now() -> float:
    return time.time()


def token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secret_matches(value: str, expected_hash: str | None) -> bool:
    return bool(expected_hash) and hmac.compare_digest(secret_hash(value), expected_hash)


def new_orchestra_id() -> str:
    return "orc_" + secrets.token_hex(8)


def new_member_id() -> str:
    return "mb_" + secrets.token_hex(6)


def new_message_id() -> str:
    return "m_" + secrets.token_hex(16)


_SOURCES_DIGEST: str | None = None


def sources_digest() -> str:
    """sha256 over this package's sources, computed once per process.

    A tree's version string can match while its code does not — a working
    checkout beside an installed copy, or a build that carries its own metadata
    (`0.1.6+codex.20260910` is the same release as `0.1.6`). The digest answers
    the question the path and the version cannot: is this the same code.
    """
    global _SOURCES_DIGEST
    if _SOURCES_DIGEST is None:
        digest = hashlib.sha256()
        try:
            for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
                digest.update(path.name.encode("utf-8"))
                digest.update(path.read_bytes())
            _SOURCES_DIGEST = digest.hexdigest()
        except OSError:
            _SOURCES_DIGEST = ""
    return _SOURCES_DIGEST


def state_root() -> Path:
    explicit = os.environ.get("AGENT_ORCHESTRA_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (base / "agent-orchestra").resolve()


def ensure_private_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except FileExistsError:
        # EEXIST is the answer this asked for. `exist_ok` swallows it only when
        # `Path.is_dir()` agrees, and is_dir() answers False on any stat error,
        # so a sandbox that lets a hook write the state directory but not stat
        # it turned an existing directory into a traceback in every turn.
        pass
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError as exc:
        raise OrchestraError(f"State file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestraError(f"Could not read state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestraError(f"Invalid object in state file: {path}")
    return value


def hub_dir(orchestra_id: str) -> Path:
    root = ensure_private_dir(state_root())
    return ensure_private_dir(ensure_private_dir(root / "hubs") / safe_id(orchestra_id))


def member_dir(member_id: str) -> Path:
    root = ensure_private_dir(state_root())
    return ensure_private_dir(ensure_private_dir(root / "members") / safe_id(member_id))


def member_path(member_id: str) -> Path:
    return member_dir(member_id) / "member.json"


def bucket_dir(member_id: str, bucket: str) -> Path:
    if bucket not in BUCKETS:
        raise OrchestraError(f"Invalid mailbox bucket: {bucket}")
    return ensure_private_dir(member_dir(member_id) / bucket)


def runtime_dir() -> Path:
    return ensure_private_dir(ensure_private_dir(state_root()) / "runtime")


def binding_records() -> list[tuple[Path, dict[str, Any]]]:
    """Every session binding on this machine, with the file that holds it.

    hooks.py writes them; member.py reads them to see whether a live session
    already holds a membership. Both need the same view, and hooks.py imports
    member.py, so the reader lives here.
    """
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in runtime_dir().glob("binding-*.json"):
        try:
            rows.append((path, read_json(path)))
        except OrchestraError:
            continue
    return rows


def safe_id(value: str) -> str:
    if not value or len(value) > 160:
        raise OrchestraError("Invalid identifier")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        raise OrchestraError("Invalid identifier")
    return value


def normalize_provider(provider: str) -> str:
    value = provider.strip().lower()
    aliases = {
        "claude-code": "claude",
        "anthropic": "claude",
        "openai": "codex",
    }
    value = aliases.get(value, value)
    if value not in {"codex", "claude", "cli", "test"}:
        raise OrchestraError("Provider must be codex, claude, cli, or test")
    return value


def instance_key(provider: str, cwd: str | Path) -> str:
    canonical = str(Path(cwd).expanduser().resolve())
    material = f"{normalize_provider(provider)}\0{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def encode_invite(payload: dict[str, Any]) -> str:
    document = {"v": PROTOCOL_VERSION, **payload}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return INVITE_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_invite(invite: str) -> dict[str, Any]:
    value = invite.strip()
    if not value.startswith(INVITE_PREFIX):
        raise OrchestraError(f"Invite must start with {INVITE_PREFIX}")
    encoded = value[len(INVITE_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrchestraError("Invite is malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != PROTOCOL_VERSION:
        raise OrchestraError("Invite protocol version is unsupported")
    required = {
        "orchestra_id",
        "endpoints",
        "fingerprint",
        "secret",
        "expires_at",
        "role",
        "parent",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise OrchestraError(f"Invite is missing required fields: {', '.join(missing)}")
    if not isinstance(payload["endpoints"], list) or not payload["endpoints"]:
        raise OrchestraError("Invite has no endpoints")
    # A crafted invite reaches this from an untrusted string, so every value is
    # checked before it is used; a bad one is malformed, never a traceback.
    if not all(isinstance(item, str) and item for item in payload["endpoints"]):
        raise OrchestraError("Invite is malformed")
    if not all(isinstance(payload[key], str) and payload[key] for key in ("fingerprint", "secret", "role")):
        raise OrchestraError("Invite is malformed")
    if payload["parent"] is not None and not isinstance(payload["parent"], str):
        raise OrchestraError("Invite is malformed")
    try:
        expires_at = float(payload["expires_at"])
    except (TypeError, ValueError) as exc:
        raise OrchestraError("Invite is malformed") from exc
    if expires_at != expires_at:  # NaN compares false against every bound
        raise OrchestraError("Invite is malformed")
    if expires_at <= now():
        raise OrchestraError("Invite has expired")
    if not isinstance(payload["orchestra_id"], str):
        raise OrchestraError("Invite is malformed")
    try:
        safe_id(payload["orchestra_id"])
    except OrchestraError as exc:
        raise OrchestraError("Invite is malformed") from exc
    return payload


def certificate_fingerprint(cert_path: Path) -> str:
    try:
        pem = cert_path.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise OrchestraError(f"Could not read TLS certificate: {exc}") from exc
    return hashlib.sha256(der).hexdigest()


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
            raise OrchestraError(
                "TLS fingerprint mismatch; the endpoint is not the hub from the invite"
            )


def _read_response_body(response: Any, limit: int) -> bytes:
    """Read a whole response body, or say it was too large.

    Reading exactly `limit` bytes and parsing what came back turns an oversized
    body into a permanent "invalid JSON" error; a caller can act on a size
    error, so read one byte past the cap and report it.
    """
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise OrchestraError(f"Hub response is larger than {limit} bytes")
    return raw


def _ps_field(flag: str, pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", flag, "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else None


def _command_name(args: str) -> str:
    tokens = args.split()
    return Path(tokens[0]).name.lower() if tokens else ""


def _is_agent_command(args: str) -> bool:
    if _command_name(args) in AGENT_SHELL_COMMANDS:
        return False
    lowered = args.lower()
    return any(marker in lowered for marker in AGENT_COMMAND_MARKERS)


def agent_ancestor_pid(pid: int | None = None) -> int | None:
    """The pid of the nearest ancestor process that is a coding agent.

    Membership ownership is anchored to it: a `claude -p` child started from the
    owning session has a different agent ancestor, so its hooks never claim the
    membership the parent session joined with.
    """
    try:
        current = os.getpid() if pid is None else int(pid)
    except (TypeError, ValueError):
        return None
    for _ in range(AGENT_ANCESTOR_LEVELS):
        parent = _ps_field("ppid=", current)
        if not parent:
            return None
        try:
            current = int(parent)
        except ValueError:
            return None
        if current <= 1:
            return None
        if _is_agent_command(_ps_field("args=", current) or ""):
            return current
    return None


def _is_one_shot_agent(pid: int) -> bool:
    """True when this agent process runs one task and exits."""
    tokens = (_ps_field("args=", pid) or "").split()
    if not tokens:
        return False
    command = Path(tokens[0]).name.lower()
    if "codex" in command:
        one_shot = AGENT_ONE_SHOT_CODEX
    elif "claude" in command:
        one_shot = AGENT_ONE_SHOT_CLAUDE
    else:
        # A wrapper: `aiq run claude -- -p …`, `timeout 5400 claude -p …`. Its
        # own name says nothing, so both vocabularies apply. A missed repair
        # costs one command; a seat handed to a run that ends costs the wake.
        one_shot = AGENT_ONE_SHOT_CODEX | AGENT_ONE_SHOT_CLAUDE
    for token in tokens[1:]:
        if token.startswith("{"):
            # An inline settings blob (`claude --settings {...}`) is data. The
            # hook commands inside it carry flags that are not this process's.
            break
        if token in one_shot:
            return True
    return False


def _under_deadline(pid: int) -> bool:
    """True when a deadline supervisor sits above this agent process.

    The one-shot reader answers for one command line. A deadline covers a whole
    subtree: a session the producer spawns inside `timeout 5400 …` carries no
    `-p` of its own and still dies on the same clock.
    """
    current = pid
    for _ in range(AGENT_ANCESTOR_LEVELS):
        parent = _ps_field("ppid=", current)
        if not parent:
            return False
        try:
            current = int(parent)
        except ValueError:
            return False
        if current <= 1:
            return False
        if _command_name(_ps_field("args=", current) or "") in AGENT_DEADLINE_COMMANDS:
            return True
    return False


def agent_session_pid(pid: int | None = None) -> int | None:
    """The agent ancestor, but only when it outlives the task it is running.

    A membership's wake-up is bound to this pid, so a run that ends must never
    take the seat: it exits with its task and leaves the membership pointing at
    a dead process again. Two things end a run — its own one-shot arguments,
    and a deadline anywhere above it.
    """
    owner = agent_ancestor_pid(pid)
    if owner is None or _is_one_shot_agent(owner) or _under_deadline(owner):
        return None
    return owner


def api_request(
    connection: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
    query: dict[str, Any] | None = None,
    auth: bool = True,
) -> dict[str, Any]:
    endpoints = connection.get("endpoints")
    fingerprint = connection.get("fingerprint")
    if not isinstance(endpoints, list) or not endpoints or not isinstance(fingerprint, str):
        raise OrchestraError("Connection state is incomplete")
    if query:
        path = f"{path}?{urlencode(query)}"
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        auth_token = connection.get("token")
        if not isinstance(auth_token, str):
            raise OrchestraError("Connection has no authentication token")
        headers["Authorization"] = f"Bearer {auth_token}"

    failures: list[str] = []
    for endpoint in endpoints:
        parsed = urlsplit(str(endpoint))
        if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
            failures.append(f"{endpoint}: invalid HTTPS endpoint")
            continue
        client = _PinnedHTTPSConnection(parsed.hostname, parsed.port, fingerprint, timeout)
        try:
            client.request(method, path, body=body, headers=headers)
            response = client.getresponse()
            success = 200 <= response.status < 300
            raw = _read_response_body(
                response, MAX_RESPONSE_BYTES if success else MAX_ERROR_RESPONSE_BYTES
            )
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OrchestraError(
                    f"Hub returned invalid JSON from {endpoint}: HTTP {response.status}"
                ) from exc
            if not isinstance(result, dict):
                raise OrchestraError(f"Hub returned an invalid response from {endpoint}")
            if success:
                return result
            message = str(result.get("error") or f"Hub returned HTTP {response.status}")
            raise APIError(response.status, message, result)
        except APIError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, OrchestraError) as exc:
            failures.append(f"{endpoint}: {exc}")
        finally:
            client.close()
    detail = "; ".join(failures) if failures else "no usable endpoints"
    raise OrchestraError(f"Could not reach hub: {detail}")
