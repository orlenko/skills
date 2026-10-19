"""Antigravity (`agy`): its processes, its hook contract, its screen, its wake."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import core, fasthook, hooks, keys, member as member_module  # noqa: E402
from agent_orchestra.core import atomic_write_json, bucket_dir, member_path  # noqa: E402

RULE = "─" * 82
# Captured from live agy 2026 panes (`tmux capture-pane -p -e`).
IDLE = "\n".join([
    "● Done.", "", RULE, ">", RULE,
    "? for shortcuts                                    Gemini 4 Argon · medium",
    "aiq agy/orlenko · 5h 1% ↺16:09 · wk 0%",
])
IDLE_WITH_TASK = "\n".join([
    "● Done.", "", RULE, ">", RULE,
    "  ● [11:30:15] /home/vlad/.../agent-orchestra wait --timeout 86400 --claim --json running",
    RULE,
    "? for shortcuts                    Gemini 4 Argon · medium · 1 task(s) · /tasks",
    "aiq agy/orlenko · 5h 1% ↺16:09 · wk 0%",
])
BUSY = "\n".join([
    "● Running (...) (ctrl+o to expand)", "⣻  Running command...", RULE, ">", RULE,
    "esc to cancel                                      Gemini 4 Argon · medium",
    "aiq agy/orlenko · 5h 0% ↺16:09 · wk 0%",
])
# What agy 2026-10-19 piped to a Stop hook, trimmed.
STOP = {"conversationId": "14d2e7cb-c49f-487f-bfa2-aa3da16b237f", "error": "",
        "executionNum": 0, "fullyIdle": True, "terminationReason": "NO_TOOL_CALL",
        "transcriptPath": "/home/vlad/.gemini/antigravity-cli/brain/x/transcript_full.jsonl",
        "workspacePaths": ["/srv/project"]}


class ProcessTests(unittest.TestCase):
    def test_provider_names(self):
        for name in ("agy", "Antigravity", "antigravity-cli"):
            with self.subTest(name=name):
                self.assertEqual(core.normalize_provider(name), "agy")
        self.assertEqual(fasthook.instance_key("antigravity", "/srv/project"),
                         core.instance_key("agy", "/srv/project"))

    def test_agent_kind_and_ancestor(self):
        args = "/home/vlad/.local/bin/agy --model claude-opus-5-5-high --dangerously-skip-permissions"
        with mock.patch.object(core, "_ps_field", return_value=args):
            self.assertEqual(core.agent_kind(1234), "agy")
        self.assertTrue(core._is_agent_command(args))

    def test_one_shot_flags(self):
        cases = {
            "/home/vlad/.local/bin/agy --model x": False,
            "/home/vlad/.local/bin/agy -p hello": True,
            "/home/vlad/.local/bin/agy --prompt hello": True,
            "/home/vlad/.local/bin/agy --print=hello": True,
            "/home/vlad/.local/bin/aiq run agy -- --prompt=hello": True,
        }
        for args, one_shot in cases.items():
            with self.subTest(args=args), mock.patch.object(core, "_ps_field", return_value=args):
                self.assertEqual(core._is_one_shot_agent(1234), one_shot)


class HookContractTests(unittest.TestCase):
    def test_the_payload_reads_as_claudes(self):
        payload = hooks._common_payload(dict(STOP), "Stop")
        self.assertEqual(payload["cwd"], "/srv/project")
        self.assertEqual(payload["session_id"], STOP["conversationId"])
        self.assertEqual(payload["transcript_path"], STOP["transcriptPath"])
        self.assertEqual(payload["hook_event_name"], "Stop")
        self.assertNotIn("stop_hook_active", payload)
        continued = hooks._common_payload({**STOP, "executionNum": 1}, "Stop")
        self.assertTrue(continued["stop_hook_active"])

    def test_fasthook_looks_in_the_workspace_not_the_hook_directory(self):
        self.assertEqual(fasthook.payload_cwd({"workspacePaths": ["/srv/project"]}), "/srv/project")
        self.assertEqual(fasthook.payload_cwd({"cwd": "/a", "workspacePaths": ["/b"]}), "/a")
        self.assertEqual(fasthook.payload_cwd({}), os.getcwd())

    def test_context_goes_in_as_a_system_message(self):
        member = {"member_id": "mb_agy00001"}
        with mock.patch.object(hooks, "hook_member", return_value=member), \
                mock.patch.object(hooks, "quiet_until", return_value=None), \
                mock.patch.object(hooks, "_attention_lines", return_value=[]), \
                mock.patch.object(hooks, "_mail_lines", return_value=["Agent Orchestra: 1 waiting"]):
            result = hooks.hook_context("agy", {"hook_event_name": "SessionStart"})
        self.assertEqual(result, {"injectSteps": [
            {"systemMessage": {"systemMessage": "Agent Orchestra: 1 waiting"}}]})

    def test_only_a_turns_first_model_call_gets_context(self):
        with mock.patch.object(hooks, "hook_member") as hook_member:
            self.assertEqual(hooks.hook_context(
                "agy", {"hook_event_name": "PreInvocation", "invocationNum": 3}), {})
        hook_member.assert_not_called()

    def test_stop_continues_with_the_mail_and_not_twice(self):
        with mock.patch.object(hooks, "hook_member", return_value={"member_id": "mb_agy00001"}), \
                mock.patch.object(hooks, "quiet_until", return_value=None), \
                mock.patch.object(hooks, "_hook_message_nudge", return_value="mail"):
            self.assertEqual(hooks.hook_stop("agy", hooks._common_payload(dict(STOP))),
                             {"decision": "continue", "reason": "mail"})
            self.assertEqual(hooks.hook_stop(
                "agy", hooks._common_payload({**STOP, "executionNum": 1})), {})
            self.assertEqual(hooks.hook_stop("claude", {}), {"decision": "block", "reason": "mail"})

    def test_the_hook_command_reads_the_event_flag(self):
        stdin = sys.stdin
        self.addCleanup(lambda: setattr(sys, "stdin", stdin))
        sys.stdin = io.StringIO(json.dumps(STOP))
        with mock.patch.object(hooks, "hook_stop", return_value={}) as hook_stop, \
                redirect_stdout(io.StringIO()):
            from agent_orchestra import cli
            cli.run(cli.build_parser().parse_args(["hook-stop", "--provider", "agy", "--event", "Stop"]))
        self.assertEqual(hook_stop.call_args.args[1]["hook_event_name"], "Stop")

    def test_the_plugin_hooks_file_is_flat_and_relative(self):
        spec = json.loads((Path(__file__).resolve().parents[1] / "hooks.json").read_text())
        events = spec["agent-orchestra"]
        self.assertEqual(set(events), {"SessionStart", "PreInvocation", "Stop"})
        for event, handlers in events.items():
            with self.subTest(event=event):
                command = handlers[0]["command"]
                # agy runs a plugin hook in the plugin's own directory.
                self.assertTrue(command.startswith("./bin/agent-orchestra hook-"))
                self.assertIn(f"--provider agy --event {event}", command)


class ScreenTests(unittest.TestCase):
    def test_an_empty_box_is_idle(self):
        for screen in (IDLE, IDLE_WITH_TASK):
            with self.subTest(screen=screen[-60:]):
                self.assertTrue(keys.read_prompt(screen, "agy").idle)

    def test_a_running_turn_is_not_idle(self):
        prompt = keys.read_prompt(BUSY, "agy")
        self.assertTrue(prompt.shown)
        self.assertTrue(prompt.working)
        spinner_only = BUSY.replace("esc to cancel", "? for shortcuts")
        self.assertTrue(keys.read_prompt(spinner_only, "agy").working)

    def test_typed_text_is_not_idle(self):
        self.assertEqual(keys.read_prompt(IDLE.replace("\n>\n", "\n> half a thought\n"), "agy").typed,
                         "half a thought")

    def test_a_markdown_quote_is_not_the_box(self):
        screen = "● Notes:\n> quoted line\n\nmore output\nesc to cancel"
        self.assertFalse(keys.read_prompt(screen, "agy").shown)

    def test_a_claude_permission_dialog_is_not_working_text(self):
        claude = "\n".join(["Esc to cancel", "", "⏺ Done.", "─" * 40, "❯ ", "─" * 40, "  ? for shortcuts"])
        self.assertTrue(keys.read_prompt(claude, "claude").idle)


class WakeTests(unittest.TestCase):
    MEMBER = "mb_agy00001"

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-agy-")
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.member = {"member_id": self.MEMBER, "name": "trumpet", "provider": "agy",
                       "owner_pid": os.getpid()}
        atomic_write_json(member_path(self.MEMBER), self.member)
        self.prompt = mock.Mock(return_value=keys.Prompt(shown=True, typed="", working=False))
        self.type_text = mock.Mock(return_value=True)
        self.find_pane = mock.Mock(return_value=keys.Pane(id="%7", pid=1, session="aiq-ops"))
        for name, value in (("find_pane", self.find_pane), ("prompt", self.prompt),
                            ("type_text", self.type_text)):
            patcher = mock.patch.object(keys, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def deliver(self, message_id: str, **row) -> None:
        row = {"id": message_id, "act": "ask", "need": "reply", "text": "x", **row}
        atomic_write_json(bucket_dir(self.MEMBER, "pending") / f"{message_id}.json", row)

    def test_an_idle_session_gets_one_notice_per_message(self):
        self.deliver("m_" + "a" * 16)
        member_module._wake_agy(self.member)
        member_module._wake_agy(self.member)
        self.type_text.assert_called_once()
        pane, agent, notice = self.type_text.call_args.args
        self.assertEqual((pane, agent), ("%7", "agy"))
        self.assertIn(f"inbox --provider agy --member-id {self.MEMBER} --claim --json", notice)
        self.assertNotIn("\n", notice)
        self.assertTrue(self.type_text.call_args.kwargs["submit"])

    def test_new_mail_waits_out_the_minute(self):
        self.deliver("m_" + "a" * 16)
        member_module._wake_agy(self.member)
        self.deliver("m_" + "b" * 16)
        member_module._wake_agy(self.member)
        self.assertEqual(self.type_text.call_count, 1)
        with mock.patch.object(member_module, "now", return_value=member_module.now() + 61):
            member_module._wake_agy(self.member)
        self.assertEqual(self.type_text.call_count, 2)

    def test_no_notice_over_a_running_turn_typed_text_or_a_quiet(self):
        self.deliver("m_" + "a" * 16)
        for prompt in (keys.Prompt(shown=True, typed="", working=True),
                       keys.Prompt(shown=True, typed="draft", working=False),
                       keys.Prompt(shown=False, typed="", working=False)):
            self.prompt.return_value = prompt
            member_module._wake_agy(self.member)
        self.prompt.return_value = keys.Prompt(shown=True, typed="", working=False)
        with mock.patch.object(member_module, "quiet_until", return_value=1e12):
            member_module._wake_agy(self.member)
        self.type_text.assert_not_called()
        member_module._wake_agy(self.member)
        self.type_text.assert_called_once()

    def test_fyi_mail_and_mail_the_stop_hook_showed_wait(self):
        self.deliver("m_" + "a" * 16, act="tell", need="none")
        self.deliver("m_" + "b" * 16)
        member_module.agy_observed(self.MEMBER, ["m_" + "b" * 16])
        member_module._wake_agy(self.member)
        self.type_text.assert_not_called()

    def test_other_seats_are_left_alone(self):
        self.deliver("m_" + "a" * 16)
        member_module._wake_agy({**self.member, "provider": "claude"})
        self.type_text.assert_not_called()

    def test_capability_follows_the_pane(self):
        wake = member_module.wake_capability("agy", self.MEMBER)
        self.assertTrue(wake["idle_reawaken"])
        self.assertEqual(wake["pane"], "%7")
        self.find_pane.return_value = None
        self.assertFalse(member_module.wake_capability("agy", self.MEMBER)["idle_reawaken"])


if __name__ == "__main__":
    unittest.main()
