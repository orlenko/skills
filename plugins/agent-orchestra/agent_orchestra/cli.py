from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .core import OrchestraError, normalize_provider


def _provider_default() -> str:
    return os.environ.get("AGENT_ORCHESTRA_PROVIDER", "cli")


def _common(parser: argparse.ArgumentParser, *, member: bool = True) -> None:
    parser.add_argument("--provider", default=_provider_default())
    parser.add_argument("--cwd", default=os.getcwd())
    if member:
        parser.add_argument("--member-id")
    parser.add_argument("--json", action="store_true", dest="as_json")


def _hub_common(parser: argparse.ArgumentParser, *, orchestra: bool = True) -> None:
    if orchestra:
        parser.add_argument("--orchestra-id")
    parser.add_argument("--json", action="store_true", dest="as_json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-orchestra",
        description="Durable many-agent messaging with a hub, a conductor, and players",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # argparse renders `help=SUPPRESS` on a subparser as the literal
    # "==SUPPRESS==", so the internal commands are hidden by omitting `help`
    # and keeping them out of the choices metavar instead.
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{hub,join,invite,send,inbox,wait,finish,status,members,tasks,"
        "events,message,conductor,kick,leave,close,monitor}",
    )

    hub = commands.add_parser("hub", help="Run and administer the hub on the always-on machine")
    hub_commands = hub.add_subparsers(dest="hub_command", required=True)

    hub_start = hub_commands.add_parser("start", help="Create a hub and print the conductor invite")
    _hub_common(hub_start, orchestra=False)
    hub_start.add_argument("--name", default=socket.gethostname())
    hub_start.add_argument("--bind", default="0.0.0.0")
    hub_start.add_argument("--port", type=int, default=0)
    hub_start.add_argument("--advertise", action="append", default=[])
    hub_start.add_argument("--invite-ttl", type=int, default=3600)

    hub_ensure = hub_commands.add_parser("ensure", help="Restart the hub process if it died")
    _hub_common(hub_ensure)

    hub_status = hub_commands.add_parser("status", help="Show hub process and orchestra state")
    _hub_common(hub_status)

    hub_list = hub_commands.add_parser("list", help="List unclosed hubs on this machine")
    _hub_common(hub_list, orchestra=False)

    hub_unit_parser = hub_commands.add_parser("unit", help="Print a launchd or systemd unit")
    _hub_common(hub_unit_parser)

    hub_invite = hub_commands.add_parser("invite", help="Mint an invite with the admin token")
    _hub_common(hub_invite)
    hub_invite.add_argument("--role", default="player", choices=("player", "conductor"))
    hub_invite.add_argument("--parent", default=None, metavar="MEMBER_ID")
    hub_invite.add_argument("--name", default=None)
    hub_invite.add_argument("--ttl", type=int, default=3600)

    hub_conductor = hub_commands.add_parser("conductor", help="Move the conductor role")
    _hub_common(hub_conductor)
    hub_conductor.add_argument("target", metavar="MEMBER_ID")

    hub_kick = hub_commands.add_parser("kick", help="Revoke a membership")
    _hub_common(hub_kick)
    hub_kick.add_argument("target", metavar="MEMBER_ID")
    hub_kick.add_argument("--reason", default=None)

    hub_close = hub_commands.add_parser("close", help="Close the orchestra for every member")
    _hub_common(hub_close)

    join = commands.add_parser("join", help="Join an orchestra with an or1. invite")
    _common(join, member=False)
    join.add_argument("invite", nargs="?")
    join.add_argument("--name", default=socket.gethostname())
    join.add_argument("--no-monitor", action="store_true")

    invite = commands.add_parser("invite", help="Mint an invite for a new member")
    _common(invite)
    invite.add_argument("--role", default="player", choices=("player", "conductor"))
    invite.add_argument("--parent", default="self", metavar="self|MEMBER_ID")
    invite.add_argument("--name", default=None)
    invite.add_argument("--ttl", type=int, default=3600)

    send = commands.add_parser("send", help="Send a message to members or aliases")
    _common(send)
    send.add_argument("text", nargs="*")
    send.add_argument("--to", action="append", default=[], metavar="TOKEN")
    send.add_argument("--stdin", action="store_true")

    inbox = commands.add_parser("inbox", help="List or claim locally delivered messages")
    _common(inbox)
    inbox.add_argument("--claim", action="store_true")

    wait = commands.add_parser("wait", help="Wait for locally delivered messages")
    _common(wait)
    wait.add_argument("--timeout", type=float, default=55)
    wait.add_argument("--claim", action="store_true")

    finish = commands.add_parser("finish", help="Mark claimed messages handled")
    _common(finish)
    finish.add_argument("message_ids", nargs="+", metavar="MESSAGE_ID")

    status = commands.add_parser("status", help="Show membership, hub, monitor, and inbox state")
    _common(status)

    members = commands.add_parser("members", help="List every member and its presence")
    _common(members)

    tasks = commands.add_parser("tasks", help="List assigned tasks and their newest message")
    _common(tasks)

    events = commands.add_parser("events", help="List recent system events")
    _common(events)
    events.add_argument("--limit", type=int, default=20)

    message = commands.add_parser("message", help="Show delivery state of one sent message")
    _common(message)
    message.add_argument("message_id", metavar="MESSAGE_ID")

    conductor = commands.add_parser("conductor", help="Move the conductor role")
    _common(conductor)
    conductor.add_argument("target", metavar="MEMBER_ID")

    kick = commands.add_parser("kick", help="Revoke another membership")
    _common(kick)
    kick.add_argument("target", metavar="MEMBER_ID")
    kick.add_argument("--reason", default=None)

    leave = commands.add_parser("leave", help="Leave the orchestra")
    _common(leave)

    close = commands.add_parser("close", help="Close the orchestra for every member")
    _common(close)

    monitor = commands.add_parser("monitor", help="Ensure the inbox monitor is running")
    _common(monitor)

    internal_serve = commands.add_parser("serve")
    internal_serve.add_argument("--orchestra-id", required=True)

    internal_monitor = commands.add_parser("monitor-run")
    internal_monitor.add_argument("--member-id", required=True)

    for hook_name in ("hook-context", "hook-stop", "hook-wait"):
        hook = commands.add_parser(hook_name)
        hook.add_argument("--provider", required=True, choices=("codex", "claude"))

    return parser


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
        if not isinstance(sender, dict):
            sender = {}
        header = [
            f"[{row['id']}] from {sender.get('name', 'member')}",
            f"act={row.get('act') or 'tell'}",
            f"task={row.get('task') or 'none'}",
            f"need={row.get('need') or 'none'}",
            f"({row.get('local_state', 'pending')})",
        ]
        print(" ".join(header))
        print(str(row.get("text", "")))
        print()


def _print_finish_reminder(member: dict[str, Any], provider: str, rows: list[dict[str, Any]]) -> None:
    """Claiming moves mail out of the way; it does not answer it.

    Two players independently read "claimed" as done and left the senders
    waiting, and the hook kept re-surfacing the same messages because nothing
    had finished them.
    """
    if not rows:
        return
    executable = Path(__file__).resolve().parent.parent / "bin" / "agent-orchestra"
    command = " ".join(
        shlex.quote(part)
        for part in (
            str(executable),
            "finish",
            "--json",
            "--provider",
            normalize_provider(provider),
            "--member-id",
            str(member["member_id"]),
            *[str(row["id"]) for row in rows],
        )
    )
    print(f"Claimed, not handled. When each one is done, run:\n{command}")


def _hub_report(hub_api: Any, record: dict[str, Any]) -> dict[str, Any]:
    from .core import api_request, hub_dir, read_json

    orchestra_id = str(record["orchestra_id"])
    running = hub_api.hub_alive(orchestra_id)
    result: dict[str, Any] = {
        "orchestra_id": orchestra_id,
        "name": record.get("name"),
        "endpoints": record.get("endpoints"),
        "fingerprint": record.get("fingerprint"),
        "port": record.get("port"),
        "closed_at": record.get("closed_at"),
        "running": running,
        "hub_pid": None,
        "remote": None,
        "error": None,
    }
    try:
        result["hub_pid"] = int(read_json(hub_dir(orchestra_id) / "ready.json").get("pid", 0)) or None
    except (OrchestraError, TypeError, ValueError):
        pass
    if running:
        try:
            result["remote"] = api_request(
                hub_api.admin_connection(orchestra_id), "GET", "/v1/status"
            )
        except OrchestraError as exc:
            result["error"] = str(exc)
    return result


def _run_hub(args: argparse.Namespace) -> int:
    from . import hub as hub_api
    from .core import api_request

    if args.hub_command == "start":
        result = hub_api.create_hub(
            name=args.name,
            bind=args.bind,
            port=args.port,
            advertise=args.advertise or None,
            invite_ttl=args.invite_ttl,
        )
        if args.as_json:
            _print(result, True)
            return 0
        print("Agent Orchestra hub is running. This session is not a member of it.")
        print(f"Orchestra: {result['orchestra_id']}")
        for endpoint in result.get("endpoints") or []:
            print(f"Endpoint: {endpoint}")
        print(f"Fingerprint: {result['fingerprint']}")
        print(f"Hub PID: {result['hub_pid']}")
        print(f"Invite expires: {_human(result['invite_expires_at'])}")
        print("Conductor invite, for the conductor machine:")
        print(result["conductor_invite"])
        return 0

    if args.hub_command == "list":
        _print(hub_api.local_hubs(), args.as_json)
        return 0

    record = hub_api.select_hub(args.orchestra_id)
    orchestra_id = str(record["orchestra_id"])

    if args.hub_command == "unit":
        unit = hub_api.hub_unit(orchestra_id)
        if args.as_json:
            _print({"orchestra_id": orchestra_id, "unit": unit}, True)
        else:
            print(unit)
        return 0
    if args.hub_command == "ensure":
        _print({"orchestra_id": orchestra_id, "hub_pid": hub_api.ensure_hub(orchestra_id)}, args.as_json)
        return 0
    if args.hub_command == "status":
        _print(_hub_report(hub_api, record), args.as_json)
        return 0

    hub_api.ensure_hub(orchestra_id)
    connection = hub_api.admin_connection(orchestra_id)
    if args.hub_command == "invite":
        _print(
            api_request(
                connection,
                "POST",
                "/v1/invite",
                {"role": args.role, "parent": args.parent, "name": args.name, "ttl": args.ttl},
            ),
            args.as_json,
        )
        return 0
    if args.hub_command == "conductor":
        _print(api_request(connection, "POST", "/v1/conductor", {"member_id": args.target}), args.as_json)
        return 0
    if args.hub_command == "kick":
        _print(
            api_request(connection, "POST", "/v1/kick", {"member_id": args.target, "reason": args.reason}),
            args.as_json,
        )
        return 0
    if args.hub_command == "close":
        _print(api_request(connection, "POST", "/v1/close", {}), args.as_json)
        return 0
    raise OrchestraError(f"Unsupported hub command: {args.hub_command}")


def run(args: argparse.Namespace) -> int:
    if args.command == "serve":
        from .hub import serve

        serve(args.orchestra_id)
        return 0
    if args.command == "monitor-run":
        from .member import monitor_loop

        monitor_loop(args.member_id)
        return 0
    if args.command.startswith("hook-"):
        from .hooks import hook_context, hook_input, hook_stop, hook_wait

        payload = hook_input()
        try:
            if args.command == "hook-context":
                result = hook_context(args.provider, payload)
                if result:
                    print(json.dumps(result, separators=(",", ":")))
                return 0
            if args.command == "hook-stop":
                print(json.dumps(hook_stop(args.provider, payload), separators=(",", ":")))
                return 0
            return hook_wait(args.provider, payload)
        except Exception:  # noqa: BLE001 - a hook never fails the turn it runs in
            # Every hook is best-effort. A traceback here reaches the user as a
            # hook error on every turn of every session, which is worse than
            # the mail this one could not surface.
            if args.command == "hook-stop":
                print("{}")
            return 0
    if args.command == "hub":
        return _run_hub(args)

    from . import member as member_api

    if args.command == "join":
        invite = args.invite or sys.stdin.read().strip()
        if not invite:
            raise OrchestraError("Pass the or1. invite as an argument or on stdin")
        result = member_api.join(
            invite,
            provider=args.provider,
            cwd=args.cwd,
            name=args.name,
            start_background_monitor=not args.no_monitor,
        )
        _print(result, args.as_json)
        return 0

    member = member_api.select_member(
        provider=args.provider, cwd=args.cwd, member_id=args.member_id
    )

    if args.command == "invite":
        _print(
            member_api.invite(
                member, role=args.role, parent=args.parent, name=args.name, ttl=args.ttl
            ),
            args.as_json,
        )
        return 0
    if args.command == "send":
        text = sys.stdin.read() if args.stdin else " ".join(args.text)
        result = member_api.send(member, text, to=args.to or None)
        _print(result, args.as_json)
        if not args.as_json and result.get("state") == "closed":
            reason = result.get("reason") or "closed"
            print(
                f"This membership is closed ({reason}); the message was not sent "
                "and this session is no longer in the orchestra."
            )
        return 0
    if args.command == "inbox":
        rows = member_api.local_messages(member, claim=args.claim)
        _print_messages(rows, args.as_json)
        if args.claim and not args.as_json:
            _print_finish_reminder(member, args.provider, rows)
        return 0
    if args.command == "wait":
        rows = member_api.wait_for_messages(member, args.timeout, claim=args.claim)
        _print_messages(rows, args.as_json)
        if args.claim and not args.as_json:
            _print_finish_reminder(member, args.provider, rows)
        return 0
    if args.command == "finish":
        _print({"messages": member_api.finish_messages(member, args.message_ids)}, args.as_json)
        return 0
    if args.command == "status":
        _print(member_api.status(member), args.as_json)
        return 0
    if args.command == "members":
        _print(member_api.members(member), args.as_json)
        return 0
    if args.command == "tasks":
        _print(member_api.tasks(member), args.as_json)
        return 0
    if args.command == "events":
        _print({"events": member_api.recent_events(member, limit=args.limit)}, args.as_json)
        return 0
    if args.command == "message":
        _print(member_api.message_status(member, args.message_id), args.as_json)
        return 0
    if args.command == "conductor":
        _print(member_api.set_conductor(member, args.target), args.as_json)
        return 0
    if args.command == "kick":
        _print(member_api.kick(member, args.target, args.reason), args.as_json)
        return 0
    if args.command == "leave":
        _print(member_api.leave(member), args.as_json)
        return 0
    if args.command == "close":
        _print(member_api.close(member), args.as_json)
        return 0
    if args.command == "monitor":
        _print({"monitor_pid": member_api.start_monitor(member)}, args.as_json)
        return 0
    raise OrchestraError(f"Unsupported command: {args.command}")


def main() -> None:
    try:
        code = run(build_parser().parse_args())
    except (OrchestraError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("Interrupted.", file=sys.stderr)
        else:
            print(f"agent-orchestra: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)
