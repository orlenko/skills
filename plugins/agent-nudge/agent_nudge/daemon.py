"""Find idle Claude and Codex sessions in tmux and ask them, once, whether they are done.

An agent often ends its turn short of its goal: it reports and stops, or says
it will act when a CI job finishes and then sets nothing to wake it. One line
from outside ("still waiting?") is usually enough to restart it. This loop
sends that line, with care:

- only to a pane whose screen has not changed for the idle threshold, whose
  input box is empty (dim suggestions do not count), and where no attached
  tmux client has typed recently;
- never while a dialog is open or the agent is mid-turn, and never when Jev
  reads the last message as a question for the user;
- once per stop. If a nudge is followed by another stop, the threshold for the
  next one triples, up to four hours. A stop that something else woke (a person,
  mail) resets it.

It starts in dry-run: it logs what it would have sent and types nothing until
the mode is `live`.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any

from . import judge, orchestra, screen, system

IDLE_ENV = "AGENT_NUDGE_IDLE_MINUTES"
QUIET_ENV = "AGENT_NUDGE_HUMAN_QUIET_MINUTES"
DEFAULT_IDLE_MINUTES = 10.0
DEFAULT_QUIET_MINUTES = 5.0
# Unread orchestra mail is a fact, not a guess: a session sitting on it is deaf.
MAIL_IDLE_MINUTES = 2.0
POLL_SECONDS = 30.0
WATCHER_FACTOR = 6
STREAK_FACTOR = 3
MAX_WAIT_SECONDS = 4 * 3600
DAILY_CAP = 12
# A change this soon after a nudge is the agent answering it.
RESPONSE_WINDOW_SECONDS = 180
# A change after a quiet spell this long, with no nudge before it, is a fresh start.
QUIET_SPELL_SECONDS = 60
LOG_CAP_BYTES = 20 * 1024 * 1024
MODES = ("dry-run", "live")

GENERIC = ("[agent-nudge] You have been idle for {minutes} min. Is your goal done, or are you "
           "blocked? If anything is still unblocked, continue with it. If you are waiting on "
           "someone or something, say what, and set up something that will wake you when it "
           "changes.")
MAIL = ("[agent-nudge] You have {unread} unread Agent Orchestra message{plural} (oldest {age} "
        "min) and have been idle for {minutes} min. Read your orchestra inbox and act on it.")
POINTED = ("[agent-nudge] You have been idle for {minutes} min after saying you would act when "
           "something changes, and nothing is watching for it. Check it now. If it is still "
           "pending, set up something that will wake you (a monitor, a scheduled wake-up, or a "
           "background wait) before you stop again.")


def home() -> Path:
    if os.environ.get("AGENT_NUDGE_HOME"):
        root = Path(os.environ["AGENT_NUDGE_HOME"])
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser() / "agent-nudge"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def env_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    return base / "agent-nudge" / "env"


def load_env_file() -> None:
    """KEY=VALUE lines, for a daemon started by launchd or systemd with a bare environment."""
    path = env_file()
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip().removeprefix("export ").strip(), value.strip().strip("'\""))


def _minutes(env: str, default: float) -> float:
    try:
        value = float(os.environ.get(env, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def mode() -> str:
    try:
        value = (home() / "mode").read_text().strip()
    except OSError:
        return "dry-run"
    return value if value in MODES else "dry-run"


def set_mode(value: str) -> None:
    if value not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    (home() / "mode").write_text(value + "\n")


def log_path() -> Path:
    return home() / "log.jsonl"


def append_log(row: dict[str, Any]) -> None:
    path = log_path()
    if path.exists() and path.stat().st_size > LOG_CAP_BYTES:
        path.replace(path.with_suffix(".jsonl.1"))
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def load_state() -> dict[str, dict[str, Any]]:
    try:
        return json.loads((home() / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    path = home() / "state.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _required_idle(rec: dict[str, Any], base: float) -> tuple[float, int]:
    streak = rec.get("streak", 0) + 1 if rec.get("run_from_nudge") else 0
    return min(base * STREAK_FACTOR ** streak, MAX_WAIT_SECONDS), streak


class Nudger:
    def __init__(self, *, now=time.time, sleep=time.sleep):
        self.now = now
        self.sleep = sleep
        self.state = load_state()

    def tick(self) -> list[dict[str, Any]]:
        """One pass over every agent pane. Returns the rows it logged."""
        at = self.now()
        table = system.process_table()
        activity = system.client_activity()
        try:
            self.seats = orchestra.seats_by_pid()
        except Exception:  # noqa: BLE001 - orchestra state is a bonus, never a requirement
            self.seats = {}
        rows, seen = [], set()
        for pane in system.panes():
            system.find_agent(pane, table)
            if not pane.agent:
                continue
            key = f"{pane.id}:{pane.agent_pid}"
            seen.add(key)
            rec = self.state.setdefault(key, {"agent": pane.agent, "path": pane.path})
            row = self._pane(pane, rec, at, activity)
            if row is not None:
                rows.append(row)
        for gone in set(self.state) - seen:
            del self.state[gone]
        save_state(self.state)
        return rows

    def _pane(self, pane: system.Pane, rec: dict[str, Any], at: float,
              activity: dict[str, float]) -> dict[str, Any] | None:
        try:
            scr = screen.parse(system.capture(pane.id), pane.agent)
        except Exception as exc:  # noqa: BLE001 - one bad pane never stops the rest
            return self._note(pane, rec, at, "skip", f"capture failed: {type(exc).__name__}")
        if scr.body_hash != rec.get("hash"):
            quiet_before = at - rec.get("changed_at", at)
            nudged_at = rec.get("last_nudge_at")
            if rec.get("awaiting") and nudged_at and at - nudged_at <= RESPONSE_WINDOW_SECONDS:
                rec["run_from_nudge"] = True
            elif quiet_before >= QUIET_SPELL_SECONDS or "hash" not in rec:
                rec["run_from_nudge"] = False
                rec["streak"] = 0
            rec["awaiting"] = False
            rec["hash"], rec["changed_at"] = scr.body_hash, at
            rec.pop("reason", None)
            return None
        idle = at - rec["changed_at"]
        if rec.get("run_from_nudge") and not rec.get("outcome_logged") and idle >= QUIET_SPELL_SECONDS:
            rec["outcome_logged"] = True
            run = max(0.0, rec["changed_at"] - rec.get("last_nudge_at", rec["changed_at"]))
            append_log({"ts": at, "event": "outcome", "pane": pane.id, "agent": pane.agent,
                        "path": pane.path, "ran_seconds": round(run), "streak": rec.get("streak", 0)})
        if pane.opt_out:
            return self._note(pane, rec, at, "skip", "opted out (@nudge off)")
        if not scr.has_prompt:
            return self._note(pane, rec, at, "skip", "no input box: dialog open or agent exited")
        if scr.working_marker:
            return None
        seat = getattr(self, "seats", {}).get(pane.agent_pid)
        mail = bool(seat and seat.unread)
        base = (MAIL_IDLE_MINUTES if mail else _minutes(IDLE_ENV, DEFAULT_IDLE_MINUTES)) * 60
        needed, streak = _required_idle(rec, base)
        if scr.watchers and not mail:
            needed = min(needed * WATCHER_FACTOR, MAX_WAIT_SECONDS)
        if idle < needed:
            return None
        if seat and not mail and not seat.open_tasks and seat.role != "conductor":
            return self._note(pane, rec, at, "skip", f"orchestra player {seat.name} has nothing open or unread")
        if rec.get("last_nudge_hash") == scr.body_hash:
            return None
        if scr.typed:
            return self._note(pane, rec, at, "skip", "text typed in the input box")
        quiet = _minutes(QUIET_ENV, DEFAULT_QUIET_MINUTES) * 60
        if at - activity.get(pane.session, 0.0) < quiet:
            return self._note(pane, rec, at, "skip", "a person is active in this tmux session")
        recent = [t for t in rec.get("nudges", []) if at - t < 86400]
        if len(recent) >= DAILY_CAP:
            return self._note(pane, rec, at, "skip", f"daily cap of {DAILY_CAP} nudges")
        live = mode() == "live"
        verdict = None
        if judge.enabled():
            cached = rec.get("jev") or {}
            if cached.get("hash") == scr.body_hash:
                verdict = cached.get("answers")
            else:
                try:
                    verdict = judge.ask(scr.tail)
                except Exception as exc:  # noqa: BLE001
                    return self._note(pane, rec, at, "skip", f"judge failed: {type(exc).__name__}")
                rec["jev"] = {"hash": scr.body_hash, "answers": verdict}
            if verdict["state"] != "idle":
                return self._note(pane, rec, at, "skip", f"judge says {verdict['state']}")
            if verdict["needs_human_p"] >= 0.5 and not mail:
                return self._note(pane, rec, at, "skip", "judge says the last message asks a person")
        elif live:
            return self._note(pane, rec, at, "skip", "live mode needs TYPESAFE_API_KEY for the judge")
        pointed = bool(verdict and verdict["waiting_p"] >= 0.5 and not scr.watchers)
        minutes = int(idle // 60)
        if mail:
            age = int((at - (seat.oldest_unread_at or at)) // 60)
            text = MAIL.format(unread=seat.unread, plural="" if seat.unread == 1 else "s", age=age, minutes=minutes)
            kind = "mail"
        else:
            text = (POINTED if pointed else GENERIC).format(minutes=minutes)
            kind = "pointed" if pointed else "generic"
            if seat and seat.open_tasks:
                text += " Your open orchestra tasks: " + ", ".join(f"{t} ({st})" for t, st in seat.open_tasks[:5]) + "."
        row = {"ts": at, "event": "nudge" if live else "would_nudge", "pane": pane.id,
               "session": pane.session, "agent": pane.agent, "path": pane.path,
               "idle_seconds": round(idle), "streak": streak, "watchers": scr.watchers,
               "kind": kind, "jev": verdict, "tail": scr.tail[-1500:],
               "orchestra": {"member": seat.name, "unread": seat.unread, "open_tasks": len(seat.open_tasks)} if seat else None}
        if live:
            try:
                system.send(pane.id, text)
            except Exception as exc:  # noqa: BLE001
                row.update(event="skip", reason=f"send failed: {type(exc).__name__}")
                append_log(row)
                return row
        # A dry run types nothing, so nothing after it can be its answer.
        rec.update(last_nudge_at=at, last_nudge_hash=scr.body_hash, awaiting=live, streak=streak,
                   outcome_logged=False, nudges=recent + [at])
        append_log(row)
        return row

    def _note(self, pane, rec, at, event, reason) -> dict[str, Any] | None:
        """Log a skip once per reason per stop, not on every tick."""
        if rec.get("reason") == reason:
            return None
        rec["reason"] = reason
        row = {"ts": at, "event": event, "reason": reason, "pane": pane.id, "agent": pane.agent,
               "path": pane.path}
        append_log(row)
        return row

    def run(self, interval: float = POLL_SECONDS) -> None:
        lock = open(home() / "daemon.lock", "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("agent-nudge is already running on this machine")
        lock.write(str(os.getpid()))
        lock.flush()
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - the daemon outlives one bad pass
                append_log({"ts": self.now(), "event": "error", "reason": f"{type(exc).__name__}: {exc}"[:300]})
            self.sleep(interval)


def daemon_pid() -> int | None:
    path = home() / "daemon.lock"
    try:
        handle = open(path)
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                return int(handle.read().strip() or 0) or None
            except ValueError:
                return None
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None
