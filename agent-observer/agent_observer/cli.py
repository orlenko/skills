from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .service import Observer, ObserverConfig


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
    with Observer(_config(args)) as observer:
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
        elif args.command == "run":
            if args.interval < 0.25:
                raise ValueError("--interval must be at least 0.25 seconds")
            try:
                while True:
                    observer.scan()
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
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
