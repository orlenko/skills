from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import hashlib
from pathlib import Path
from threading import Event
from typing import Any

from .service import Observer, ObserverConfig


_BACKGROUND_PROCESSES: list[subprocess.Popen[bytes]] = []


def _runtime_dir(config: ObserverConfig) -> Path:
    path = config.state_dir / "runtime"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _instance_registry_path() -> Path:
    configured = os.environ.get("AGENT_OBSERVER_INSTANCE_REGISTRY")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "agent-observer-active.json"


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


def ensure_auth_token(config: ObserverConfig) -> str:
    path = _runtime_dir(config) / "auth-token"
    try:
        token = path.read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    return token


def issue_bootstrap_token(config: ObserverConfig) -> str:
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    directory = _runtime_dir(config) / "bootstrap"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    now = time.time()
    for candidate in directory.glob("*.json"):
        value = _read_json(candidate) or {}
        if float(value.get("expires_at") or 0) < now:
            try:
                candidate.unlink()
            except OSError:
                pass
    _atomic_json(directory / f"{digest}.json", {"expires_at": now + 300})
    return token


def consume_bootstrap_token(config: ObserverConfig, token: str) -> bool:
    if not token:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    path = _runtime_dir(config) / "bootstrap" / f"{digest}.json"
    value = _read_json(path)
    if not value or float(value.get("expires_at") or 0) < time.time():
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def _alive(pid: Any) -> bool:
    try:
        number = int(pid)
        if number <= 1:
            return False
        os.kill(number, 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _base_command(config: ObserverConfig) -> list[str]:
    executable = Path(__file__).resolve().parents[1] / "bin" / "agent-observer"
    command = (
        [sys.executable, str(executable)]
        if executable.is_file()
        else [sys.executable, "-m", "agent_observer"]
    )
    command.extend(("--state-dir", str(config.state_dir)))
    for root in config.claude_roots:
        command.extend(("--claude-root", str(root)))
    for root in config.codex_roots:
        command.extend(("--codex-root", str(root)))
    return command


def _spawn(config: ObserverConfig, kind: str, extra: list[str]) -> int:
    _BACKGROUND_PROCESSES[:] = [
        process for process in _BACKGROUND_PROCESSES if process.poll() is None
    ]
    runtime = _runtime_dir(config)
    log_path = runtime / f"{kind}.log"
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [*_base_command(config), kind, *extra],
            cwd=runtime,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        _BACKGROUND_PROCESSES.append(process)
    finally:
        log.close()
    return process.pid


def service_status(config: ObserverConfig) -> dict[str, Any]:
    runtime = _runtime_dir(config)
    server = _read_json(runtime / "server.json") or {}
    daemon = _read_json(runtime / "daemon.json") or {}
    server_running = _alive(server.get("pid"))
    daemon_running = _alive(daemon.get("pid"))
    result: dict[str, Any] = {
        "server": {**server, "running": server_running},
        "daemon": {**daemon, "running": daemon_running},
    }
    if server_running and server.get("port"):
        result["dashboard_url"] = f"http://127.0.0.1:{int(server['port'])}/"
    return result


def start_services(config: ObserverConfig) -> dict[str, Any]:
    runtime = _runtime_dir(config)
    ensure_auth_token(config)
    status = service_status(config)
    if not status["daemon"]["running"]:
        try:
            (runtime / "daemon.json").unlink()
        except OSError:
            pass
        _spawn(config, "daemon", [])
    if not status["server"]["running"]:
        try:
            (runtime / "server.json").unlink()
        except OSError:
            pass
        _spawn(config, "serve", ["--port", str(_free_port())])

    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = service_status(config)
        if status["server"]["running"] and status["daemon"]["running"]:
            port = int(status["server"]["port"])
            bootstrap = issue_bootstrap_token(config)
            status["dashboard_url"] = f"http://127.0.0.1:{port}/?bootstrap={bootstrap}"
            return status
        time.sleep(0.05)
    stop_services(config)
    raise OSError(
        f"observer sidecars did not become ready; inspect the runtime logs in {runtime}"
    )


def claim_active_instance(config: ObserverConfig) -> dict[str, Any]:
    current = str(config.state_dir.resolve())
    registry = _instance_registry_path()
    registered = _read_json(registry) or {}
    candidates: list[Path] = []
    prior = registered.get("state_dir")
    if isinstance(prior, str) and prior:
        candidates.append(Path(prior).expanduser())
    if not os.environ.get("AGENT_OBSERVER_INSTANCE_REGISTRY"):
        legacy = Path.home() / ".local" / "state" / "agent-observer"
        if legacy.exists():
            candidates.append(legacy)
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state_dir in candidates:
        resolved = str(state_dir.resolve())
        if resolved == current or resolved in seen:
            continue
        seen.add(resolved)
        old_config = ObserverConfig.defaults(resolved)
        services = service_status(old_config)
        if services["server"]["running"] or services["daemon"]["running"]:
            try:
                with Observer(old_config) as observer:
                    observer.db.revoke_supervisor("another workspace took over")
            except (OSError, sqlite3.DatabaseError, ValueError):
                pass
            cleaned.append(
                {"state_dir": resolved, "services": stop_services(old_config)}
            )
    _atomic_json(
        registry,
        {"state_dir": current, "claimed_at": time.time(), "pid": os.getpid()},
    )
    return {"state_dir": current, "cleaned": cleaned}


def release_active_instance(config: ObserverConfig) -> bool:
    registry = _instance_registry_path()
    value = _read_json(registry) or {}
    if value.get("state_dir") != str(config.state_dir.resolve()):
        return False
    try:
        registry.unlink()
    except OSError:
        return False
    return True


def _owned_process(pid: int, kind: str) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    command = result.stdout
    owns_observer = "agent-observer" in command or "agent_observer" in command
    return owns_observer and kind in command.split()


def stop_services(config: ObserverConfig) -> dict[str, Any]:
    runtime = _runtime_dir(config)
    stopped: list[str] = []
    refused: list[str] = []
    for kind in ("server", "daemon"):
        path = runtime / f"{kind}.json"
        info = _read_json(path) or {}
        try:
            pid = int(info.get("pid"))
        except (TypeError, ValueError):
            pid = 0
        if pid and _alive(pid):
            process_kind = "serve" if kind == "server" else "daemon"
            if not _owned_process(pid, process_kind):
                refused.append(kind)
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for process in _BACKGROUND_PROCESSES:
                if process.pid == pid:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            deadline = time.monotonic() + 3
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            if _alive(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            stopped.append(kind)
        try:
            path.unlink()
        except OSError:
            pass
    return {"stopped": stopped, "refused": refused}


def run_daemon(
    config: ObserverConfig,
    *,
    interval: float = 2.0,
    rescan_interval: float = 300.0,
) -> int:
    if interval < 0.25:
        raise ValueError("daemon interval must be at least 0.25 seconds")
    if rescan_interval < 30:
        raise ValueError("rescan interval must be at least 30 seconds")
    stopping = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    info_path = _runtime_dir(config) / "daemon.json"
    started_at = time.time()
    next_rescan = time.monotonic() + rescan_interval
    with Observer(config) as observer:
        while not stopping.is_set():
            error: str | None = None
            scan: dict[str, Any] = {}
            try:
                scan = observer.scan()
                if time.monotonic() >= next_rescan:
                    for project in observer.db.projects():
                        observer.rescan_project(str(project["project_id"]))
                    next_rescan = time.monotonic() + rescan_interval
            except (OSError, ValueError, KeyError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            _atomic_json(
                info_path,
                {
                    "pid": os.getpid(),
                    "started_at": started_at,
                    "heartbeat_at": time.time(),
                    "last_scan": scan,
                    "error": error,
                },
            )
            stopping.wait(interval)
    return 0


def write_server_info(config: ObserverConfig, *, port: int) -> Path:
    path = _runtime_dir(config) / "server.json"
    _atomic_json(
        path,
        {"pid": os.getpid(), "started_at": time.time(), "port": port},
    )
    return path
