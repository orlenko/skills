"""agent-nudge against a fake tmux: what it reads, when it types, and when it holds back."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_nudge import daemon, judge, orchestra, screen, system  # noqa: E402

DIM = "\x1b[2m"
RESET = "\x1b[0m"
RULE = "─" * 60


def claude_screen(body: str, typed: str = "", ghost: str = "", footer: str = "  ⏵⏵ bypass permissions on") -> str:
    prompt = f"\x1b[39m❯\xa0{typed}" + (f"{DIM}{ghost}{RESET}" if ghost else "")
    return "\n".join([body, "", RULE, prompt, RULE, footer, ""])


IDLE_BODY = "⏺ Opened the PR and pushed.\n\n✻ Brewed for 1m 33s · done 11:30 AM"


class ScreenTest(unittest.TestCase):
    def test_ghost_suggestion_is_not_typed(self):
        scr = screen.parse(claude_screen(IDLE_BODY, ghost="check messages"), "claude")
        self.assertTrue(scr.has_prompt)
        self.assertEqual(scr.typed, "")

    def test_real_text_is_typed(self):
        scr = screen.parse(claude_screen(IDLE_BODY, typed="Trumpet sez: "), "claude")
        self.assertEqual(scr.typed, "Trumpet sez:")

    def test_spinner_and_interrupt_mean_working(self):
        self.assertTrue(screen.parse(claude_screen("⏺ Reading.\n✳ Nucleating…"), "claude").working_marker)
        codex = "• Working (12s • esc to interrupt)\n\n› \n  100% context left"
        self.assertTrue(screen.parse(codex, "codex").working_marker)
        self.assertFalse(screen.parse(claude_screen(IDLE_BODY), "claude").working_marker)

    def test_footer_watchers(self):
        scr = screen.parse(claude_screen(IDLE_BODY, footer="  ⏵⏵ bypass permissions on · 1 monitor ·"), "claude")
        self.assertEqual(scr.watchers, 1)

    def test_watchers_in_a_narrow_pane(self):
        body = IDLE_BODY.replace("done 11:30 AM", "done 6:33 PM · 1\n  monitor still running")
        scr = screen.parse(claude_screen(body, footer="  ⏵⏵ bypass permissions on · 1 monit"), "claude")
        self.assertEqual(scr.watchers, 1)

    def test_dialog_has_no_prompt(self):
        dialog = "Do you want to proceed?\n  1. Yes\n  2. No\n\nEsc to cancel"
        self.assertFalse(screen.parse(dialog, "claude").has_prompt)

    def test_menu_cursor_reads_as_typed(self):
        # Claude marks the selected option with the same glyph; the guard must hold.
        dialog = "Do you want to proceed?\n\x1b[39m❯ 1. Yes\n  2. No"
        self.assertTrue(screen.parse(dialog, "claude").typed)

    def test_hash_ignores_footer(self):
        a = screen.parse(claude_screen(IDLE_BODY, footer="Reset: 3hr 59m"), "claude")
        b = screen.parse(claude_screen(IDLE_BODY, footer="Reset: 3hr 58m"), "claude")
        self.assertEqual(a.body_hash, b.body_hash)


class AgentDetectionTest(unittest.TestCase):
    def test_argv(self):
        self.assertEqual(system.agent_of(["/Users/v/.local/bin/claude", "--settings", "{}"]), "claude")
        self.assertEqual(system.agent_of(["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"]), "codex")
        self.assertIsNone(system.agent_of(["/bin/zsh", "-l"]))
        self.assertIsNone(system.agent_of(["npm", "exec", "@playwright/mcp"]))

    def test_wrapped_codex_keeps_the_inner_pid(self):
        table = {10: (1, ["-zsh"]),
                 11: (10, ["node", "/home/v/.nvm/versions/node/v22/bin/codex", "--enable", "hooks"]),
                 12: (11, ["/home/v/.nvm/.../codex/codex", "--enable", "hooks"]),
                 13: (12, ["codex-code-mode"])}
        pane = system.Pane(id="%1", pid=10, session="s", window="@1", path="/w", opt_out=False)
        system.find_agent(pane, table)
        self.assertEqual((pane.agent, pane.agent_pid), ("codex", 11))
        self.assertEqual(pane.extra["agent_pids"], [11, 12])

    def test_tree_walk_finds_child(self):
        table = {10: (1, ["-zsh"]), 11: (10, ["aiq", "long"]), 12: (11, ["/bin/codex", "--yolo"])}
        pane = system.Pane(id="%1", pid=10, session="s", window="@1", path="/w", opt_out=False)
        system.find_agent(pane, table)
        self.assertEqual((pane.agent, pane.agent_pid), ("codex", 12))


class TmuxOutputTest(unittest.TestCase):
    def test_panes_split_on_a_printable_separator(self):
        saved = system.run
        line = system._SEP.join(["%3", "74923", "aiq-ops5", "@2", "/home/vlad/code/ops", "0", ""])
        system.run = lambda args: line + "\n"
        try:
            [pane] = system.panes()
        finally:
            system.run = saved
        self.assertEqual((pane.id, pane.pid, pane.path, pane.opt_out), ("%3", 74923, "/home/vlad/code/ops", False))


class SendTest(unittest.TestCase):
    def setUp(self):
        self.saved = system.run
        self.calls = []
        self.composer = ""

        def run(args):
            self.calls.append(args)
            if args[1] == "send-keys" and args[-1] == "Enter":
                if self.submits_on.pop(0):
                    self.composer = ""
            elif args[1] == "send-keys":
                self.composer = args[-1]
            return f"output\n› {self.composer}\n  footer" if args[1] == "capture-pane" else ""
        system.run = run

    def tearDown(self):
        system.run = self.saved

    def test_pauses_before_enter_and_retries_once(self):
        self.submits_on = [False, True]  # the first Enter becomes a newline, the second submits
        pauses = []
        self.assertTrue(system.send("%1", "[agent-nudge] still waiting?", sleep=pauses.append))
        self.assertEqual(pauses[0], system.SUBMIT_PAUSE_SECONDS)
        self.assertEqual([c[-1] for c in self.calls if c[1] == "send-keys"],
                         ["[agent-nudge] still waiting?", "Enter", "Enter"])

    def test_reports_text_left_unsent(self):
        self.submits_on = [False, False, False]
        self.assertFalse(system.send("%1", "[agent-nudge] still waiting?", sleep=lambda s: None))


class OrchestraFilesTest(unittest.TestCase):
    def test_reads_seat_mail_and_tasks(self):
        root = Path(tempfile.mkdtemp(prefix="orch-"))
        self.addCleanup(shutil.rmtree, root, True)
        saved = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = str(root)
        self.addCleanup(lambda: os.environ.pop("AGENT_ORCHESTRA_HOME") if saved is None
                        else os.environ.__setitem__("AGENT_ORCHESTRA_HOME", saved))
        member = root / "members" / "mb_t"
        (member / "pending").mkdir(parents=True)
        (root / "runtime").mkdir()
        (member / "member.json").write_text(json.dumps({"member_id": "mb_t", "name": "triangle", "role": "player", "owner_pid": 74923}))
        (member / "pending" / "m_1.json").write_text(json.dumps({"received_at": 100.0}))
        (member / "pending" / "m_2.json").write_text(json.dumps({"received_at": 50.0}))
        (root / "runtime" / "mb_t.tasks.json").write_text(json.dumps({"tasks": [
            {"task": "t_a", "owners": [{"id": "mb_t", "state": "started"}]},
            {"task": "t_b", "owners": [{"id": "mb_t", "state": "done"}]},
            {"task": "t_c", "owners": [{"id": "mb_x", "state": "started"}]}]}))
        closed = root / "members" / "mb_old"
        closed.mkdir()
        (closed / "member.json").write_text(json.dumps({"member_id": "mb_old", "owner_pid": 5, "closed_at": 1}))
        seats = orchestra.seats_by_pid()
        self.assertEqual(list(seats), [74923])
        seat = seats[74923]
        self.assertEqual((seat.unread, seat.oldest_unread_at, seat.open_tasks), (2, 50.0, [("t_a", "started")]))
        self.assertEqual(sorted(seat.unread_times), [50.0, 100.0])


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class NudgerTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="agent-nudge-")
        self.env = {k: os.environ.get(k) for k in ("AGENT_NUDGE_HOME", judge.KEY_ENV, daemon.IDLE_ENV)}
        os.environ["AGENT_NUDGE_HOME"] = self.home
        os.environ[judge.KEY_ENV] = "k"
        os.environ.pop(daemon.IDLE_ENV, None)
        self.saved = {name: getattr(system, name) for name in
                      ("panes", "process_table", "client_activity", "capture", "send")}
        self.saved_ask = judge.ask
        self.saved_seats = orchestra.seats_by_pid
        self.seats = {}
        orchestra.seats_by_pid = lambda: self.seats
        self.screen = claude_screen(IDLE_BODY)
        self.sent: list[str] = []
        self.activity = {}
        self.verdict = {"state": "idle", "needs_human_p": 0.1, "waiting_p": 0.1,
                        "nudge_again_p": 0.9}
        self.asks = 0
        self.judge_contexts: list[str] = []
        self.pane_list = [("%1", 10)]
        system.panes = lambda: [system.Pane(id=p, pid=pid, session="s", window="@1", path="/w", opt_out=False)
                                for p, pid in self.pane_list]
        system.process_table = lambda: {10: (1, ["/usr/bin/claude"])}
        system.client_activity = lambda: self.activity
        system.capture = lambda pane_id: self.screen
        system.send = lambda pane_id, text: (self.sent.append(text), self._answer(text), True)[2]

        def ask(tail, *, context=""):
            self.asks += 1
            self.judge_contexts.append(context)
            return dict(self.verdict)
        judge.ask = ask
        self.clock = FakeClock()
        self.nudger = daemon.Nudger(now=self.clock)

    def _answer(self, text):
        self.screen = claude_screen(IDLE_BODY + f"\n❯ {text}\n⏺ Checking now.")

    def tearDown(self):
        for name, fn in self.saved.items():
            setattr(system, name, fn)
        judge.ask = self.saved_ask
        orchestra.seats_by_pid = self.saved_seats
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)

    def advance(self, minutes):
        self.clock.t += minutes * 60
        return self.nudger.tick()

    def events(self):
        path = daemon.log_path()
        return [json.loads(l)["event"] for l in path.read_text().splitlines()] if path.exists() else []

    def test_dry_run_logs_and_types_nothing(self):
        self.nudger.tick()
        self.assertEqual(self.advance(9), [])
        [row] = self.advance(2)
        self.assertEqual(row["event"], "would_nudge")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.advance(30), [])  # once per stop

    def test_live_sends_once_per_stop_then_backs_off(self):
        daemon.set_mode("live")
        self.nudger.tick()
        [row] = self.advance(11)
        self.assertEqual(row["event"], "nudge")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Is your goal done", self.sent[0])
        self.advance(0.5)   # the reply changes the screen: a run caused by the nudge
        self.advance(11)    # stops again: now three times the threshold
        self.assertEqual(len(self.sent), 1)
        self.advance(20)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("outcome", self.events())

    def test_outside_wake_resets_backoff(self):
        daemon.set_mode("live")
        self.nudger.tick()
        self.advance(11)
        self.advance(0.5)
        self.advance(5)
        self.screen = claude_screen(IDLE_BODY + "\n❯ new task from the person")  # after a quiet spell
        self.advance(0.5)
        self.advance(11)
        self.assertEqual(len(self.sent), 2)

    def test_terminal_answer_retires_repeated_nudges(self):
        daemon.set_mode("live")
        self.nudger.tick()
        self.advance(11)
        self.advance(0.5)
        self.screen = claude_screen(
            IDLE_BODY + "\n❯ [agent-nudge] Is your goal done?\n"
            "⏺ The goal is complete; nothing is pending or blocked."
        )
        self.verdict["nudge_again_p"] = 0.05
        self.advance(0.5)
        [row] = self.advance(31)
        self.assertIn("another nudge would not help", row["reason"])
        self.assertIn("uninterrupted chain of 1 prior nudge/reply cycle", self.judge_contexts[-1])
        self.assertIn("No non-nudge screen wake interrupted the chain", self.judge_contexts[-1])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.advance(240), [])

    def test_continuity_context_recovers_full_span_from_old_state(self):
        at = self.clock.t
        rec = {"run_from_nudge": True, "last_nudge_at": at - 60,
               "nudges": [at - 6 * 3600, at - 3 * 3600, at - 3600]}
        context = daemon._judge_context(rec, at, idle=55 * 60, streak=3)
        self.assertIn("3 prior nudge/reply cycles spanning 360.0 minutes", context)

    def test_open_orchestra_task_overrides_terminal_screen_verdict(self):
        daemon.set_mode("live")
        self.verdict["nudge_again_p"] = 0.05
        self.seats = {10: orchestra.Seat("mb_t", "triangle", "player",
                                         open_tasks=[("t_still-open", "started")])}
        self.nudger.tick()
        self.advance(11)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("t_still-open (started)", self.sent[0])

    def _held_back(self, arrange):
        daemon.set_mode("live")
        arrange()
        self.nudger.tick()
        self.advance(11)
        self.assertEqual(self.sent, [])

    def test_holds_back_for_typed_text(self):
        self._held_back(lambda: setattr(self, "screen", claude_screen(IDLE_BODY, typed="half a thought")))

    def test_holds_back_for_an_active_person(self):
        self._held_back(lambda: self.activity.update(s=self.clock.t + 60 * 10))

    def test_holds_back_for_a_question_to_the_user(self):
        self._held_back(lambda: self.verdict.update(needs_human_p=0.8))

    def test_holds_back_when_judge_says_working(self):
        self._held_back(lambda: self.verdict.update(state="working"))

    def test_live_needs_the_judge(self):
        daemon.set_mode("live")
        os.environ.pop(judge.KEY_ENV)
        self.nudger.tick()
        [row] = self.advance(11)
        self.assertIn("needs TYPESAFE_API_KEY", row["reason"])
        self.assertEqual(self.sent, [])

    def test_waiting_without_watcher_gets_the_pointed_nudge(self):
        daemon.set_mode("live")
        self.verdict["waiting_p"] = 0.9
        self.nudger.tick()
        self.advance(11)
        self.assertIn("nothing is watching", self.sent[0])

    def test_watcher_extends_the_wait(self):
        daemon.set_mode("live")
        self.screen = claude_screen(IDLE_BODY, footer="  ⏵⏵ bypass permissions on · 1 monitor ·")
        self.nudger.tick()
        self.advance(11)
        self.assertEqual(self.sent, [])
        self.advance(50)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Is your goal done", self.sent[0])

    def test_judge_called_once_per_stop(self):
        self.verdict["needs_human_p"] = 0.9
        self.nudger.tick()
        self.advance(11)
        self.advance(1)
        self.advance(1)
        self.assertEqual(self.asks, 1)

    def test_unread_orchestra_mail_nudges_after_two_minutes(self):
        daemon.set_mode("live")
        self.verdict["needs_human_p"] = 0.9  # mail beats "the last message asks a person"
        self.nudger.tick()
        old = self.clock.t - 3600  # shown before this stop and left unfinished
        new = [self.clock.t + 30, self.clock.t + 40]
        self.seats = {10: orchestra.Seat("mb_t", "triangle", "player", unread=3, unread_times=[old] + new)}
        self.advance(1)
        self.assertEqual(self.sent, [])
        self.advance(1.5)
        self.assertIn("2 Agent Orchestra messages arrived while you were idle", self.sent[0])

    def test_mail_the_agent_already_left_is_not_news(self):
        daemon.set_mode("live")
        self.nudger.tick()
        self.seats = {10: orchestra.Seat("mb_t", "triangle", "player", unread=5,
                                         unread_times=[self.clock.t - 600] * 5)}
        [row] = self.advance(11)
        self.assertIn("no new mail", row["reason"])
        self.assertEqual(self.sent, [])

    def test_player_with_nothing_open_is_left_alone(self):
        daemon.set_mode("live")
        self.seats = {10: orchestra.Seat("mb_t", "triangle", "player")}
        self.nudger.tick()
        [row] = self.advance(11)
        self.assertIn("nothing open and no new mail", row["reason"])
        self.assertEqual(self.sent, [])

    def test_conductor_is_nudged_without_tasks(self):
        daemon.set_mode("live")
        self.seats = {10: orchestra.Seat("mb_m", "maestro", "conductor")}
        self.nudger.tick()
        self.advance(11)
        self.assertEqual(len(self.sent), 1)

    def test_open_tasks_are_named(self):
        daemon.set_mode("live")
        self.seats = {10: orchestra.Seat("mb_t", "trumpet", "player", open_tasks=[("t_ci-sweep", "started")])}
        self.nudger.tick()
        self.advance(11)
        self.assertIn("t_ci-sweep (started)", self.sent[0])

    def test_gone_pane_forgotten(self):
        self.nudger.tick()
        self.pane_list = []
        self.nudger.tick()
        self.assertEqual(daemon.load_state(), {})

    def test_mode_defaults_to_dry_run(self):
        self.assertEqual(daemon.mode(), "dry-run")
        with self.assertRaises(ValueError):
            daemon.set_mode("loud")


if __name__ == "__main__":
    unittest.main()
