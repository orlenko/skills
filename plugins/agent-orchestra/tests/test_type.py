"""ACT type: the header, the tmux side, and the monitor's own checks."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import keys, member as member_module  # noqa: E402
from agent_orchestra.core import atomic_write_json, member_path  # noqa: E402
from agent_orchestra.protocol import (  # noqa: E402
    ProtocolError,
    TypeOptions,
    message_body,
    parse_message,
)

DIM, RESET = "\x1b[2m", "\x1b[0m"


class TypeProtocolTests(unittest.TestCase):
    def test_options_round_trip_through_the_header(self):
        options = TypeOptions(submit=False, anytime=True, quiet=60)
        envelope = parse_message(f"ACT type\nTO mb_abcd1234\nTYPE {options.header()}\n\n/qc 1")
        self.assertEqual(envelope.typing, options)
        self.assertEqual(message_body(envelope.text), "/qc 1")

    def test_defaults_submit_when_idle_and_stay_quiet(self):
        envelope = parse_message("ACT type\nTO mb_abcd1234\n\n/aprs 3711")
        self.assertEqual(envelope.typing, TypeOptions())
        self.assertTrue(envelope.typing.submit)
        self.assertFalse(envelope.typing.anytime)
        self.assertGreater(envelope.typing.quiet, 0)

    def test_the_body_keeps_its_lines(self):
        text = "ACT type\nTO mb_abcd1234\n\nfirst line\n\nthird line"
        self.assertEqual(message_body(parse_message(text).text), "first line\n\nthird line")

    def test_one_member_id_only(self):
        for to in ("all", "conductor", "mb_abcd1234,mb_efgh5678"):
            with self.subTest(to=to), self.assertRaisesRegex(ProtocolError, "exactly one member"):
                parse_message(f"ACT type\nTO {to}\n\n/qc 1")

    def test_malformed_type_messages(self):
        cases = {
            "ACT type\nTO mb_abcd1234\n\n": "needs a body",
            "ACT type\nTO mb_abcd1234\nTYPE loud\n\nx": "Unknown TYPE token",
            "ACT type\nTO mb_abcd1234\nTYPE quiet=soon\n\nx": "whole seconds",
            "ACT type\nTO mb_abcd1234\nTYPE quiet=999999\n\nx": "quiet must be",
            "ACT tell\nTO all\nTYPE enter\n\nx": "goes on ACT type",
        }
        for text, error in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(ProtocolError, error):
                parse_message(text)


def claude_screen(prompt_line: str, footer: str = "  ? for shortcuts") -> str:
    return "\n".join(["⏺ Done.", "", "─" * 40, prompt_line, "─" * 40, footer])


class ReadPromptTests(unittest.TestCase):
    def test_an_empty_box_is_idle(self):
        self.assertTrue(keys.read_prompt(claude_screen("❯ "), "claude").idle)

    def test_dim_suggestion_text_is_not_typed(self):
        screen = claude_screen(f"❯ {DIM}check messages{RESET}")
        self.assertTrue(keys.read_prompt(screen, "claude").idle)

    def test_typed_text_is_not_idle(self):
        prompt = keys.read_prompt(claude_screen("❯ half a thought"), "claude")
        self.assertEqual(prompt.typed, "half a thought")
        self.assertFalse(prompt.idle)

    def test_a_running_turn_is_not_idle(self):
        screen = "\n".join(["✳ Nucleating… (12s)", "─" * 40, "❯ ", "─" * 40,
                            "  esc to interrupt"])
        self.assertTrue(keys.read_prompt(screen, "claude").working)

    def test_no_prompt_glyph_means_no_input_box(self):
        self.assertFalse(keys.read_prompt("$ vim notes.txt", "claude").shown)

    def test_codex_draws_its_own_glyph(self):
        self.assertTrue(keys.read_prompt("output\n› \n  footer", "codex").idle)


class FakeTmux:
    def __init__(self, screens: list[str]):
        self.calls: list[list[str]] = []
        self.screens = screens

    def __call__(self, args: list[str]) -> str:
        self.calls.append(args)
        if args[:2] == ["tmux", "list-panes"]:
            return "%3|:orchestra:|100|:orchestra:|aiq-ops|:orchestra:|0\n"
        if args[0] == "ps":
            return "100 1\n200 100\n300 200\n"
        if args[:2] == ["tmux", "capture-pane"]:
            return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return ""


class TmuxTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("sleep", lambda _: None),):
            patcher = mock.patch.object(keys, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def use(self, fake: FakeTmux) -> FakeTmux:
        patcher = mock.patch.object(keys, "run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def test_the_pane_is_found_through_the_agent_ancestors(self):
        self.use(FakeTmux([""]))
        pane = keys.find_pane(300)
        self.assertEqual((pane.id, pane.session), ("%3", "aiq-ops"))
        self.assertIsNone(keys.find_pane(999))

    def test_one_line_goes_in_as_literal_keys_then_enter(self):
        fake = self.use(FakeTmux([claude_screen("❯ ")]))
        self.assertTrue(keys.type_text("%3", "claude", "/qc 3696", submit=True))
        sent = [call for call in fake.calls if call[:2] == ["tmux", "send-keys"]]
        self.assertEqual(sent[0], ["tmux", "send-keys", "-t", "%3", "-l", "--", "/qc 3696"])
        self.assertEqual(sent[1][-1], "Enter")

    def test_several_lines_go_in_as_a_bracketed_paste(self):
        fake = self.use(FakeTmux([claude_screen("❯ ")]))
        keys.type_text("%3", "claude", "line one\nline two", submit=True)
        paste = next(call for call in fake.calls if call[:2] == ["tmux", "paste-buffer"])
        self.assertIn("-p", paste)
        self.assertFalse(any(call[:2] == ["tmux", "send-keys"] and "-l" in call
                             for call in fake.calls))

    def test_text_still_in_the_box_after_two_enters_is_not_submitted(self):
        fake = self.use(FakeTmux([claude_screen("❯ /qc 3696")]))
        self.assertFalse(keys.type_text("%3", "claude", "/qc 3696", submit=True))
        enters = [call for call in fake.calls if call[-1] == "Enter"]
        self.assertEqual(len(enters), 2)

    def test_no_enter_types_and_stops(self):
        fake = self.use(FakeTmux([claude_screen("❯ ")]))
        self.assertTrue(keys.type_text("%3", "claude", "draft", submit=False))
        self.assertFalse(any(call[-1] == "Enter" for call in fake.calls))


class MonitorCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-type-")
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.member = {"member_id": "mb_player0001", "name": "player", "provider": "claude",
                       "conductor_id": "mb_conduct001", "owner_pid": os.getpid()}
        atomic_write_json(member_path("mb_player0001"), self.member)
        self.type_text = mock.Mock(return_value=True)
        for name, value in (
            ("find_pane", mock.Mock(return_value=keys.Pane(id="%1", pid=1, session="s"))),
            ("prompt", mock.Mock(return_value=keys.Prompt(shown=True, typed="", working=False))),
            ("type_text", self.type_text),
        ):
            patcher = mock.patch.object(keys, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def envelope(self, sender: str, message_id: str = "m_" + "a" * 16) -> dict:
        return {"id": message_id, "from": {"id": sender, "name": "x"}, "act": "type",
                "text": "ACT type\nTO mb_player0001\n\n/qc 3696", "task": None}

    def outbox(self) -> list[dict]:
        folder = member_module.bucket_dir("mb_player0001", "outbox")
        return [json.loads(path.read_text()) for path in folder.glob("*.json")]

    def test_a_sender_who_is_not_the_conductor_is_refused_here_too(self):
        result = member_module._handle_type(self.member, self.envelope("mb_someone01"))
        self.assertEqual(result, "not-allowed")
        self.type_text.assert_not_called()
        reply, = self.outbox()
        self.assertEqual(reply["to"], ["mb_someone01"])
        self.assertIn("type not-allowed", reply["text"])

    def test_a_redelivered_message_is_not_typed_twice(self):
        self.assertEqual(member_module._handle_type(self.member, self.envelope("mb_conduct001")),
                         "typed")
        self.assertEqual(member_module._handle_type(self.member, self.envelope("mb_conduct001")),
                         "duplicate")
        self.assertEqual(self.type_text.call_count, 1)

    def test_quiet_is_set_before_typing_and_ends_on_state_started(self):
        seen = []
        self.type_text.side_effect = lambda *a, **k: seen.append(
            member_module.quiet_until("mb_player0001")) or True
        member_module._handle_type(self.member, self.envelope("mb_conduct001"))
        self.assertIsNotNone(seen[0])
        # Not tied to a task: any STATE started ends it.
        member_module.clear_quiet("mb_player0001", "T-any")
        self.assertIsNone(member_module.quiet_until("mb_player0001"))

    def test_a_quiet_tied_to_a_task_waits_for_that_task(self):
        envelope = self.envelope("mb_conduct001")
        envelope["task"] = "T-qc"
        member_module._handle_type(self.member, envelope)
        member_module.clear_quiet("mb_player0001", "T-other")
        self.assertIsNotNone(member_module.quiet_until("mb_player0001"))
        member_module.clear_quiet("mb_player0001", "T-qc")
        self.assertIsNone(member_module.quiet_until("mb_player0001"))

    def test_a_codex_seat_gets_no_queue_wake_while_quiet(self):
        codex = {**self.member, "provider": "codex"}
        member_module._handle_type(codex, self.envelope("mb_conduct001"))
        with mock.patch.object(member_module.codex_wake, "wake") as wake:
            member_module._wake_codex(codex)
        wake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
