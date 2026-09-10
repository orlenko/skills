from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest import mock  # noqa: E402

from agent_orchestra import __version__  # noqa: E402
from agent_orchestra.cli import build_parser, run  # noqa: E402


DOCUMENTED = [
    ["hub", "start"],
    ["hub", "start", "--name", "box", "--bind", "127.0.0.1", "--port", "0",
     "--advertise", "10.0.0.2", "--advertise", "10.0.0.3", "--invite-ttl", "600", "--json"],
    ["hub", "ensure"],
    ["hub", "ensure", "--orchestra-id", "orc_abc", "--json"],
    ["hub", "status"],
    ["hub", "list"],
    ["hub", "unit"],
    ["hub", "invite"],
    ["hub", "invite", "--role", "conductor", "--parent", "mb_1234", "--name", "alice", "--ttl", "900"],
    ["hub", "conductor", "mb_1234"],
    ["hub", "kick", "mb_1234"],
    ["hub", "kick", "mb_1234", "--reason", "gone"],
    ["hub", "close"],
    ["join", "or1.abc"],
    ["join", "or1.abc", "--name", "alice", "--no-monitor"],
    ["invite"],
    ["invite", "--role", "player", "--parent", "self", "--name", "child", "--ttl", "60"],
    ["send", "--to", "conductor", "--stdin"],
    ["send", "--to", "conductor", "--to", "mb_1234", "hello", "there"],
    ["inbox"],
    ["inbox", "--claim"],
    ["wait"],
    ["wait", "--timeout", "55", "--claim"],
    ["finish", "m_0011223344556677"],
    ["finish", "m_0011223344556677", "m_8899aabbccddeeff"],
    ["status"],
    ["members"],
    ["tasks"],
    ["events"],
    ["events", "--limit", "20"],
    ["message", "m_0011223344556677"],
    ["conductor", "mb_1234"],
    ["kick", "mb_1234", "--reason", "idle"],
    ["leave"],
    ["close"],
    ["monitor"],
    ["serve", "--orchestra-id", "orc_abc"],
    ["monitor-run", "--member-id", "mb_1234"],
    ["hook-context", "--provider", "codex"],
    ["hook-stop", "--provider", "claude"],
    ["hook-wait", "--provider", "claude"],
]


class ParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = build_parser()

    def test_documented_command_lines_parse(self) -> None:
        for argv in DOCUMENTED:
            with self.subTest(argv=argv):
                args = self.parser.parse_args(argv)
                self.assertEqual(args.command, argv[0])

    def test_member_common_flags(self) -> None:
        args = self.parser.parse_args(
            ["status", "--provider", "claude", "--cwd", "/tmp", "--member-id", "mb_1234", "--json"]
        )
        self.assertEqual(args.provider, "claude")
        self.assertEqual(args.cwd, "/tmp")
        self.assertEqual(args.member_id, "mb_1234")
        self.assertTrue(args.as_json)

    def test_provider_default_comes_from_environment(self) -> None:
        previous = os.environ.get("AGENT_ORCHESTRA_PROVIDER")
        os.environ["AGENT_ORCHESTRA_PROVIDER"] = "codex"
        try:
            args = build_parser().parse_args(["status"])
        finally:
            if previous is None:
                os.environ.pop("AGENT_ORCHESTRA_PROVIDER", None)
            else:
                os.environ["AGENT_ORCHESTRA_PROVIDER"] = previous
        self.assertEqual(args.provider, "codex")
        self.assertEqual(build_parser().parse_args(["status"]).provider, "cli")

    def test_hub_subcommand_group(self) -> None:
        args = self.parser.parse_args(["hub", "invite", "--role", "conductor"])
        self.assertEqual(args.command, "hub")
        self.assertEqual(args.hub_command, "invite")
        self.assertEqual(args.role, "conductor")

    def test_version_prints_project_version(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stdout(out):
            self.parser.parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"agent-orchestra {__version__}")
        self.assertEqual(__version__, "0.1.4")

    def test_unknown_command_exits_non_zero(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            self.parser.parse_args(["conduct"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_missing_command_exits_non_zero(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            self.parser.parse_args([])
        self.assertNotEqual(caught.exception.code, 0)

    def test_unknown_hub_subcommand_exits_non_zero(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            self.parser.parse_args(["hub", "restart"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_hook_provider_is_restricted(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, redirect_stderr(err):
            self.parser.parse_args(["hook-stop", "--provider", "cli"])
        self.assertNotEqual(caught.exception.code, 0)


class HookFailureTest(unittest.TestCase):
    """A hook that blows up says nothing. It never fails the turn it runs in."""

    def _run(self, command: str, error: Exception) -> tuple[int, str]:
        args = build_parser().parse_args([command, "--provider", "claude"])
        target = "agent_orchestra.hooks." + command.replace("-", "_")
        out = io.StringIO()
        with mock.patch(target, side_effect=error), mock.patch(
            "agent_orchestra.hooks.hook_input", return_value={}
        ), redirect_stdout(out):
            return run(args), out.getvalue().strip()

    def test_hook_stop_still_answers_an_empty_decision(self) -> None:
        # The nono sandbox denies stat on the state directory, and pathlib
        # turns that into FileExistsError however the directory really looks.
        code, printed = self._run("hook-stop", FileExistsError(17, "File exists"))
        self.assertEqual(code, 0)
        self.assertEqual(printed, "{}")

    def test_hook_context_prints_nothing(self) -> None:
        code, printed = self._run("hook-context", OSError(13, "Permission denied"))
        self.assertEqual(code, 0)
        self.assertEqual(printed, "")

    def test_hook_wait_returns_zero(self) -> None:
        code, printed = self._run("hook-wait", RuntimeError("boom"))
        self.assertEqual(code, 0)
        self.assertEqual(printed, "")


if __name__ == "__main__":
    unittest.main()
