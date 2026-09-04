"""End-to-end member behaviour when the orchestra turns against the member.

Every case here runs against a real hub on 127.0.0.1: a revoked membership, a
conductor handover, a forged presence line, a page of oversized messages, and
the child-reaping rule in `_pid_alive`.
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra import hub as hub_module  # noqa: E402
from agent_orchestra import member as member_module  # noqa: E402
from agent_orchestra.core import (  # noqa: E402
    OrchestraError,
    api_request,
    atomic_write_json,
    bucket_dir,
    new_message_id,
    now,
    read_json,
)


class ResilienceTestCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-orchestra-resilience-")
        self.previous_home = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = self.state
        self.hub_pids: list[int] = []
        self.monitor_threads: list[tuple[str, threading.Thread]] = []

        # No desktop notifications and no monitor subprocesses: a test that wants
        # a monitor runs monitor_loop in a daemon thread it can join.
        self._patch(member_module, "_notify", lambda *args, **kwargs: None)
        self._patch(member_module, "start_monitor", lambda member: 0)
        self._patch(member_module, "MONITOR_WAIT_SECONDS", 1)

    def tearDown(self):
        for member_id, _ in self.monitor_threads:
            try:
                member = member_module.load_member(member_id)
            except OrchestraError:
                continue
            if not member.get("closed_at"):
                member["closed_at"] = now()
                member["closed_reason"] = "test teardown"
                member_module.save_member(member)
        for _, thread in self.monitor_threads:
            thread.join(timeout=6)
        member_module._reap_spawned_processes(
            [process.pid for process in member_module._BACKGROUND_PROCESSES], timeout=0
        )
        reap = getattr(hub_module, "_reap_spawned_processes", None)
        if reap:
            reap(
                [process.pid for process in getattr(hub_module, "_BACKGROUND_PROCESSES", [])],
                timeout=0,
            )
        for pid in self.hub_pids:
            self._kill(pid)
        if self.previous_home is None:
            os.environ.pop("AGENT_ORCHESTRA_HOME", None)
        else:
            os.environ["AGENT_ORCHESTRA_HOME"] = self.previous_home
        shutil.rmtree(self.state, ignore_errors=True)

    # helpers ---------------------------------------------------------------

    def _patch(self, module, name, value):
        previous = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, previous)

    def _kill(self, pid: int | None) -> None:
        if not pid:
            return
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and member_module._pid_alive(int(pid)):
            time.sleep(0.05)

    def _cwd(self, name: str) -> str:
        path = Path(self.state) / "sessions" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _create_hub(self, **kwargs) -> dict[str, Any]:
        created = hub_module.create_hub(
            name="Resilience Orchestra", bind="127.0.0.1", advertise=["127.0.0.1"], **kwargs
        )
        self.hub_pids.append(int(created["hub_pid"]))
        return created

    def _join(self, invite: str, session: str, name: str) -> dict[str, Any]:
        joined = member_module.join(
            invite,
            provider="test",
            cwd=self._cwd(session),
            name=name,
            start_background_monitor=False,
        )
        return member_module.load_member(joined["member_id"])

    def _topology(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """One conductor and one player, neither of them running a monitor."""
        created = self._create_hub()
        conductor = self._join(created["conductor_invite"], "conductor", "Conductor")
        minted = member_module.invite(conductor, role="player", parent="self")
        player = self._join(minted["invite"], "player-a", "Player A")
        return conductor, player

    def _start_monitor(self, member_id: str) -> threading.Thread:
        thread = threading.Thread(
            target=member_module.monitor_loop, args=(member_id,), daemon=True
        )
        thread.start()
        self.monitor_threads.append((member_id, thread))
        path = member_module._monitor_state_path(member_id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not path.exists():
            time.sleep(0.02)
        return thread

    def _write_event(self, member_id: str, text: str, *, sent_at: float) -> str:
        event_id = new_message_id()
        atomic_write_json(
            bucket_dir(member_id, "events") / f"{event_id}.json",
            {
                "id": event_id,
                "from": {"id": "sys", "name": "hub", "role": "sys"},
                "act": "tell",
                "re": None,
                "task": None,
                "need": "none",
                "refs": [],
                "sent_at": sent_at,
                "text": text,
                "received_at": sent_at,
            },
        )
        return event_id

    def _event_texts(self, member_id: str) -> list[str]:
        return [
            str(read_json(path).get("text"))
            for path in bucket_dir(member_id, "events").glob("*.json")
        ]

    # tests -----------------------------------------------------------------

    def test_a_kicked_member_closes_locally_and_its_monitor_exits(self):
        conductor, player = self._topology()
        player_id = str(player["member_id"])
        monitor = self._start_monitor(player_id)

        member_module.kick(conductor, player_id, reason="reassigned")

        monitor.join(timeout=15)
        self.assertFalse(monitor.is_alive(), "the monitor did not exit after the 410")

        stored = member_module.load_member(player_id)
        self.assertTrue(stored["closed_at"], "member.json has no closed_at")
        self.assertEqual(stored["closed_reason"], "kicked")
        self.assertIn("kicked", self._event_texts(player_id))

        result = member_module.send(player, "ACT tell\nTO conductor\n\nAm I still here?")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["reason"], "kicked")
        self.assertEqual(result["status"], 410)
        rejected = read_json(bucket_dir(player_id, "sent") / f"{result['id']}.json")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(len(list(bucket_dir(player_id, "outbox").glob("*.json"))), 0)

    def test_a_revoked_row_reports_its_reason_as_presence(self):
        conductor, leaver = self._topology()
        minted = member_module.invite(conductor, role="player", parent="self")
        kicked = self._join(minted["invite"], "player-b", "Player B")
        conductor_id = str(conductor["member_id"])
        leaver_id = str(leaver["member_id"])
        kicked_id = str(kicked["member_id"])

        self.assertEqual(member_module.leave(leaver)["state"], "left")
        member_module.kick(conductor, kicked_id, reason="reassigned")

        roster = {
            str(row["id"]): row for row in member_module.members(conductor)["members"]
        }
        self.assertEqual(roster[leaver_id]["presence"], "left")
        self.assertEqual(roster[leaver_id]["revoked_reason"], "left")
        self.assertEqual(roster[kicked_id]["presence"], "kicked")
        self.assertEqual(roster[kicked_id]["revoked_reason"], "kicked")
        self.assertEqual(roster[conductor_id]["presence"], "connected")

        summarized = {
            str(row["id"]): row for row in member_module.status(conductor)["members"]
        }
        self.assertEqual(summarized[leaver_id]["presence"], "left")
        self.assertEqual(summarized[kicked_id]["presence"], "kicked")

    def test_conductor_handover_reaches_both_member_files(self):
        conductor, player = self._topology()
        conductor_id = str(conductor["member_id"])
        player_id = str(player["member_id"])

        moved = member_module.set_conductor(conductor, player_id)
        self.assertEqual(moved["conductor_id"], player_id)

        player_status = member_module.status(player)
        self.assertEqual(player_status["role"], "conductor")
        self.assertEqual(player_status["conductor"]["id"], player_id)

        conductor_status = member_module.status(conductor)
        self.assertEqual(conductor_status["role"], "player")
        self.assertEqual(conductor_status["conductor"]["id"], player_id)

        stored_player = member_module.load_member(player_id)
        stored_conductor = member_module.load_member(conductor_id)
        self.assertEqual(stored_player["role"], "conductor")
        self.assertEqual(stored_player["conductor_id"], player_id)
        self.assertEqual(stored_conductor["role"], "player")
        self.assertEqual(stored_conductor["conductor_id"], player_id)

    def test_only_the_first_line_of_an_event_can_owe_a_status(self):
        conductor, player = self._topology()
        conductor_id = str(conductor["member_id"])
        player_id = str(player["member_id"])
        reconnect = f"presence {conductor_id} Conductor connected absent_since=1.0"

        self._write_event(player_id, f"a member wrote this\n{reconnect}", sent_at=now())
        forged = member_module.status_owed(player)
        self.assertFalse(forged["owed"], "a second-line presence line forged a status debt")
        self.assertIsNone(forged["absent_since"])

        self._write_event(player_id, f"{reconnect}\na member wrote this", sent_at=now() + 1)
        real = member_module.status_owed(player)
        self.assertTrue(real["owed"])
        self.assertEqual(real["conductor_id"], conductor_id)
        self.assertEqual(real["absent_since"], 1.0)

        member_module.send(player, "ACT status\nTO conductor\n\nStill on the parser.")
        self.assertFalse(member_module.status_owed(player)["owed"])

    def test_three_oversized_messages_all_drain_into_the_inbox(self):
        conductor, player = self._topology()
        player_id = str(player["member_id"])
        body = "x" * (200 * 1024)

        texts = {}
        for index in range(3):
            text = f"ACT tell\nTO {player_id}\n\n{index}:{body}"
            result = member_module.send(conductor, text)
            self.assertEqual(result["state"], "queued", result)
            texts[str(result["id"])] = text
        self.assertEqual(member_module.pending_count(player), 0)

        self._start_monitor(player_id)
        rows: list[dict[str, Any]] = []
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and len(rows) < 3:
            rows = member_module.wait_for_messages(player, 2, claim=False)
        self.assertEqual(len(rows), 3, "the monitor did not drain every oversized message")
        self.assertEqual(sorted(str(row["id"]) for row in rows), sorted(texts))
        for row in rows:
            self.assertEqual(row["text"], texts[str(row["id"])])

    def test_pid_alive_reaps_its_own_child_before_answering(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        member_module._BACKGROUND_PROCESSES.append(process)
        self.addCleanup(
            member_module._reap_spawned_processes, [process.pid], 5.0
        )

        # kill(pid, 0) still answers for an unreaped zombie, so poll until the
        # process is gone by that measure or a second passes; wait() is never
        # called here, which is exactly the case _pid_alive has to survive.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(process.pid, 0)
            except OSError:
                break
            time.sleep(0.02)

        self.assertFalse(
            member_module._pid_alive(process.pid),
            "_pid_alive reported an exited child alive",
        )

    def test_a_closed_membership_never_gets_a_new_monitor(self):
        conductor, player = self._topology()
        player_id = str(player["member_id"])
        api_request(
            hub_module.admin_connection(str(player["orchestra_id"])),
            "POST",
            "/v1/kick",
            {"member_id": player_id},
        )
        result = member_module.send(player, "ACT tell\nTO conductor\n\nAnyone there?")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["reason"], "kicked")
        self.assertEqual(member_module.ensure_monitor(member_module.load_member(player_id)), 0)


if __name__ == "__main__":
    unittest.main()
