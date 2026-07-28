from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .client import (
    DEFAULT_TTL_SECONDS,
    accept_pair,
    close_pair,
    create_pair,
    finish_messages,
    hook_context,
    hook_input,
    hook_stop,
    hook_wait,
    local_messages,
    monitor_loop,
    pair_status,
    select_endpoint_by_id,
    send_message,
    start_monitor,
    wait_for_messages,
)
from .core import AgentPairError
from .server import serve


def _provider_default() -> str:
    return os.environ.get("AGENT_PAIR_PROVIDER", "cli")


def _common(parser: argparse.ArgumentParser, *, pair: bool = True) -> None:
    parser.add_argument("--provider", default=_provider_default())
    parser.add_argument("--cwd", default=os.getcwd())
    if pair:
        parser.add_argument("--pair-id")
        parser.add_argument("--endpoint-id")
    parser.add_argument("--json", action="store_true", dest="as_json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-pair", description="Direct two-agent mailbox")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    host = commands.add_parser("host", help="Create a pair and print a one-time invite")
    _common(host, pair=False)
    host.add_argument("--name", default=socket.gethostname())
    host.add_argument("--bind", default="0.0.0.0")
    host.add_argument("--port", type=int, default=0)
    host.add_argument("--advertise", action="append", default=[])
    host.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS, help="Pair lifetime in seconds")
    host.add_argument("--no-monitor", action="store_true", help=argparse.SUPPRESS)

    accept = commands.add_parser("accept", help="Accept a one-time invite")
    _common(accept, pair=False)
    accept.add_argument("invite", nargs="?")
    accept.add_argument("--name", default=socket.gethostname())
    accept.add_argument("--no-monitor", action="store_true", help=argparse.SUPPRESS)

    send = commands.add_parser("send", help="Queue a message for the peer")
    _common(send)
    send.add_argument("text", nargs="*")
    send.add_argument("--stdin", action="store_true")

    inbox = commands.add_parser("inbox", help="List or claim locally delivered messages")
    _common(inbox)
    inbox.add_argument("--claim", action="store_true")

    finish = commands.add_parser("finish", help="Mark claimed messages handled")
    _common(finish)
    finish.add_argument("message_ids", nargs="+")

    wait = commands.add_parser("wait", help="Wait for locally delivered messages")
    _common(wait)
    wait.add_argument("--timeout", type=float, default=55)
    wait.add_argument("--claim", action="store_true")

    status = commands.add_parser("status", help="Show pair, monitor, and delivery state")
    _common(status)

    close = commands.add_parser("close", help="Close the pair for both peers")
    _common(close)

    monitor = commands.add_parser("monitor", help="Ensure the inbox monitor is running")
    _common(monitor)

    internal_serve = commands.add_parser("serve", help=argparse.SUPPRESS)
    internal_serve.add_argument("--pair-id", required=True)

    internal_monitor = commands.add_parser("monitor-run", help=argparse.SUPPRESS)
    internal_monitor.add_argument("--endpoint-id", required=True)

    for hook_name in ("hook-context", "hook-stop", "hook-wait"):
        hook = commands.add_parser(hook_name, help=argparse.SUPPRESS)
        hook.add_argument("--provider", required=True, choices=("codex", "claude"))

    return parser


def _endpoint(args: argparse.Namespace) -> dict[str, Any]:
    return select_endpoint_by_id(
        provider=args.provider,
        cwd=args.cwd,
        pair_id=args.pair_id,
        endpoint_id=args.endpoint_id,
    )


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {_human(item)}")
    elif isinstance(value, list):
        for item in value:
            print(_human(item))
    else:
        print(value)


def _human(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, float) and value > 1_000_000_000:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


def _print_messages(rows: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        _print({"messages": rows, "count": len(rows)}, True)
        return
    if not rows:
        print("No waiting messages.")
        return
    for row in rows:
        sender = row.get("from") or {}
        print(f"[{row['id']}] from {sender.get('name', 'peer')} ({row.get('local_state', 'pending')})")
        print(str(row.get("text", "")))
        print()


def run(args: argparse.Namespace) -> int:
    if args.command == "serve":
        serve(args.pair_id)
        return 0
    if args.command == "monitor-run":
        monitor_loop(args.endpoint_id)
        return 0
    if args.command.startswith("hook-"):
        payload = hook_input()
        if args.command == "hook-context":
            result = hook_context(args.provider, payload)
            if result:
                print(json.dumps(result, separators=(",", ":")))
            return 0
        if args.command == "hook-stop":
            result = hook_stop(args.provider, payload)
            print(json.dumps(result, separators=(",", ":")))
            return 0
        return hook_wait(args.provider, payload)

    if args.command == "host":
        result = create_pair(
            provider=args.provider,
            cwd=args.cwd,
            name=args.name,
            bind=args.bind,
            port=args.port,
            advertise=args.advertise or None,
            ttl_seconds=args.ttl,
            start_background_monitor=not args.no_monitor,
        )
        if args.as_json:
            _print(result, True)
        else:
            print("Agent Pair is ready. Send this one-time invite to the other agent:")
            print(result["invite"])
            print(f"Pair: {result['pair_id']}")
            print(f"Expires: {_human(result['expires_at'])}")
            print(f"Monitor PID: {result['monitor_pid']}")
        return 0

    if args.command == "accept":
        invite = args.invite or sys.stdin.read().strip()
        if not invite:
            raise AgentPairError("Pass the ap1. invite as an argument or on stdin")
        result = accept_pair(
            invite,
            provider=args.provider,
            cwd=args.cwd,
            name=args.name,
            start_background_monitor=not args.no_monitor,
        )
        _print(result, args.as_json)
        return 0

    endpoint = _endpoint(args)
    if args.command == "send":
        text = sys.stdin.read() if args.stdin else " ".join(args.text)
        result = send_message(endpoint, text)
        _print(result, args.as_json)
        return 0
    if args.command == "inbox":
        _print_messages(local_messages(endpoint, claim=args.claim), args.as_json)
        return 0
    if args.command == "finish":
        _print({"messages": finish_messages(endpoint, args.message_ids)}, args.as_json)
        return 0
    if args.command == "wait":
        _print_messages(
            wait_for_messages(endpoint, args.timeout, claim=args.claim),
            args.as_json,
        )
        return 0
    if args.command == "status":
        _print(pair_status(endpoint), args.as_json)
        return 0
    if args.command == "close":
        _print(close_pair(endpoint), args.as_json)
        return 0
    if args.command == "monitor":
        _print({"monitor_pid": start_monitor(endpoint)}, args.as_json)
        return 0
    raise AgentPairError(f"Unsupported command: {args.command}")


def main() -> None:
    try:
        code = run(build_parser().parse_args())
    except (AgentPairError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Interrupted.", file=sys.stderr)
        else:
            print(f"agent-pair: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)
