from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .analyzer import run_analyzer
from .ingest import run_ingest
from .remote import (
    DEFAULT_INVITE_TTL,
    accept_invite,
    claim_uploader,
    enable_home_remote,
    issue_invite,
    load_home_config,
    push_snapshot,
    remote_connection_status,
)
from .reviews import next_review, prepare_review, submit_review
from .runtime import (
    claim_active_instance,
    issue_bootstrap_token,
    release_active_instance,
    run_daemon,
    service_status,
    start_analyzer_service,
    start_remote_services,
    start_services,
    stop_services,
)
from .service import Observer, ObserverConfig
from .web import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-observer",
        description="Passively observe local and explicitly enrolled remote Claude and Codex session traces",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--state-dir")
    parser.add_argument(
        "--workspace",
        help="Root Observer state at WORKSPACE/.agent-observer",
    )
    parser.add_argument("--claude-root", action="append")
    parser.add_argument("--codex-root", action="append")
    parser.add_argument("--json", action="store_true", dest="as_json")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="Watch a project and run a bounded baseline")
    add.add_argument("path")

    remove = commands.add_parser("remove", help="Stop watching a project")
    remove.add_argument("project")

    rescan = commands.add_parser("rescan", help="Rediscover bounded provider sources")
    rescan.add_argument("project")

    commands.add_parser("scan", help="Read newly appended records once")
    commands.add_parser(
        "status", help="Show cached project, session, and finding state"
    )

    run = commands.add_parser("run", help="Continuously scan tracked sources")
    run.add_argument("--interval", type=float, default=2.0)

    commands.add_parser("up", help="Start the local sync daemon and dashboard")
    commands.add_parser("down", help="Stop the local sync daemon and dashboard")
    commands.add_parser("services", help="Show sidecar service health")

    start = commands.add_parser(
        "start", help="Watch a project, start services, and prepare an AI review"
    )
    start.add_argument("project", nargs="?", default=".")
    start.add_argument("--provider", choices=("claude", "codex"), required=True)
    start.add_argument("--model")
    start.add_argument("--analyzer-session-id")
    start.add_argument("--session-id", help="Review this worker session only")
    start.add_argument("--session-provider", choices=("claude", "codex"))
    start.add_argument("--rescan", action="store_true")

    review = commands.add_parser(
        "review-prepare", help="Prepare a bounded packet for this active AI session"
    )
    review.add_argument("project")
    review.add_argument("--provider", choices=("claude", "codex"), required=True)
    review.add_argument("--model")
    review.add_argument("--analyzer-session-id")
    review.add_argument("--session-id", help="Review this worker session only")
    review.add_argument("--session-provider", choices=("claude", "codex"))

    include = commands.add_parser(
        "include-session", help="Allow a previously excluded analyzer session"
    )
    include.add_argument("provider", choices=("claude", "codex"))
    include.add_argument("session_id")

    submit = commands.add_parser(
        "review-submit", help="Validate and publish an AI review draft"
    )
    submit.add_argument("job_id")
    submit.add_argument("draft_path")
    submit.add_argument("--lease-token")

    begin = commands.add_parser(
        "supervisor-begin",
        help="Ensure collector, dashboard, ingest, and dormant analyzer sidecars",
    )
    begin.add_argument("--provider", choices=("claude", "codex"), required=True)
    begin.add_argument("--model")
    begin.add_argument("--analyzer-session-id")
    begin.add_argument("--allow-cross-provider", action="store_true")

    next_command = commands.add_parser(
        "review-next", help="Wait for one deterministic supervised review packet"
    )
    next_command.add_argument("lease_token")
    next_command.add_argument("--wait", type=float, default=0)

    commands.add_parser(
        "supervisor-status", help="Show collector, dashboard, and analyzer health"
    )
    commands.add_parser(
        "supervisor-stop", help="Revoke the analyzer lease and stop sidecars"
    )

    remote_enable = commands.add_parser(
        "remote-enable", help="Enable the dedicated LAN/Tailscale ingest listener"
    )
    remote_enable.add_argument("--bind", default="0.0.0.0")
    remote_enable.add_argument("--port", type=int)
    remote_enable.add_argument("--advertise", action="append")
    remote_enable.add_argument("--ttl", type=int, default=DEFAULT_INVITE_TTL)

    remote_invite = commands.add_parser(
        "remote-invite", help="Issue a fresh single-use remote enrollment key"
    )
    remote_invite.add_argument("--ttl", type=int, default=DEFAULT_INVITE_TTL)

    commands.add_parser("remote-nodes", help="List enrolled remote Observer nodes")
    remote_revoke = commands.add_parser(
        "remote-revoke", help="Revoke one remote Observer node credential"
    )
    remote_revoke.add_argument("node_id")

    remote_begin = commands.add_parser(
        "remote-begin",
        help="Enroll or resume a remote collector and dormant analyzer",
    )
    remote_begin.add_argument("invite", nargs="?")
    remote_begin.add_argument("--provider", choices=("claude", "codex"), required=True)
    remote_begin.add_argument("--model")
    remote_begin.add_argument("--analyzer-session-id")
    remote_begin.add_argument("--display-name")
    remote_begin.add_argument("--allow-cross-provider", action="store_true")
    commands.add_parser("remote-status", help="Show this remote node connection state")
    commands.add_parser(
        "remote-stop", help="Detach the remote analyzer and stop its collector"
    )

    serve = commands.add_parser(
        "serve", help="Run the localhost dashboard in the foreground"
    )
    serve.add_argument("--port", type=int, required=True)
    daemon = commands.add_parser(
        "daemon", help="Run collection and discovery in the foreground"
    )
    daemon.add_argument("--interval", type=float, default=2.0)
    daemon.add_argument("--rescan-interval", type=float, default=300.0)
    ingest = commands.add_parser(
        "ingest", help="Run the dedicated remote snapshot ingress server"
    )
    ingest.add_argument("--bind", required=True)
    ingest.add_argument("--port", type=int, required=True)
    analyzer = commands.add_parser(
        "analyzer", help="Run the dormant subscription-backed analyzer sidecar"
    )
    analyzer.add_argument("--provider", choices=("claude", "codex"), required=True)
    analyzer.add_argument("--model")
    analyzer.add_argument("--allow-cross-provider", action="store_true")
    analyzer.add_argument("--poll", type=float, default=15.0)
    analyzer.add_argument("--interval", type=float, default=3600.0)
    return parser


def _config(args: argparse.Namespace) -> ObserverConfig:
    workspace_value = getattr(args, "workspace", None)
    if args.state_dir and workspace_value:
        raise ValueError("use either --state-dir or --workspace, not both")
    state_dir = args.state_dir
    if workspace_value:
        workspace = Path(workspace_value).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"observer workspace does not exist: {workspace}")
        state = workspace / ".agent-observer"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state, 0o700)
        ignore = state / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        state_dir = str(state)
    defaults = ObserverConfig.defaults(state_dir)
    return ObserverConfig(
        defaults.state_dir,
        tuple(Path(value).expanduser() for value in args.claude_root)
        if args.claude_root
        else defaults.claude_roots,
        tuple(Path(value).expanduser() for value in args.codex_root)
        if args.codex_root
        else defaults.codex_roots,
        defaults.codex_session_index,
    )


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and "projects" in value:
        projects = value["projects"]
        if not projects:
            print("No watched projects.")
            return
        for project in projects:
            path = _terminal_text(project["resolved_path"])
            branch = _terminal_text(project.get("current_branch") or "n/a")
            print(f"{path}  branch={branch}")
            for session in project["sessions"]:
                age = session.get("activity_age_seconds")
                age_text = f"{int(age)}s" if age is not None else "unknown"
                provider = _terminal_text(session["provider"])
                session_id = _terminal_text(session["session_id"])
                print(
                    f"  {provider}:{session_id[:8]}  "
                    f"activity={age_text}  last={session.get('last_kind') or 'unknown'}"
                )
                if session.get("last_message_excerpt"):
                    first = _terminal_text(
                        session["last_message_excerpt"]
                    ).splitlines()[0]
                    print(f"    {first[:120]}")
            for finding in project["findings"]:
                kind = _terminal_text(finding["kind"])
                summary = _terminal_text(finding["summary"])
                print(f"  ! {kind}: {summary[:120]}")
            for source in project["sources"]:
                if source["health"] != "healthy" or source.get("health_detail"):
                    provider = _terminal_text(source["provider"])
                    session_id = _terminal_text(source["session_id"])
                    health = _terminal_text(source["health"])
                    detail = _terminal_text(source.get("health_detail") or "")
                    print(f"  observer {provider}:{session_id[:8]} {health}: {detail}")
        return
    print(json.dumps(value, indent=2, sort_keys=True))


def _terminal_text(value: Any) -> str:
    text = str(value)
    return "".join(character if character.isprintable() else "�" for character in text)


def run(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.command == "serve":
        return run_server(config, port=args.port)
    if args.command == "daemon":
        return run_daemon(
            config,
            interval=args.interval,
            rescan_interval=args.rescan_interval,
        )
    if args.command == "ingest":
        return run_ingest(config, bind=args.bind, port=args.port)
    if args.command == "analyzer":
        return run_analyzer(
            config,
            provider=args.provider,
            model=args.model,
            allow_cross_provider=args.allow_cross_provider,
            poll_seconds=args.poll,
            interval_seconds=args.interval,
        )
    if args.command == "up":
        _print(start_services(config), args.as_json)
        return 0
    if args.command == "down":
        _print(stop_services(config), args.as_json)
        return 0
    if args.command == "services":
        result = service_status(config)
        if result.get("dashboard_url"):
            result["dashboard_url"] += f"?bootstrap={issue_bootstrap_token(config)}"
        _print(result, args.as_json)
        return 0
    if args.command == "supervisor-stop":
        with Observer(config) as observer:
            supervisor = observer.db.revoke_supervisor()
        _print(
            {
                "supervisor": supervisor,
                "services": stop_services(config),
                "state_dir": str(config.state_dir),
                "released_active_instance": release_active_instance(config),
            },
            args.as_json,
        )
        return 0
    if args.command == "remote-stop":
        with Observer(config) as observer:
            supervisor = observer.db.revoke_supervisor("remote Observer stopped")
        _print(
            {
                "supervisor": supervisor,
                "services": stop_services(config),
                "remote": remote_connection_status(config),
                "released_active_instance": release_active_instance(config),
            },
            args.as_json,
        )
        return 0
    if args.command == "remote-status":
        with Observer(config) as observer:
            supervisor = observer.db.supervisor_status()
        _print(
            {
                "state_dir": str(config.state_dir),
                "services": service_status(config),
                "supervisor": supervisor,
                "remote": remote_connection_status(config),
            },
            args.as_json,
        )
        return 0
    if args.command == "remote-enable":
        home = enable_home_remote(
            config,
            bind=args.bind,
            port=args.port,
            advertise=args.advertise,
        )
        services = start_services(config)
        with Observer(config) as observer:
            enrollment = issue_invite(observer, home, ttl_seconds=args.ttl)
        _print(
            {"state_dir": str(config.state_dir), "services": services, "remote_enrollment": enrollment},
            args.as_json,
        )
        return 0
    if args.command == "remote-invite":
        home = load_home_config(config)
        if home is None:
            raise ValueError("remote ingestion is not enabled; run remote-enable first")
        services = start_services(config)
        with Observer(config) as observer:
            enrollment = issue_invite(observer, home, ttl_seconds=args.ttl)
        _print(
            {"state_dir": str(config.state_dir), "services": services, "remote_enrollment": enrollment},
            args.as_json,
        )
        return 0
    if args.command == "remote-begin":
        analyzer_session_id = args.analyzer_session_id or os.environ.get(
            "CODEX_THREAD_ID" if args.provider == "codex" else "CLAUDE_SESSION_ID"
        )
        if args.invite:
            accept_invite(
                config,
                args.invite,
                provider=args.provider,
                display_name=args.display_name,
            )
        claim_uploader(config)
        instance = claim_active_instance(config)
        services = start_remote_services(config)
        with Observer(config) as observer:
            if analyzer_session_id:
                observer.db.exclude_session(
                    args.provider,
                    analyzer_session_id,
                    "invoking remote Observer controller session",
                )
        analyzer = start_analyzer_service(
            config,
            provider=args.provider,
            model=args.model,
            allow_cross_provider=args.allow_cross_provider,
        )
        with Observer(config) as observer:
            supervisor = observer.db.supervisor_status()
        services = service_status(config)
        synced = push_snapshot(config)
        _print(
            {
                "state_dir": str(config.state_dir),
                "services": services,
                "supervisor": supervisor,
                "analyzer": analyzer,
                "instance": instance,
                "remote": remote_connection_status(config),
                "sync": synced,
            },
            args.as_json,
        )
        return 0

    with Observer(config) as observer:
        if args.command == "add":
            result = observer.add_project(args.path)
        elif args.command == "remove":
            result = observer.remove_project(args.project)
        elif args.command == "rescan":
            result = {"sources_added": observer.rescan_project(args.project)}
        elif args.command == "scan":
            result = observer.scan()
        elif args.command == "status":
            result = observer.status()
        elif args.command == "supervisor-status":
            services = service_status(config)
            if services.get("dashboard_url"):
                services["dashboard_url"] += (
                    f"?bootstrap={issue_bootstrap_token(config)}"
                )
            result = {
                "state_dir": str(config.state_dir),
                "services": services,
                "supervisor": observer.db.supervisor_status(),
            }
        elif args.command == "supervisor-begin":
            analyzer_session_id = args.analyzer_session_id or os.environ.get(
                "CODEX_THREAD_ID"
                if args.provider == "codex"
                else "CLAUDE_SESSION_ID"
            )
            if analyzer_session_id:
                observer.db.exclude_session(
                    args.provider,
                    analyzer_session_id,
                    "invoking Observer controller session",
                )
            instance = claim_active_instance(config)
            home = load_home_config(config) or enable_home_remote(config)
            services = start_services(config)
            analyzer = start_analyzer_service(
                config,
                provider=args.provider,
                model=args.model,
                allow_cross_provider=args.allow_cross_provider,
            )
            supervisor = observer.db.supervisor_status()
            services = service_status(config)
            result = {
                "state_dir": str(config.state_dir),
                "services": services,
                "supervisor": supervisor,
                "analyzer": analyzer,
                "instance": instance,
                "remote_enrollment": issue_invite(observer, home),
            }
        elif args.command == "remote-nodes":
            result = {"nodes": observer.db.remote_nodes()}
        elif args.command == "remote-revoke":
            result = observer.db.revoke_remote_node(args.node_id)
        elif args.command == "review-next":
            result = next_review(
                observer, args.lease_token, wait_seconds=args.wait
            )
        elif args.command == "include-session":
            result = {
                "included": observer.db.include_session(args.provider, args.session_id),
                "provider": args.provider,
                "session_id": args.session_id,
            }
        elif args.command == "run":
            if args.interval < 0.25:
                raise ValueError("--interval must be at least 0.25 seconds")
            try:
                while True:
                    observer.scan()
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
        elif args.command == "start":
            if args.session_provider and not args.session_id:
                raise ValueError("--session-provider requires --session-id")
            analyzer_session_id = args.analyzer_session_id or os.environ.get(
                "CODEX_THREAD_ID" if args.provider == "codex" else "CLAUDE_SESSION_ID"
            )
            if analyzer_session_id:
                observer.db.exclude_session(
                    args.provider,
                    analyzer_session_id,
                    "invoking interactive Observer analyzer",
                )
            try:
                project = observer._resolve_project(args.project)
                if args.rescan:
                    observer.rescan_project(str(project["project_id"]))
                else:
                    observer.scan()
            except KeyError:
                added = observer.add_project(args.project)
                project = added["project"]
            services = start_services(config)
            review = prepare_review(
                observer,
                str(project["project_id"]),
                analyzer_provider=args.provider,
                analyzer_model=args.model,
                exclude_session_id=analyzer_session_id,
                target_session_id=args.session_id,
                target_provider=args.session_provider,
            )
            result = {"services": services, "review": review}
        elif args.command == "review-prepare":
            if args.session_provider and not args.session_id:
                raise ValueError("--session-provider requires --session-id")
            analyzer_session_id = args.analyzer_session_id or os.environ.get(
                "CODEX_THREAD_ID" if args.provider == "codex" else "CLAUDE_SESSION_ID"
            )
            if analyzer_session_id:
                observer.db.exclude_session(
                    args.provider,
                    analyzer_session_id,
                    "invoking interactive Observer analyzer",
                )
            result = prepare_review(
                observer,
                args.project,
                analyzer_provider=args.provider,
                analyzer_model=args.model,
                exclude_session_id=analyzer_session_id,
                target_session_id=args.session_id,
                target_provider=args.session_provider,
            )
        elif args.command == "review-submit":
            result = submit_review(
                observer,
                args.job_id,
                args.draft_path,
                lease_token=args.lease_token,
            )
        else:
            raise AssertionError(args.command)
    _print(result, args.as_json)
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (KeyError, OSError, ValueError) as exc:
        print(f"agent-observer: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
