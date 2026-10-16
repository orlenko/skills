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
    ["podium", "check", "plan.json"],
    ["podium", "publish", "plan.json", "--dir", "/tmp/podium", "--json"],
    ["podium", "serve"],
    ["podium", "serve", "--dir", "/tmp/podium", "--port", "4303"],
    ["podium", "serve", "--member-id", "mb_1234"],
    ["podium", "start"],
    ["podium", "start", "--member-id", "mb_1234", "--port", "4301", "--dir", "/tmp/podium", "--restart", "--json"],
    ["podium", "stop", "--member-id", "mb_1234"],
    ["podium", "status", "--json"],
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

    def test_provider_comes_from_flag_then_environment_then_agent(self) -> None:
        from agent_orchestra import cli

        self.assertEqual(
            build_parser().parse_args(["status", "--provider", "codex"]).provider, "codex"
        )
        self.assertIsNone(build_parser().parse_args(["status"]).provider)
        with mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_PROVIDER": "codex"}), \
                mock.patch("agent_orchestra.core.agent_provider", return_value="claude"):
            self.assertEqual(cli._provider_default(), "codex")
        with mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_PROVIDER": ""}):
            with mock.patch("agent_orchestra.core.agent_provider", return_value="claude"):
                self.assertEqual(cli._provider_default(), "claude")
            with mock.patch("agent_orchestra.core.agent_provider", return_value=None):
                self.assertEqual(cli._provider_default(), "cli")

    def test_mail_filters_select_by_sender_act_age_and_count(self) -> None:
        from agent_orchestra import cli

        now = cli.datetime.now(cli.timezone.utc).timestamp()

        def row(message_id: str, name: str, act: str, age: float) -> dict:
            return {"id": message_id, "from": {"name": name, "id": "mb_" + name},
                    "act": act, "sent_at": now - age}

        rows = [row("m_1", "oboe", "ask", 7200), row("m_2", "oboe", "tell", 600),
                row("m_3", "triangle", "ask", 300), row("m_4", "oboe", "ask", 60)]
        parse = build_parser().parse_args
        self.assertIsNone(cli._mail_select(parse(["inbox"])))
        pick = cli._mail_select(parse(["inbox", "--from", "OBOE", "--act", "ask"]))
        self.assertEqual([r["id"] for r in pick(rows)], ["m_1", "m_4"])
        pick = cli._mail_select(parse(["inbox", "--from", "mb_triangle", "--from", "oboe",
                                       "--since", "1h"]))
        self.assertEqual([r["id"] for r in pick(rows)], ["m_2", "m_3", "m_4"])
        pick = cli._mail_select(parse(["wait", "--limit", "2"]))
        self.assertEqual([r["id"] for r in pick(list(reversed(rows)))], ["m_4", "m_3"])
        self.assertEqual(cli._parse_age("90s"), 90)
        self.assertEqual(cli._parse_age("2h"), 7200)
        with self.assertRaises(cli.OrchestraError):
            cli._parse_age("soon")

    def test_brief_prints_one_line_per_message_without_the_header(self) -> None:
        from agent_orchestra import cli

        out = io.StringIO()
        with redirect_stdout(out):
            cli._print_messages([{
                "id": "m_aaaaaaaaaaaaaaaa", "from": {"name": "oboe"}, "act": "ask",
                "task": "t_x", "need": "sha", "sent_at": 0, "local_state": "pending",
                "text": "ACT ask\nTO conductor\nTASK t_x\n\nfirst line\nsecond " + "y" * 300,
            }], False, brief=True)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("m_aaaaaaaaaaaaaaaa  oboe  ask  task=t_x  need=sha  (pending)  | first line second", lines[0])
        self.assertNotIn("TO conductor", lines[0])
        self.assertLess(len(lines[0]), 200)

    def test_tasks_narrow_to_ids_and_wait_until_one_moves(self) -> None:
        from agent_orchestra import cli

        def view(state: str) -> dict:
            return {
                "tasks": [
                    {"task": "t_alpha", "state": state, "owners": [{"id": "mb_1", "state": state}]},
                    {"task": "t_beta", "state": "done", "owners": []},
                ],
                "attention": [{"task": "t_beta", "kind": "stale"}],
            }

        answers = iter([view("pending"), view("pending"), view("started")])
        api = mock.Mock()
        api.tasks.side_effect = lambda *a, **k: next(answers)
        args = build_parser().parse_args(["tasks", "--match", "^t_al", "--wait", "30"])
        with mock.patch.object(cli, "_TASK_WAIT_POLL_SECONDS", 0.01):
            result = cli._tasks_view(api, {}, args)
        self.assertEqual([row["task"] for row in result["tasks"]], ["t_alpha"])
        self.assertEqual(result["tasks"][0]["state"], "started")
        self.assertEqual(result["attention"], [])
        self.assertTrue(result["changed"])

        api.tasks.side_effect = lambda *a, **k: view("pending")
        args = build_parser().parse_args(["tasks", "--task", "t_beta", "--wait", "0.05"])
        with mock.patch.object(cli, "_TASK_WAIT_POLL_SECONDS", 0.01):
            result = cli._tasks_view(api, {}, args)
        self.assertEqual([row["task"] for row in result["tasks"]], ["t_beta"])
        self.assertEqual(len(result["attention"]), 1)
        self.assertFalse(result["changed"])

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
        self.assertEqual(__version__, "0.2.24")

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


class ClaimReminderTest(unittest.TestCase):
    def test_claiming_prints_the_finish_command(self) -> None:
        from agent_orchestra.cli import _print_finish_reminder

        out = io.StringIO()
        with redirect_stdout(out):
            _print_finish_reminder(
                {"member_id": "mb_aaaa1111"},
                "claude",
                [{"id": "m_" + "a" * 16}, {"id": "m_" + "b" * 16}],
            )
        printed = out.getvalue()
        self.assertIn("Claimed, not handled.", printed)
        self.assertIn("finish --json --provider claude --member-id mb_aaaa1111", printed)
        self.assertIn("m_" + "a" * 16, printed)
        self.assertIn("m_" + "b" * 16, printed)

    def test_nothing_claimed_says_nothing(self) -> None:
        from agent_orchestra.cli import _print_finish_reminder

        out = io.StringIO()
        with redirect_stdout(out):
            _print_finish_reminder({"member_id": "mb_aaaa1111"}, "claude", [])
        self.assertEqual(out.getvalue(), "")


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


class TasksFlagsTest(unittest.TestCase):
    def test_tasks_takes_both_attention_clocks(self):
        args = build_parser().parse_args(
            ["tasks", "--response-within", "60", "--stale-after", "7200", "--json"]
        )
        self.assertEqual((args.response_within, args.stale_after), (60.0, 7200.0))
        defaults = build_parser().parse_args(["tasks"])
        self.assertIsNone(defaults.response_within)
        self.assertIsNone(defaults.stale_after)


if __name__ == "__main__":
    unittest.main()
