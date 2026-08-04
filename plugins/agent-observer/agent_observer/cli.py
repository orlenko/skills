from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .reviews import prepare_review, submit_review
from .runtime import (
    issue_bootstrap_token,
    run_daemon,
    service_status,
    start_services,
    stop_services,
)
from .service import Observer, ObserverConfig
from .web import run_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-observer",
        description="Passively observe local Claude and Codex session traces",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("--state-dir")
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

    serve = commands.add_parser(
        "serve", help="Run the localhost dashboard in the foreground"
    )
    serve.add_argument("--port", type=int, required=True)
    daemon = commands.add_parser(
        "daemon", help="Run collection and discovery in the foreground"
    )
    daemon.add_argument("--interval", type=float, default=2.0)
    daemon.add_argument("--rescan-interval", type=float, default=300.0)
    return parser


def _config(args: argparse.Namespace) -> ObserverConfig:
    defaults = ObserverConfig.defaults(args.state_dir)
    return ObserverConfig(
        defaults.state_dir,
        tuple(Path(value).expanduser() for value in args.claude_root)
        if args.claude_root
        else defaults.claude_roots,
        tuple(Path(value).expanduser() for value in args.codex_root)
        if args.codex_root
        else defaults.codex_roots,
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
            result = submit_review(observer, args.job_id, args.draft_path)
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
