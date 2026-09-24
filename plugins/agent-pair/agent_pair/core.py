from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 256 * 1024
AGENT_ANCESTOR_LEVELS = 20
AGENT_COMMAND_MARKERS = ("claude", "codex")
# A tool call runs its command in a shell whose own command line carries the
# agent's plugin, state, and snapshot paths, so "claude" appears in the args of
# a process that exits with the command. Anchoring a binding there records a pid
# that is dead a second later, so a shell is never the agent whatever it quotes.
AGENT_SHELL_COMMANDS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash", "busybox"}
)


class AgentPairError(RuntimeError):
    """A user-actionable agent-pair error."""


class APIError(AgentPairError):
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


def state_root() -> Path:
    explicit = os.environ.get("AGENT_PAIR_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (base / "agent-pair").resolve()


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
        raise AgentPairError(f"State file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentPairError(f"Could not read state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AgentPairError(f"Invalid object in state file: {path}")
    return value


def pair_dir(pair_id: str) -> Path:
    root = ensure_private_dir(state_root())
    return ensure_private_dir(root / "pairs") / safe_id(pair_id)


def endpoint_path(endpoint_id: str) -> Path:
    root = ensure_private_dir(state_root())
    return ensure_private_dir(root / "endpoints") / f"{safe_id(endpoint_id)}.json"


def runtime_dir() -> Path:
    return ensure_private_dir(ensure_private_dir(state_root()) / "runtime")


def binding_records() -> list[tuple[Path, dict[str, Any]]]:
    """Every session binding on this machine, with the file that holds it."""
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in runtime_dir().glob("binding-*.json"):
        try:
            rows.append((path, read_json(path)))
        except AgentPairError:
            continue
    return rows


def inbox_dir(endpoint_id: str, bucket: str) -> Path:
    if bucket not in {"pending", "claimed", "done", "outbox", "sent"}:
        raise AgentPairError(f"Invalid mailbox bucket: {bucket}")
    root = ensure_private_dir(state_root())
    return ensure_private_dir(root / "mailboxes" / safe_id(endpoint_id) / bucket)


def safe_id(value: str) -> str:
    if not value or len(value) > 160:
        raise AgentPairError("Invalid identifier")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value):
        raise AgentPairError("Invalid identifier")
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
        raise AgentPairError("Provider must be codex, claude, cli, or test")
    return value


def instance_key(provider: str, cwd: str | Path) -> str:
    canonical = str(Path(cwd).expanduser().resolve())
    material = f"{normalize_provider(provider)}\0{canonical}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def encode_invite(payload: dict[str, Any]) -> str:
    document = {"v": PROTOCOL_VERSION, **payload}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "ap1." + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_invite(invite: str) -> dict[str, Any]:
    value = invite.strip()
    if not value.startswith("ap1."):
        raise AgentPairError("Invite must start with ap1.")
    encoded = value[4:]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentPairError("Invite is malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != PROTOCOL_VERSION:
        raise AgentPairError("Invite protocol version is unsupported")
    required = {"pair_id", "endpoints", "fingerprint", "secret", "expires_at"}
    if not required.issubset(payload):
        raise AgentPairError("Invite is missing required fields")
    if not isinstance(payload["endpoints"], list) or not payload["endpoints"]:
        raise AgentPairError("Invite has no endpoints")
    if float(payload["expires_at"]) <= now():
        raise AgentPairError("Invite has expired")
    safe_id(str(payload["pair_id"]))
    return payload


def certificate_fingerprint(cert_path: Path) -> str:
    try:
        pem = cert_path.read_text(encoding="utf-8")
        der = ssl.PEM_cert_to_DER_cert(pem)
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise AgentPairError(f"Could not read TLS certificate: {exc}") from exc
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
            raise AgentPairError(
                "TLS fingerprint mismatch; the endpoint is not the peer from the invite"
            )


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
        raise AgentPairError("Connection state is incomplete")
    if query:
        path = f"{path}?{urlencode(query)}"
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if auth:
        auth_token = connection.get("token")
        if not isinstance(auth_token, str):
            raise AgentPairError("Connection has no authentication token")
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
            raw = response.read(MAX_MESSAGE_BYTES * 2)
            try:
                result = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AgentPairError(
                    f"Peer returned invalid JSON from {endpoint}: HTTP {response.status}"
                ) from exc
            if not isinstance(result, dict):
                raise AgentPairError(f"Peer returned an invalid response from {endpoint}")
            if 200 <= response.status < 300:
                return result
            message = str(result.get("error") or f"Peer returned HTTP {response.status}")
            raise APIError(response.status, message, result)
        except APIError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError, AgentPairError) as exc:
            failures.append(f"{endpoint}: {exc}")
        finally:
            client.close()
    detail = "; ".join(failures) if failures else "no usable endpoints"
    raise AgentPairError(f"Could not reach peer: {detail}")


# libproc's proc_pidinfo(PROC_PIDTBSDINFO) fills a 136-byte proc_bsdinfo with
# pbi_ppid at offset 16; sysctl {CTL_KERN, KERN_PROCARGS2, pid} returns argc,
# the executable path, NUL padding, then the arguments.
_PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136
_PROC_BSDINFO_PPID_OFFSET = 16
_CTL_KERN = 1
_KERN_PROCARGS2 = 49
_LIBC: Any = None


def _libc() -> Any:
    global _LIBC
    if _LIBC is None:
        try:
            import ctypes
            import ctypes.util

            library = ctypes.CDLL(None, use_errno=True)
            if not hasattr(library, "proc_pidinfo"):
                library = ctypes.CDLL(ctypes.util.find_library("proc"), use_errno=True)
            library.proc_pidinfo  # noqa: B018 - AttributeError when absent
            _LIBC = library
        except (OSError, AttributeError, TypeError):
            _LIBC = False
    return _LIBC


def _native_field(flag: str, pid: int) -> str | None:
    """`ppid=` or `args=` for a process, read in-process without running ps.

    The nono `safe-claude` profile denies exec of /bin/ps (agent-orchestra hit
    this first), so inside it a ps-only walk never finds the agent ancestor.
    libproc and sysctl on macOS, and /proc on Linux, answer the same two
    questions inside it.
    """
    try:
        if sys.platform == "darwin":
            return _darwin_field(flag, int(pid))
        if sys.platform.startswith("linux"):
            return _linux_field(flag, int(pid))
    except (OSError, ValueError, AttributeError, IndexError, struct.error):
        return None
    return None


def _darwin_field(flag: str, pid: int) -> str | None:
    import ctypes

    library = _libc()
    if not library or pid <= 0:
        return None
    if flag == "ppid=":
        buffer = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
        size = library.proc_pidinfo(
            ctypes.c_int(pid), _PROC_PIDTBSDINFO, ctypes.c_uint64(0), buffer, _PROC_BSDINFO_SIZE
        )
        if size != _PROC_BSDINFO_SIZE:
            return None
        return str(struct.unpack_from("I", buffer.raw, _PROC_BSDINFO_PPID_OFFSET)[0])
    if flag == "args=":
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if library.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if library.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buffer.raw[: size.value]
        argc = struct.unpack_from("i", raw, 0)[0]
        rest = raw[4:]
        rest = rest[rest.index(b"\0") :].lstrip(b"\0")
        args = [part.decode("utf-8", "replace") for part in rest.split(b"\0")[:argc]]
        return " ".join(args) or None
    return None


def _linux_field(flag: str, pid: int) -> str | None:
    base = Path("/proc") / str(pid)
    if flag == "ppid=":
        stat = (base / "stat").read_text(encoding="utf-8", errors="replace")
        # The command name may hold spaces and parentheses; the fields after
        # the last ")" cannot.
        return stat[stat.rindex(")") + 2 :].split()[1]
    if flag == "args=":
        raw = (base / "cmdline").read_bytes()
        args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return " ".join(args) or None
    return None


def _ps_field(flag: str, pid: int) -> str | None:
    native = _native_field(flag, pid)
    if native is not None:
        return native
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


def _is_agent_command(args: str) -> bool:
    tokens = args.split()
    if tokens and Path(tokens[0]).name.lower() in AGENT_SHELL_COMMANDS:
        return False
    lowered = args.lower()
    return any(marker in lowered for marker in AGENT_COMMAND_MARKERS)


def agent_ancestor_pid(pid: int | None = None) -> int | None:
    """The pid of the nearest ancestor process that is a coding agent.

    A hook binding records it so that a binding whose session has exited reads
    as stale, instead of holding its endpoint away from every later session.
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
