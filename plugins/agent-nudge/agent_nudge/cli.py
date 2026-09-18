"""agent-nudge: wake Claude and Codex sessions in tmux that stopped short of their goal."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__, daemon, judge, orchestra, screen, system

LABEL = "agent-nudge"
BIN = Path(__file__).resolve().parent.parent / "bin" / "agent-nudge"


def _print(value, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")
    else:
        print(value)


def cmd_panes(args) -> None:
    table = system.process_table()
    seats = orchestra.seats_by_pid()
    rows = []
    for pane in system.panes():
        system.find_agent(pane, table)
        if not pane.agent:
            continue
        try:
            scr = screen.parse(system.capture(pane.id), pane.agent)
        except Exception as exc:  # noqa: BLE001
            rows.append({"pane": pane.id, "agent": pane.agent, "error": str(exc)[:120]})
            continue
        rows.append({"pane": pane.id, "session": pane.session, "agent": pane.agent, "path": pane.path,
                     "input_box": scr.has_prompt, "typed": bool(scr.typed), "working": scr.working_marker,
                     "watchers": scr.watchers, "opted_out": pane.opt_out})
        seat = seats.get(pane.agent_pid)
        if seat:
            rows[-1]["orchestra"] = {"member": seat.name, "role": seat.role, "unread": seat.unread,
                                     "open_tasks": [t for t, _ in seat.open_tasks]}
    if args.json:
        _print(rows, True)
        return
    for row in rows:
        state = "working" if row.get("working") else ("no input box" if not row.get("input_box") else "at prompt")
        flags = [f for f, on in (("typed", row.get("typed")), ("opted out", row.get("opted_out"))) if on]
        watchers = f", {row['watchers']} watching" if row.get("watchers") else ""
        seat = row.get("orchestra")
        member = (f"  {seat['member']}: {seat['unread']} unread, {len(seat['open_tasks'])} open"
                  if seat else "")
        print(f"{row['pane']:>5} {row['agent']:<7} {state}{watchers}{' [' + ', '.join(flags) + ']' if flags else ''}  {row.get('path', '')}{member}")


def _recent_log(hours: float) -> list[dict]:
    path = daemon.log_path()
    if not path.exists():
        return []
    since = time.time() - hours * 3600
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("ts", 0) >= since:
            rows.append(row)
    return rows


def cmd_status(args) -> None:
    daemon.load_env_file()  # report the judge as the service sees it
    rows = _recent_log(24)
    outcomes = [r for r in rows if r.get("event") == "outcome"]
    value = {
        "version": __version__,
        "mode": daemon.mode(),
        "daemon_pid": daemon.daemon_pid(),
        "judge": "on" if judge.enabled() else "off (no TYPESAFE_API_KEY)",
        "last_24h": dict(Counter(r.get("event") for r in rows)),
        "resumed_work_after_nudge": sum(1 for r in outcomes if r.get("ran_seconds", 0) >= 120),
        "replied_only": sum(1 for r in outcomes if r.get("ran_seconds", 0) < 120),
        "state_dir": str(daemon.home()),
        "env_file": str(daemon.env_file()),
    }
    _print(value, args.json)


def cmd_log(args) -> None:
    rows = _recent_log(args.hours)[-args.n:]
    if args.json:
        _print(rows, True)
        return
    for row in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(row.get("ts", 0)))
        detail = row.get("reason") or row.get("kind") or (f"ran {row.get('ran_seconds')} s" if row.get("event") == "outcome" else "")
        print(f"{stamp} {row.get('event', ''):<11} {row.get('pane', ''):>5} {row.get('agent', ''):<6} {detail}  {row.get('path', '')}")


def cmd_mode(args) -> None:
    if args.value:
        daemon.set_mode(args.value)
    print(daemon.mode())


def cmd_run(args) -> None:
    daemon.load_env_file()
    nudger = daemon.Nudger()
    if args.once:
        for row in nudger.tick():
            print(json.dumps({k: v for k, v in row.items() if k != "tail"}, ensure_ascii=False))
        return
    nudger.run(args.interval)


def _unit_path() -> Path:
    if platform.system() == "Darwin":
        return Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "systemd" / "user" / f"{LABEL}.service"


def _unit_text() -> str:
    path_dirs = [str(Path(sys.executable).parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    tmux = shutil.which("tmux")
    if tmux:
        path_dirs.insert(0, str(Path(tmux).parent))
    path_value = ":".join(dict.fromkeys(path_dirs))
    log = daemon.home() / "daemon.out"
    if platform.system() == "Darwin":
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>{BIN}</string><string>run</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path_value}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    return f"""[Unit]
Description=agent-nudge: wake idle Claude and Codex sessions in tmux

[Service]
ExecStart={sys.executable} {BIN} run
Environment=PATH={path_value}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def cmd_install(args) -> None:
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_unit_text())
    if platform.system() == "Darwin":
        target = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{target}/{LABEL}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", target, str(path)], check=True)
    else:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{LABEL}.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", f"{LABEL}.service"], check=True)
    print(f"installed {path}; mode is {daemon.mode()}")


def cmd_uninstall(args) -> None:
    path = _unit_path()
    if platform.system() == "Darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{LABEL}.service"], capture_output=True)
    path.unlink(missing_ok=True)
    print(f"removed {path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agent-nudge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"agent-nudge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run the loop (what the service runs)")
    run.add_argument("--once", action="store_true", help="One pass, print what it logged")
    run.add_argument("--interval", type=float, default=daemon.POLL_SECONDS)
    for name, helptext in (("panes", "List agent panes and what their screens show"),
                           ("status", "Mode, daemon, and the last 24 h")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--json", action="store_true")
    log = sub.add_parser("log", help="Recent decisions")
    log.add_argument("-n", type=int, default=30)
    log.add_argument("--hours", type=float, default=24)
    log.add_argument("--json", action="store_true")
    mode = sub.add_parser("mode", help="Show or set dry-run / live")
    mode.add_argument("value", nargs="?", choices=daemon.MODES)
    sub.add_parser("install", help="Install and start the launchd / systemd user service")
    sub.add_parser("uninstall", help="Stop and remove the service")
    args = parser.parse_args(argv)
    {"run": cmd_run, "panes": cmd_panes, "status": cmd_status, "log": cmd_log, "mode": cmd_mode,
     "install": cmd_install, "uninstall": cmd_uninstall}[args.command](args)
