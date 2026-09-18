"""The shadow Jev judge logs and never steers.

No network: `ask_jev` is patched. Covers the gate, both transcript renderers,
the per-member rate limit and single flight, sha reuse, error rows, the timer
view it logs beside Jev, and that nothing outside the module reads it.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra import jev_shadow  # noqa: E402
from agent_orchestra.core import atomic_write_json, bucket_dir, runtime_dir  # noqa: E402


ANSWER = {
    "activity": "working", "activity_probs": {"working": 0.97}, "activity_conf": 0.95,
    "needs_human_p": 0.04, "reported_recently_p": 0.1, "input_tokens": 800,
    "model": "jev-test", "latency_ms": 5,
}


def claude_lines(open_call: bool) -> list[dict]:
    rows = [
        {"type": "user", "timestamp": "2026-09-18T10:00:00Z",
         "message": {"role": "user", "content": "run the tests"}},
        {"type": "assistant", "timestamp": "2026-09-18T10:00:01Z",
         "message": {"content": [{"type": "text", "text": "Running them."},
                                 {"type": "tool_use", "id": "tu1", "name": "Bash",
                                  "input": {"command": "pytest"}}]}},
        {"type": "attachment", "timestamp": "2026-09-18T10:00:01Z"},
    ]
    if not open_call:
        rows += [
            {"type": "user", "timestamp": "2026-09-18T10:00:30Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "tu1",
                                      "content": "3 passed"}]}},
            {"type": "assistant", "timestamp": "2026-09-18T10:00:31Z",
             "message": {"content": [{"type": "text", "text": "All green."}]}},
            {"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-18T10:00:31Z"},
        ]
    return rows


CODEX_LINES = [
    {"type": "event_msg", "timestamp": "2026-09-18T10:00:00Z", "payload": {"type": "task_started"}},
    {"type": "response_item", "timestamp": "2026-09-18T10:00:01Z",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "Sending my report."}]}},
    {"type": "response_item", "timestamp": "2026-09-18T10:00:02Z",
     "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                 "input": "agent-orchestra send 'ACT report'"}},
    {"type": "response_item", "timestamp": "2026-09-18T10:00:03Z",
     "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                 "output": [{"type": "input_text", "text": "queued"}]}},
    {"type": "event_msg", "timestamp": "2026-09-18T10:00:04Z", "payload": {"type": "task_complete"}},
    {"type": "event_msg", "timestamp": "2026-09-18T10:00:04Z", "payload": {"type": "token_count"}},
]


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class JevShadowTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-orchestra-jev-")
        self.env = {k: os.environ.get(k) for k in (
            "AGENT_ORCHESTRA_HOME", jev_shadow.FLAG_ENV, jev_shadow.KEY_ENV,
            jev_shadow.INTERVAL_ENV, "CODEX_HOME")}
        os.environ["AGENT_ORCHESTRA_HOME"] = self.state
        os.environ[jev_shadow.FLAG_ENV] = "1"
        os.environ[jev_shadow.KEY_ENV] = "test-key"
        os.environ.pop(jev_shadow.INTERVAL_ENV, None)
        self.calls: list[str] = []
        self._ask = jev_shadow.ask_jev
        jev_shadow.ask_jev = lambda state, **kw: (self.calls.append(state), dict(ANSWER))[1]
        jev_shadow._STATE.clear()
        self.member = {"member_id": "mb_p1", "provider": "claude", "conductor_id": "mb_c"}
        self.transcript = write_jsonl(Path(self.state) / "t.jsonl", claude_lines(open_call=True))
        jev_shadow.record_transcript(self.member, "claude", {
            "session_id": "s1", "transcript_path": str(self.transcript)})

    def tearDown(self):
        jev_shadow.ask_jev = self._ask
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.state, ignore_errors=True)

    def rows(self) -> list[dict]:
        path = jev_shadow.log_path("mb_p1")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def run_tick(self, seat: str = "held") -> bool:
        started = jev_shadow.tick(self.member, seat)
        thread = jev_shadow._STATE.get("mb_p1", {}).get("thread")
        if thread is not None:
            thread.join(5)
        return started

    def test_gate_needs_flag_and_key(self):
        os.environ.pop(jev_shadow.KEY_ENV)
        self.assertFalse(jev_shadow.enabled())
        self.assertFalse(self.run_tick())
        os.environ[jev_shadow.KEY_ENV] = "k"
        os.environ[jev_shadow.FLAG_ENV] = "0"
        self.assertFalse(jev_shadow.enabled())
        self.assertEqual(self.rows(), [])

    def test_only_a_held_seat_is_sampled(self):
        for seat in ("empty", "unverified"):
            self.assertFalse(self.run_tick(seat))
        self.assertEqual(self.calls, [])

    def test_claude_open_tool_call(self):
        tail = jev_shadow.render_tail("claude", self.transcript)
        self.assertTrue(tail["open_tool_call"])
        self.assertEqual(tail["last_tool"], "Bash")
        self.assertIn("a Bash tool call with no result yet", tail["text"])
        self.assertIn("user: run the tests", tail["text"])
        self.assertNotIn("attachment", tail["text"])

    def test_claude_finished_turn(self):
        write_jsonl(self.transcript, claude_lines(open_call=False))
        tail = jev_shadow.render_tail("claude", self.transcript)
        self.assertFalse(tail["open_tool_call"])
        self.assertEqual(tail["last_event"], "turn_end")
        self.assertIn("tool result (ok): 3 passed", tail["text"])

    def test_codex_rollout_found_by_session_id(self):
        home = Path(self.state) / "codex"
        folder = home / "sessions" / "2026" / "09" / "18"
        folder.mkdir(parents=True)
        write_jsonl(folder / "rollout-2026-09-18T10-00-00-th_9.jsonl", CODEX_LINES)
        os.environ["CODEX_HOME"] = str(home)
        member = {"member_id": "mb_x", "provider": "codex", "session_id": "th_9"}
        provider, path = jev_shadow.transcript_for(member)
        self.assertEqual(provider, "codex")
        tail = jev_shadow.render_tail(provider, path)
        self.assertEqual(tail["last_event"], "turn_end")
        self.assertIn("tool result: queued", tail["text"])
        self.assertIn("tool call exec: agent-orchestra send", tail["text"])
        self.assertNotIn("token_count", tail["text"])

    def test_tail_is_capped(self):
        rows = [{"type": "user", "timestamp": "2026-09-18T10:00:00Z",
                 "message": {"content": f"line {i} " + "x" * 200}} for i in range(100)]
        write_jsonl(self.transcript, rows)
        tail = jev_shadow.render_tail("claude", self.transcript)
        body = tail["text"].split("\n---\n", 1)[1]
        self.assertLessEqual(len(body.encode()), jev_shadow.TAIL_BYTES)
        self.assertIn("line 99", body)

    def test_sample_logs_row_and_tail(self):
        self.assertTrue(self.run_tick())
        [row] = self.rows()
        self.assertEqual(row["jev"]["activity"], "working")
        self.assertFalse(row["reused"])
        self.assertTrue(row["open_tool_call"])
        tail_file = jev_shadow.tails_dir("mb_p1") / f"{row['sha']}.txt"
        self.assertEqual(tail_file.read_text(), self.calls[0])
        self.assertEqual(tail_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(jev_shadow.log_path("mb_p1").stat().st_mode & 0o777, 0o600)

    def test_rate_limit_and_sha_reuse(self):
        os.environ[jev_shadow.INTERVAL_ENV] = "0.05"
        self.assertTrue(self.run_tick())
        self.assertFalse(self.run_tick())  # inside the interval
        time.sleep(0.06)
        self.assertTrue(self.run_tick())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([row["reused"] for row in self.rows()], [False, True])
        write_jsonl(self.transcript, claude_lines(open_call=False))
        time.sleep(0.06)
        self.run_tick()
        self.assertEqual(len(self.calls), 2)

    def test_single_flight(self):
        gate = threading.Event()
        jev_shadow.ask_jev = lambda state, **kw: (gate.wait(5), dict(ANSWER))[1]
        os.environ[jev_shadow.INTERVAL_ENV] = "0.001"
        self.assertTrue(jev_shadow.tick(self.member, "held"))
        time.sleep(0.01)
        self.assertFalse(jev_shadow.tick(self.member, "held"))
        gate.set()
        jev_shadow._STATE["mb_p1"]["thread"].join(5)

    def test_failure_is_a_row(self):
        def boom(state, **kw):
            raise TimeoutError("timed out")
        jev_shadow.ask_jev = boom
        self.assertTrue(self.run_tick())
        [row] = self.rows()
        self.assertIn("TimeoutError", row["error"])
        self.assertNotIn("jev", row)

    def test_missing_transcript_is_a_row(self):
        self.transcript.unlink()
        self.run_tick()
        [row] = self.rows()
        self.assertEqual(row["error"], "no transcript")

    def test_timer_view_and_sends(self):
        long_ago = time.time() - 7200
        atomic_write_json(runtime_dir() / "mb_p1.tasks.json", {"fetched_at": time.time(), "tasks": [
            {"task": "t_a", "sender": "mb_c", "message_id": "m1",
             "owners": [{"id": "mb_p1", "state": "started", "state_at": long_ago,
                         "last_report_at": long_ago}]},
            {"task": "t_b", "sender": "mb_p1", "message_id": "m2",
             "owners": [{"id": "mb_p2", "state": "pending", "state_at": long_ago}]},
        ]})
        atomic_write_json(bucket_dir("mb_p1", "sent") / "x.json",
                          {"act": "report", "queued_locally_at": time.time()})
        write_jsonl(self.transcript, [{**row, "timestamp": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60))} for row in claude_lines(True)])
        self.run_tick()
        [row] = self.rows()
        timer = row["timer"]
        self.assertEqual(timer["on_me"], "stale")
        self.assertEqual(timer["tasks"][0]["task"], "t_a")
        self.assertEqual([i["kind"] for i in timer["self_attention"]], ["no-response"])
        self.assertFalse(timer["is_conductor"])
        self.assertEqual(row["sends_in_tail"], ["report"])

    def test_tick_leaves_member_and_snapshot_alone(self):
        snapshot = runtime_dir() / "mb_p1.tasks.json"
        atomic_write_json(snapshot, {"fetched_at": 1.0, "tasks": []})
        before_member, before_snapshot = dict(self.member), snapshot.read_bytes()
        self.run_tick()
        self.assertEqual(self.member, before_member)
        self.assertEqual(snapshot.read_bytes(), before_snapshot)

    def test_nothing_else_reads_the_shadow(self):
        package = PLUGIN_ROOT / "agent_orchestra"
        readers = {
            path.name for path in package.glob("*.py")
            if path.name != "jev_shadow.py" and "jev_shadow" in path.read_text()
        }
        self.assertEqual(readers, {"member.py", "hooks.py"})
        member_src = (package / "member.py").read_text()
        self.assertEqual(re.findall(r"jev_shadow\.\w+", member_src), ["jev_shadow.tick"])
        hooks_src = (package / "hooks.py").read_text()
        self.assertEqual(
            sorted(set(re.findall(r"jev_shadow\.\w+", hooks_src))),
            ["jev_shadow.enabled", "jev_shadow.record_transcript"],
        )


if __name__ == "__main__":
    unittest.main()
