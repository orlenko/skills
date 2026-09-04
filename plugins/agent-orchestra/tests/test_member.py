import os
import shutil
import signal
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
    APIError,
    OrchestraError,
    api_request,
    bucket_dir,
    decode_invite,
    encode_invite,
    now,
    read_json,
)


# The hub runs in a separate process, so the constants a test shortens must be
# patched in this process and the hub re-served here. Names are probed because
# hub.py owns them.
_STALE_CONSTANTS = (
    "PRESENCE_STALE_SECONDS",
    "PRESENCE_TIMEOUT_SECONDS",
    "STALE_AFTER_SECONDS",
    "PRESENCE_STALE_AFTER",
)
_SWEEP_CONSTANTS = (
    "PRESENCE_INTERVAL_SECONDS",
    "PRESENCE_SWEEP_SECONDS",
    "PRESENCE_TICK_SECONDS",
    "PRESENCE_INTERVAL",
)


def _ids(recipients: Any) -> list[str]:
    out = []
    for item in recipients or []:
        out.append(str(item["id"]) if isinstance(item, dict) else str(item))
    return sorted(out)


def _last_line(text: str) -> str:
    return [line for line in str(text).splitlines() if line.strip()][-1]


class MemberTestCase(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-orchestra-test-")
        self.previous_home = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = self.state
        self.hub_pids: list[int] = []
        self.inprocess_hubs: list[str] = []
        self.monitor_threads: list[tuple[str, threading.Thread]] = []

        # No desktop notifications and no real monitor subprocesses: tests drive
        # monitor_loop in daemon threads instead.
        self._patch(member_module, "_notify", lambda *args, **kwargs: None)
        # Kept so one test can check the real spawn guard, not the stub.
        self.real_start_monitor = member_module.start_monitor
        self._patch(member_module, "start_monitor", lambda member: 0)
        self._patch(member_module, "MONITOR_WAIT_SECONDS", 1)

    def tearDown(self):
        for orchestra_id in self.inprocess_hubs:
            try:
                api_request(
                    hub_module.admin_connection(orchestra_id), "POST", "/v1/close", {}, timeout=3
                )
            except OrchestraError:
                pass
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
        background = getattr(hub_module, "_BACKGROUND_PROCESSES", [])
        if reap:
            reap([process.pid for process in background], timeout=0)
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
            name="Test Orchestra", bind="127.0.0.1", advertise=["127.0.0.1"], **kwargs
        )
        self.hub_pids.append(int(created["hub_pid"]))
        return created

    def _serve_in_process(self, created: dict[str, Any], *, stale_after: float, sweep: float) -> None:
        """Re-serve the hub here so shortened presence constants take effect."""
        orchestra_id = created["orchestra_id"]
        self._kill(created["hub_pid"])
        patched_stale = [name for name in _STALE_CONSTANTS if hasattr(hub_module, name)]
        patched_sweep = [name for name in _SWEEP_CONSTANTS if hasattr(hub_module, name)]
        self.assertTrue(patched_stale, "hub.py exposes no presence-stale constant to patch")
        self.assertTrue(patched_sweep, "hub.py exposes no presence-sweep constant to patch")
        for name in patched_stale:
            self._patch(hub_module, name, stale_after)
        for name in patched_sweep:
            self._patch(hub_module, name, sweep)
        thread = threading.Thread(target=hub_module.serve, args=(orchestra_id,), daemon=True)
        thread.start()
        self.inprocess_hubs.append(orchestra_id)
        self._wait_hub_ready(orchestra_id)

    def _wait_hub_ready(self, orchestra_id: str, timeout: float = 15) -> None:
        connection = hub_module.admin_connection(orchestra_id)
        deadline = time.monotonic() + timeout
        last = "never answered"
        while time.monotonic() < deadline:
            try:
                api_request(connection, "GET", "/v1/status", timeout=3)
                return
            except OrchestraError as exc:
                last = str(exc)
                time.sleep(0.1)
        self.fail(f"hub did not become ready: {last}")

    def _join(self, invite: str, session: str, name: str, *, monitor: bool = True) -> dict[str, Any]:
        joined = member_module.join(
            invite,
            provider="test",
            cwd=self._cwd(session),
            name=name,
            start_background_monitor=False,
        )
        member = member_module.load_member(joined["member_id"])
        if monitor:
            self._start_monitor(member["member_id"])
        return member

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

    def _wait_rows(
        self, member: dict[str, Any], count: int, *, timeout: float = 20, claim: bool = False
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = member_module.local_messages(member, claim=claim)
            if len(rows) >= count:
                return rows
            time.sleep(0.1)
        self.fail(f"expected {count} message(s) for {member['name']}, got {len(rows)}")
        return rows

    def _wait_for(self, predicate, *, timeout: float = 20, what: str = "condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    # tests -----------------------------------------------------------------

    def test_four_member_topology_routes_every_alias(self):
        created = self._create_hub()
        orchestra_id = created["orchestra_id"]
        conductor = self._join(created["conductor_invite"], "conductor", "Conductor")
        conductor_id = conductor["member_id"]

        admin = hub_module.admin_connection(orchestra_id)
        minted_a = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        player_a = self._join(minted_a["invite"], "player-a", "Player A")
        minted_b = member_module.invite(conductor, role="player", parent="self")
        player_b = self._join(minted_b["invite"], "player-b", "Player B")
        minted_c = member_module.invite(player_b, role="player", parent="self")
        child_c = self._join(minted_c["invite"], "child-c", "Child C")

        self.assertEqual(conductor["role"], "conductor")
        self.assertEqual(player_a["parent"], conductor_id)
        self.assertEqual(player_b["parent"], conductor_id)
        self.assertEqual(child_c["parent"], player_b["member_id"])
        self.assertEqual(player_a["conductor_id"], conductor_id)

        assign = member_module.send(
            conductor,
            "ACT assign\nTO children\nTASK t_alpha\nNEED plan\n\nBuild the parser and prove it.",
        )
        self.assertEqual(assign["state"], "queued")
        self.assertEqual(
            _ids(assign["recipients"]), sorted([player_a["member_id"], player_b["member_id"]])
        )

        a_rows = self._wait_rows(player_a, 1)
        b_rows = self._wait_rows(player_b, 1)
        self.assertEqual(a_rows[0]["act"], "assign")
        self.assertEqual(a_rows[0]["task"], "t_alpha")
        self.assertEqual(a_rows[0]["need"], "plan")
        self.assertEqual(a_rows[0]["from"]["id"], conductor_id)
        self.assertEqual(b_rows[0]["id"], a_rows[0]["id"])
        self.assertEqual(member_module.pending_count(child_c), 0)
        assign_id = a_rows[0]["id"]

        duplicate = member_module.send(
            conductor, "ACT assign\nTO children\nTASK t_alpha\n\nAssigned twice by mistake."
        )
        self.assertEqual(duplicate["state"], "rejected")
        self.assertEqual(duplicate["status"], 409)
        self.assertIn("t_alpha", duplicate["error"])
        rejected_path = bucket_dir(conductor_id, "sent") / f"{duplicate['id']}.json"
        self.assertTrue(rejected_path.exists())
        self.assertEqual(read_json(rejected_path)["state"], "rejected")
        self.assertEqual(len(list(bucket_dir(conductor_id, "outbox").glob("*.json"))), 0)

        reply = member_module.send(
            player_b,
            f"ACT done\nTO parent\nTASK t_alpha\nRE {assign_id}\n\nParser green: 41 tests pass.",
        )
        self.assertEqual(reply["state"], "queued")
        self.assertEqual(_ids(reply["recipients"]), [conductor_id])

        conductor_rows = self._wait_rows(conductor, 1, claim=True)
        self.assertEqual(conductor_rows[0]["act"], "done")
        self.assertEqual(conductor_rows[0]["re"], assign_id)
        done_id = conductor_rows[0]["id"]

        finished = member_module.finish_messages(conductor, [done_id])
        self.assertEqual(finished[0]["state"], "handled")
        self.assertEqual(finished[0]["sync"], "synced")
        remote_status = member_module.message_status(player_b, done_id)
        recipient = next(
            item for item in remote_status["recipients"] if item["id"] == conductor_id
        )
        self.assertEqual(recipient["state"], "handled")

        broadcast = member_module.send(child_c, "ACT tell\nTO all\n\nChild C is online.")
        self.assertEqual(
            _ids(broadcast["recipients"]),
            sorted([conductor_id, player_a["member_id"], player_b["member_id"]]),
        )
        a_after = self._wait_rows(player_a, 2)
        self.assertIn("Child C is online.", a_after[-1]["text"])
        self._wait_rows(conductor, 1)

        tasks = member_module.tasks(player_a)
        alpha = next(item for item in tasks["tasks"] if item["task"] == "t_alpha")
        self.assertEqual(alpha["sender"], conductor_id)
        self.assertEqual(_ids(alpha["recipients"]), sorted([player_a["member_id"], player_b["member_id"]]))

        state = member_module.status(conductor)
        self.assertTrue(state["hub"]["reachable"])
        self.assertIsNone(state["hub"]["error"])
        self.assertEqual(state["role"], "conductor")
        self.assertEqual(state["conductor"]["presence"], "connected")
        self.assertEqual(len(state["members"]), 4)
        self.assertEqual(state["inbox_by_act"], {"tell": 1})
        self.assertEqual(state["local"]["pending"] + state["local"]["claimed"], 1)
        self.assertGreater(state["local"]["events"], 0)
        self.assertEqual(state["status_owed"]["owed"], False)
        self.assertEqual(state["remote"]["conductor_id"], conductor_id)

        left = member_module.leave(child_c)
        self.assertEqual(left["state"], "left")
        roster = member_module.members(player_b)["members"]
        child_row = next(item for item in roster if item["id"] == child_c["member_id"])
        self.assertEqual(child_row["revoked_reason"], "left")

    def test_conductor_absence_queues_then_drains_in_sent_order(self):
        created = self._create_hub()
        orchestra_id = created["orchestra_id"]
        self._serve_in_process(created, stale_after=3.0, sweep=0.25)

        conductor = self._join(
            created["conductor_invite"], "conductor", "Conductor", monitor=False
        )
        conductor_id = conductor["member_id"]
        admin = hub_module.admin_connection(orchestra_id)
        minted_a = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        player_a = self._join(minted_a["invite"], "player-a", "Player A")
        minted_b = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        player_b = self._join(minted_b["invite"], "player-b", "Player B")

        first = member_module.send(player_a, "ACT tell\nTO conductor\n\nA first")
        self.assertEqual(first["state"], "queued")
        time.sleep(0.05)
        second = member_module.send(player_b, "ACT tell\nTO conductor\n\nB second")
        self.assertEqual(second["state"], "queued")
        self.assertLess(float(first["sent_at"]), float(second["sent_at"]))

        offline = member_module.status(conductor)
        self.assertEqual(offline["remote"]["queued_for_me"], 2)
        self.assertEqual(offline["local"]["pending"], 0)
        self.assertFalse(offline["monitor"]["running"])

        def conductor_row():
            roster = member_module.members(player_a)["members"]
            row = next(item for item in roster if item["id"] == conductor_id)
            return row if row["presence"] == "stale" else None

        self._wait_for(conductor_row, timeout=20, what="the conductor to go stale")

        self._start_monitor(conductor_id)
        drained = self._wait_rows(conductor, 2, timeout=25)
        self.assertEqual(
            [_last_line(row["text"]) for row in drained], ["A first", "B second"]
        )

        def reconnect_event():
            for event in member_module.recent_events(player_a, limit=100):
                if member_module._reconnect_absent_since(event.get("text"), conductor_id) is not None:
                    return event
            return None

        event = self._wait_for(
            reconnect_event, timeout=25, what="a connected event in Player A's events/"
        )
        self.assertEqual(event["from"]["id"], "sys")
        self.assertIn("connected", event["text"])
        self.assertEqual(member_module.pending_count(player_a), 0)

        owed = member_module.status_owed(player_a)
        self.assertTrue(owed["owed"])
        self.assertEqual(owed["conductor_id"], conductor_id)
        self.assertIsNotNone(owed["absent_since"])

        answered = member_module.send(
            player_a, "ACT status\nTO conductor\n\nStill on the parser, no blockers."
        )
        self.assertEqual(answered["state"], "queued")
        self.assertFalse(member_module.status_owed(player_a)["owed"])
        self.assertFalse(member_module.status_owed(conductor)["owed"])

    def test_send_queues_locally_while_the_hub_is_down_and_flushes_after_ensure_hub(self):
        created = self._create_hub()
        orchestra_id = created["orchestra_id"]
        conductor = self._join(created["conductor_invite"], "conductor", "Conductor", monitor=False)
        admin = hub_module.admin_connection(orchestra_id)
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        player_a = self._join(minted["invite"], "player-a", "Player A")

        # A member that is not on the hub machine cannot restart the hub; that is
        # the case this queue exists for.
        real_ensure_hub_if_local = member_module.ensure_hub_if_local
        self._patch(member_module, "ensure_hub_if_local", lambda member: None)
        self._kill(created["hub_pid"])
        self.assertFalse(hub_module.hub_alive(orchestra_id))

        queued = member_module.send(player_a, "ACT tell\nTO conductor\n\nWritten while the hub was down")
        self.assertEqual(queued["state"], "queued-locally")
        self.assertIn("Could not reach hub", queued["detail"])
        outbox = bucket_dir(player_a["member_id"], "outbox")
        self.assertEqual([path.stem for path in outbox.glob("*.json")], [queued["id"]])

        member_module.ensure_hub_if_local = real_ensure_hub_if_local
        restarted = hub_module.ensure_hub(orchestra_id)
        self.hub_pids.append(int(restarted))
        self._wait_hub_ready(orchestra_id)

        self._wait_for(
            lambda: not list(outbox.glob("*.json")),
            timeout=25,
            what="the monitor to flush the outbox",
        )
        sent = read_json(bucket_dir(player_a["member_id"], "sent") / f"{queued['id']}.json")
        self.assertEqual(sent["state"], "queued")
        self.assertEqual(sent["recipients"], [conductor["member_id"]])

        self._start_monitor(conductor["member_id"])
        delivered = self._wait_rows(conductor, 1, timeout=25)
        self.assertIn("Written while the hub was down", delivered[0]["text"])

    def test_rejects_a_tampered_fingerprint_and_a_reused_invite(self):
        created = self._create_hub()
        tampered = decode_invite(created["conductor_invite"])
        tampered["fingerprint"] = "00" * 32
        tampered.pop("v", None)
        with self.assertRaisesRegex(OrchestraError, "fingerprint"):
            member_module.join(
                encode_invite(tampered),
                provider="test",
                cwd=self._cwd("attacker"),
                name="Wrong endpoint",
            )

        conductor = self._join(created["conductor_invite"], "conductor", "Conductor", monitor=False)
        self.assertTrue(conductor["member_id"])
        with self.assertRaises(APIError) as caught:
            member_module.join(
                created["conductor_invite"],
                provider="test",
                cwd=self._cwd("second-conductor"),
                name="Late conductor",
            )
        self.assertEqual(caught.exception.status, 403)

    def test_leave_closes_the_membership_with_the_hub_gone(self):
        created = self._create_hub()
        orchestra_id = created["orchestra_id"]
        self._join(created["conductor_invite"], "conductor", "Conductor", monitor=False)
        admin = hub_module.admin_connection(orchestra_id)
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        player_a = self._join(minted["invite"], "player-a", "Player A", monitor=False)

        self._patch(member_module, "ensure_hub_if_local", lambda member: None)
        self._kill(created["hub_pid"])

        result = member_module.leave(player_a)
        self.assertEqual(result["state"], "left-locally")
        self.assertIn("could not notify the hub", result["detail"])
        stored = member_module.load_member(player_a["member_id"])
        self.assertTrue(stored["closed_at"])
        self.assertEqual(stored["closed_reason"], "left")
        with self.assertRaises(OrchestraError):
            member_module.select_member(provider="test", cwd=self._cwd("player-a"))

    def test_close_reaches_a_running_player_monitor(self):
        created = self._create_hub()
        conductor = self._join(created["conductor_invite"], "conductor", "Conductor")
        conductor_thread = self.monitor_threads[-1][1]
        minted = member_module.invite(conductor, role="player", parent="self")
        player = self._join(minted["invite"], "player", "Player")
        player_thread = self.monitor_threads[-1][1]
        player_id = player["member_id"]

        result = member_module.close(conductor)
        self.assertEqual(result["state"], "closed")

        self._wait_for(
            lambda: member_module.load_member(player_id).get("closed_at"),
            timeout=20,
            what="the closure to reach the player",
        )
        stored = member_module.load_member(player_id)
        self.assertEqual(stored["closed_reason"], "closed")

        player_thread.join(timeout=15)
        conductor_thread.join(timeout=15)
        self.assertFalse(player_thread.is_alive())
        self.assertFalse(conductor_thread.is_alive())
        with self.assertRaises(OrchestraError):
            member_module.select_member(provider="test", cwd=self._cwd("player"))

        # The real spawner refuses a closed membership, so nothing restarts it.
        spawned = len(member_module._BACKGROUND_PROCESSES)
        self.assertEqual(self.real_start_monitor(stored), 0)
        self.assertEqual(len(member_module._BACKGROUND_PROCESSES), spawned)

        # The hub is done once both members have been told. The hub process is a
        # child of this one, so ask hub._pid_alive, which reaps the zombie.
        self._wait_for(
            lambda: not hub_module._pid_alive(int(created["hub_pid"])),
            timeout=30,
            what="the closed hub to exit",
        )

    def test_inbox_puts_reply_required_first_then_sent_at(self):
        member = {
            "member_id": "mb_ordering",
            "name": "Ordering",
            "orchestra_id": "orc_ordering",
        }
        rows = [
            {"id": "m_aaaaaaaaaa", "act": "tell", "need": "none", "sent_at": 10.0},
            {"id": "m_bbbbbbbbbb", "act": "ask", "need": "answer", "sent_at": 30.0},
            {"id": "m_cccccccccc", "act": "tell", "need": "none", "sent_at": 20.0},
            {"id": "m_dddddddddd", "act": "assign", "need": "plan", "sent_at": 5.0},
        ]
        for row in rows:
            path = bucket_dir("mb_ordering", "pending") / f"{row['id']}.json"
            member_module.atomic_write_json(path, {**row, "received_at": now()})

        ordered = member_module.local_messages(member, claim=False)
        self.assertEqual(
            [row["id"] for row in ordered],
            ["m_dddddddddd", "m_bbbbbbbbbb", "m_aaaaaaaaaa", "m_cccccccccc"],
        )
        self.assertEqual(member_module.pending_count(member), 4)
        claimed = member_module.local_messages(member, claim=True)
        self.assertEqual([row["local_state"] for row in claimed], ["claimed"] * 4)


if __name__ == "__main__":
    unittest.main()
