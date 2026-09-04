import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra import core, hub  # noqa: E402
from agent_orchestra.core import (  # noqa: E402
    APIError,
    OrchestraError,
    api_request,
    atomic_write_json,
    decode_invite,
    now,
    read_json,
    secret_hash,
    token,
)
from agent_orchestra.hub import (  # noqa: E402
    HubStore,
    _pid_alive,
    _reap_spawned_processes,
    admin_connection,
    create_hub,
    ensure_hub,
    hub_alive,
    hub_unit,
    local_hubs,
    select_hub,
)


# cli.py is written by another agent in parallel; drive the detached hub through
# hub._serve_main so these tests do not depend on the CLI landing first. The
# production spawn path stays `python -m agent_orchestra serve`.
TEST_SPAWN_ENTRY = (
    "-c",
    "import sys; from agent_orchestra import hub; hub._serve_main(sys.argv[1:])",
)


def _text(act="tell", body="hello"):
    return f"ACT {act}\n\n{body}\n"


class TempHomeTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="agent-orchestra-test-")
        self.previous_home = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = self.state

    def tearDown(self):
        if self.previous_home is None:
            os.environ.pop("AGENT_ORCHESTRA_HOME", None)
        else:
            os.environ["AGENT_ORCHESTRA_HOME"] = self.previous_home
        shutil.rmtree(self.state, ignore_errors=True)


class HubStoreTests(TempHomeTests):
    def setUp(self):
        super().setUp()
        self.admin_token = token(16)
        self.store = HubStore(Path(self.state) / "hub.sqlite")
        self.store.initialize(
            orchestra_id="orc_test",
            name="test hub",
            admin_token_hash=secret_hash(self.admin_token),
            endpoints=["https://127.0.0.1:1"],
            fingerprint="ab" * 32,
        )
        self.constants = {
            name: getattr(hub, name)
            for name in (
                "PRUNE_MESSAGE_SECONDS",
                "PRUNE_SYSTEM_MESSAGE_SECONDS",
                "PRUNE_INVITE_SECONDS",
                "CLOSE_GRACE_SECONDS",
                "PENDING_RESPONSE_BYTES",
            )
        }

    def tearDown(self):
        for name, value in self.constants.items():
            setattr(hub, name, value)
        self.store.disconnect()
        super().tearDown()

    def _invite(self, role="player", parent=None, ttl=3600):
        secret = token(16)
        moment = now()
        with self.store.lock:
            self.store.db.execute(
                "INSERT INTO invites (secret_hash, role, parent, name, issued_by, created_at,"
                " expires_at, used_at, used_by) VALUES (?, ?, ?, NULL, 'admin', ?, ?, NULL, NULL)",
                (secret_hash(secret), role, parent, moment, moment + ttl),
            )
            self.store.db.commit()
        return secret

    def _join(self, name, role="player", parent=None):
        result = self.store.join(self._invite(role, parent), name, "test")
        result["row"] = self.store._member(result["member_id"])
        return result

    def _orchestra(self):
        conductor = self._join("C", role="conductor")
        player_a = self._join("A", parent=conductor["member_id"])
        player_b = self._join("B", parent=conductor["member_id"])
        child_d = self._join("D", parent=player_b["member_id"])
        return conductor, player_a, player_b, child_d

    def _drain(self, member):
        """Take everything queued for this member out of the queued state."""
        rows = self.store.pending(member["row"], 0, 100)
        for row in rows:
            self.store.mark(member["row"], row["id"], "delivered")
        return [row["text"] for row in rows]

    def _send(self, sender, to, act="tell", task=None, text=None, message_id=None):
        payload = {
            "id": message_id,
            "to": to,
            "act": act,
            "re": None,
            "task": task,
            "need": "none",
            "refs": [],
            "text": text or _text(act),
        }
        return self.store.send(sender["row"], payload)

    def test_join_fills_the_public_row_and_the_conductor_slot(self):
        conductor = self._join("C", role="conductor")
        self.assertEqual(conductor["role"], "conductor")
        self.assertEqual(conductor["conductor_id"], conductor["member_id"])
        self.assertEqual(conductor["hub"]["endpoints"], ["https://127.0.0.1:1"])
        player = self._join("A", parent=conductor["member_id"])
        self.assertEqual(player["parent"], conductor["member_id"])
        rows = self.store.members()
        self.assertEqual([item["name"] for item in rows["members"]], ["C", "A"])
        self.assertEqual(rows["members"][0]["presence"], "connected")
        joined = self.store.pending(conductor["row"], 0, 50)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["from"]["id"], "sys")
        self.assertEqual(joined[0]["text"], f"presence {player['member_id']} A joined")

    def test_a_second_conductor_invite_is_rejected(self):
        self._join("C", role="conductor")
        with self.assertRaises(APIError) as caught:
            self._join("C2", role="conductor")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(str(caught.exception), "Orchestra already has a conductor")

    def test_a_used_or_expired_invite_is_rejected(self):
        secret = self._invite()
        self.store.join(secret, "A", "test")
        with self.assertRaises(APIError) as caught:
            self.store.join(secret, "B", "test")
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(APIError) as expired:
            self.store.join(self._invite(ttl=-10), "B", "test")
        self.assertEqual(expired.exception.status, 403)

    def test_aliases_resolve_relative_to_the_sender(self):
        conductor, player_a, player_b, child_d = self._orchestra()
        self.assertEqual(
            [item["id"] for item in self._send(player_a, ["conductor"])["recipients"]],
            [conductor["member_id"]],
        )
        self.assertEqual(
            [item["id"] for item in self._send(player_b, ["children"])["recipients"]],
            [child_d["member_id"]],
        )
        self.assertEqual(
            [item["id"] for item in self._send(child_d, ["parent"])["recipients"]],
            [player_b["member_id"]],
        )
        self.assertEqual(
            [item["id"] for item in self._send(player_a, ["siblings"])["recipients"]],
            [player_b["member_id"]],
        )
        self.assertEqual(
            sorted(item["id"] for item in self._send(conductor, ["all"])["recipients"]),
            sorted([player_a["member_id"], player_b["member_id"], child_d["member_id"]]),
        )
        self.assertEqual(
            [item["id"] for item in self._send(conductor, ["all", "children"])["recipients"]],
            [player_a["member_id"], player_b["member_id"], child_d["member_id"]],
        )

    def test_alias_failures_carry_the_contract_errors(self):
        conductor, player_a, _player_b, child_d = self._orchestra()
        with self.assertRaises(APIError) as leaf:
            self._send(child_d, ["children"])
        self.assertEqual(leaf.exception.status, 400)
        self.assertEqual(str(leaf.exception), "No recipients resolved")

        with self.assertRaises(APIError) as unknown:
            self._send(player_a, ["mb_deadbeef"])
        self.assertEqual(unknown.exception.status, 400)
        self.assertEqual(str(unknown.exception), "Unknown recipient: mb_deadbeef")

        with self.assertRaises(APIError) as itself:
            self._send(player_a, [player_a["member_id"]])
        self.assertEqual(itself.exception.status, 400)

        with self.assertRaises(APIError) as own_role:
            self._send(conductor, ["conductor"])
        self.assertEqual(own_role.exception.status, 400)
        self.assertEqual(str(own_role.exception), "You are the conductor")

        with self.assertRaises(APIError) as orphan:
            self._send(conductor, ["parent"])
        self.assertEqual(orphan.exception.status, 409)
        self.assertEqual(str(orphan.exception), "No parent")

    def test_send_is_idempotent_for_the_same_sender_and_body(self):
        conductor, player_a, _b, _d = self._orchestra()
        message_id = core.new_message_id()
        first = self._send(player_a, ["conductor"], message_id=message_id)
        self.assertEqual(first["state"], "queued")
        second = self._send(player_a, ["conductor"], message_id=message_id)
        self.assertEqual(second["id"], message_id)
        self.assertEqual(second["state"], "queued")
        self.assertEqual([item["id"] for item in second["recipients"]], [conductor["member_id"]])
        delivered = [
            item
            for item in self.store.pending(conductor["row"], 0, 50)
            if item["from"]["id"] == player_a["member_id"]
        ]
        self.assertEqual([item["id"] for item in delivered], [message_id])

        with self.assertRaises(APIError) as reused:
            self._send(player_a, ["conductor"], message_id=message_id, text=_text(body="other"))
        self.assertEqual(reused.exception.status, 409)
        with self.assertRaises(APIError) as stolen:
            self._send(conductor, ["all"], message_id=message_id)
        self.assertEqual(stolen.exception.status, 409)

    def test_a_duplicate_assign_task_is_rejected_and_writes_nothing(self):
        _c, player_a, player_b, _d = self._orchestra()
        conductor = self._orchestra_conductor()
        first = self._send(
            conductor, ["children"], act="assign", task="t_build", text=_text("assign")
        )
        self.assertEqual(len(first["recipients"]), 2)
        second_id = core.new_message_id()
        with self.assertRaises(APIError) as caught:
            self._send(
                conductor,
                [player_a["member_id"]],
                act="assign",
                task="t_build",
                text=_text("assign", "again"),
                message_id=second_id,
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(str(caught.exception), "Task already assigned: t_build")
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE id=?", (second_id,)
            ).fetchone()
        self.assertEqual(row["total"], 0)
        tasks = self.store.tasks()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task"], "t_build")
        self.assertEqual(
            sorted(tasks[0]["recipients"]),
            sorted([player_a["member_id"], player_b["member_id"]]),
        )
        self.assertIsNone(tasks[0]["latest"])
        self._send(player_b, ["parent"], act="done", task="t_build", text=_text("done"))
        latest = self.store.tasks()["tasks"][0]["latest"]
        self.assertEqual(latest["act"], "done")
        self.assertEqual(latest["sender"], player_b["member_id"])

    def _orchestra_conductor(self):
        row = self.store.db.execute(
            "SELECT * FROM members WHERE role='conductor'"
        ).fetchone()
        return {"row": dict(row), "member_id": row["id"]}

    def test_the_body_is_nulled_once_every_delivery_is_delivered(self):
        conductor, player_a, player_b, _d = self._orchestra()
        sent = self._send(conductor, ["children"], text=_text("tell", "review parser.py"))
        message_id = sent["id"]

        first = self.store.mark(player_a["row"], message_id, "delivered")
        self.assertEqual(first["state"], "delivered")
        self.assertIsNotNone(self._body(message_id))

        second = self.store.mark(player_b["row"], message_id, "handled")
        self.assertEqual(second["state"], "handled")
        self.assertIsNotNone(second["delivered_at"])
        self.assertIsNone(self._body(message_id))

        status = self.store.message_status(conductor["row"], message_id)
        self.assertEqual(status["state"], "delivered")
        self.assertEqual(
            sorted(item["state"] for item in status["recipients"]), ["delivered", "handled"]
        )
        with self.assertRaises(APIError) as outsider:
            self.store.mark(self._join("E")["row"], message_id, "delivered")
        self.assertEqual(outsider.exception.status, 403)
        with self.assertRaises(APIError) as missing:
            self.store.mark(player_a["row"], "m_" + "0" * 20, "delivered")
        self.assertEqual(missing.exception.status, 404)

    def _body(self, message_id):
        with self.store.lock:
            return self.store.db.execute(
                "SELECT body FROM messages WHERE id=?", (message_id,)
            ).fetchone()["body"]

    def test_leave_clears_the_conductor_and_tells_everyone(self):
        conductor, player_a, _b, _d = self._orchestra()
        self.store.pending(player_a["row"], 0, 50)
        result = self.store.leave(conductor["row"])
        self.assertTrue(result["ok"])
        self.assertIsNone(self.store.members()["conductor_id"])
        bodies = [item["text"] for item in self.store.pending(player_a["row"], 0, 50)]
        self.assertIn(f"presence {conductor['member_id']} C left", bodies)
        with self.assertRaises(APIError) as caught:
            self._send(player_a, ["conductor"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(str(caught.exception), "No conductor")

    def test_a_revoked_or_closed_membership_is_410(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        self.store.leave(player["row"])
        with self.assertRaises(APIError) as revoked:
            self.store.authenticate(f"Bearer {player['token']}")
        self.assertEqual(revoked.exception.status, 410)
        self.assertEqual(str(revoked.exception), "Membership revoked (left)")

        principal = self.store.authenticate(f"Bearer {conductor['token']}")
        self.assertEqual(principal["kind"], "member")
        self.assertEqual(self.store.authenticate(f"Bearer {self.admin_token}")["kind"], "admin")
        with self.assertRaises(APIError) as anonymous:
            self.store.authenticate(None)
        self.assertEqual(anonymous.exception.status, 401)
        with self.assertRaises(APIError) as wrong:
            self.store.authenticate("Bearer nope")
        self.assertEqual(wrong.exception.status, 401)

        self.store.close({"kind": "admin"})
        with self.assertRaises(APIError) as closed:
            self.store.authenticate(f"Bearer {conductor['token']}")
        self.assertEqual(closed.exception.status, 410)
        self.assertEqual(str(closed.exception), "Orchestra is closed")
        with self.assertRaises(APIError) as joining:
            self.store.join(self._invite(), "late", "test")
        self.assertEqual(joining.exception.status, 410)

    def test_a_closed_hub_records_who_has_seen_the_410(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        self.assertIsNone(self.store.close_shutdown_delay())

        closed_at = self.store.close({"kind": "admin"})["closed_at"]
        # Neither member has called since the close, so the hub must keep serving.
        self.assertGreater(self.store.close_shutdown_delay(), 0)

        before = self.store._member(player["member_id"])["last_seen_at"]
        self.assertLess(before, closed_at)
        with self.assertRaises(APIError) as gone:
            self.store.authenticate(f"Bearer {player['token']}")
        self.assertEqual(gone.exception.status, 410)
        self.assertEqual(str(gone.exception), "Orchestra is closed")
        after = self.store._member(player["member_id"])["last_seen_at"]
        self.assertGreater(after, before)
        self.assertGreaterEqual(after, closed_at)

        # The conductor is still owed the news.
        self.assertGreater(self.store.close_shutdown_delay(), 0)
        with self.assertRaises(APIError):
            self.store.authenticate(f"Bearer {conductor['token']}")
        self.assertEqual(self.store.close_shutdown_delay(), 0)

    def test_the_close_grace_window_bounds_the_wait(self):
        conductor = self._join("C", role="conductor")
        self._join("A", parent=conductor["member_id"])
        self.store.close({"kind": "admin"})
        self.assertGreater(self.store.close_shutdown_delay(), 0)
        hub.CLOSE_GRACE_SECONDS = 0
        # Nobody has seen the closure, but the grace window is spent.
        self.assertEqual(self.store.close_shutdown_delay(), 0)

    def test_a_revoked_member_never_holds_a_closed_hub_open(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        self.store.leave(player["row"])
        self.store.close({"kind": "admin"})
        with self.assertRaises(APIError):
            self.store.authenticate(f"Bearer {conductor['token']}")
        self.assertEqual(self.store.close_shutdown_delay(), 0)

    def test_a_410_on_a_parked_poll_counts_as_seeing_the_closure(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        closed_at = self.store.close({"kind": "admin"})["closed_at"]
        self.assertGreater(self.store.close_shutdown_delay(), 0)

        # A long poll authenticates before the close and learns of it from the
        # 410 the parked call raises, so that 410 is the member's last_seen.
        for member in (conductor, player):
            with self.assertRaises(APIError) as gone:
                self.store.pending(member["row"], 0, 50)
            self.assertEqual(gone.exception.status, 410)
            self.assertEqual(str(gone.exception), "Orchestra is closed")
            self.assertGreaterEqual(
                self.store._member(member["member_id"])["last_seen_at"], closed_at
            )
        self.assertEqual(self.store.close_shutdown_delay(), 0)

    def test_pending_caps_the_response_and_drains_over_repeated_polls(self):
        conductor, player_a, _b, _d = self._orchestra()
        self._drain(player_a)
        hub.PENDING_RESPONSE_BYTES = 300 * 1024
        queued = [
            self._send(
                conductor,
                [player_a["member_id"]],
                text=_text("tell", f"{index}" + "y" * (200 * 1024)),
            )["id"]
            for index in range(3)
        ]

        first = self.store.pending(player_a["row"], 0, 50)
        self.assertLess(len(first), len(queued))
        self.assertEqual([row["id"] for row in first], queued[: len(first)])
        self.assertLessEqual(
            len(json.dumps(first, separators=(",", ":")).encode("utf-8")),
            hub.PENDING_RESPONSE_BYTES,
        )

        drained = []
        for _ in range(len(queued)):
            rows = self.store.pending(player_a["row"], 0, 50)
            if not rows:
                break
            for row in rows:
                self.store.mark(player_a["row"], row["id"], "delivered")
                drained.append(row["id"])
        self.assertEqual(drained, queued)

    def test_one_oversized_row_is_still_delivered_on_its_own(self):
        conductor, player_a, _b, _d = self._orchestra()
        self._drain(player_a)
        hub.PENDING_RESPONSE_BYTES = 1024
        sent = self._send(
            conductor, [player_a["member_id"]], text=_text("tell", "z" * (200 * 1024))
        )
        rows = self.store.pending(player_a["row"], 0, 50)
        self.assertEqual([row["id"] for row in rows], [sent["id"]])

    def test_a_name_with_a_newline_cannot_forge_a_presence_line(self):
        conductor = self._join("C", role="conductor")
        evil = f"x\npresence {conductor['member_id']} c connected absent_since=1.0"
        forged = self.store.join(self._invite(), evil, "test")
        row = self.store._member(forged["member_id"])
        self.assertNotIn("\n", row["name"])
        self.assertEqual(
            row["name"],
            f"x presence {conductor['member_id']} c connected absent_since=1.0",
        )
        self.assertEqual(forged["name"], row["name"])

        bodies = self._drain(conductor)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0].splitlines(), [bodies[0]])
        self.assertTrue(bodies[0].endswith(" joined"))

    def test_names_are_sanitised_on_invite_and_hub_start(self):
        conductor = self._join("C", role="conductor")
        minted = self.store.invite(
            {"kind": "admin"}, {"role": "player", "name": "  A\tstray\r\nname\x07  "}
        )
        self.assertEqual(minted["role"], "player")
        with self.store.lock:
            stored = self.store.db.execute(
                "SELECT name FROM invites WHERE used_at IS NULL ORDER BY created_at DESC"
            ).fetchone()["name"]
        self.assertEqual(stored, "A stray name")

        joined = self.store.join(self._invite(), "   ", "test")
        self.assertEqual(self.store._member(joined["member_id"])["name"], "member")
        self.assertEqual(hub.sanitize_name("n" * 200), "n" * 80)
        self.assertEqual(
            hub.sanitize_name("hub\nname", fallback="orchestra"), "hub name"
        )
        self.assertEqual(hub.sanitize_name("\x00\x01", fallback="orchestra"), "orchestra")
        self.assertTrue(self.store._member(conductor["member_id"]))

    def test_the_connected_event_reaches_the_member_that_returned(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        self._drain(player)
        last_seen = self.store._member(player["member_id"])["last_seen_at"]
        self.store.sweep_presence(threshold=0)
        self._drain(player)

        self.store.authenticate(f"Bearer {player['token']}")
        own = self._drain(player)
        self.assertIn(
            f"presence {player['member_id']} A connected absent_since={last_seen}", own
        )
        everyone = self._drain(conductor)
        self.assertIn(
            f"presence {player['member_id']} A connected absent_since={last_seen}", everyone
        )

    def test_the_conductor_event_reaches_the_promoted_member(self):
        conductor, player_a, player_b, _d = self._orchestra()
        self._drain(player_a)
        self.store.set_conductor({"kind": "admin"}, player_a["member_id"])
        promoted = self._drain(player_a)
        self.assertIn(f"conductor {player_a['member_id']} A", promoted)
        self.assertIn(f"conductor {player_a['member_id']} A", self._drain(player_b))
        self.assertIn(f"conductor {player_a['member_id']} A", self._drain(conductor))

    def test_presence_flips_to_stale_and_back_with_events(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        last_seen = self.store._member(player["member_id"])["last_seen_at"]

        flipped = self.store.sweep_presence(threshold=0)
        self.assertEqual(sorted(flipped), sorted([conductor["member_id"], player["member_id"]]))
        self.assertEqual(self.store._member(player["member_id"])["presence"], "stale")
        stale = [item["text"] for item in self.store.pending(conductor["row"], 0, 50)]
        self.assertIn(f"presence {player['member_id']} A stale since={last_seen}", stale)

        principal = self.store.authenticate(f"Bearer {player['token']}")
        self.assertEqual(principal["presence"], "connected")
        bodies = [item["text"] for item in self.store.pending(conductor["row"], 0, 50)]
        connected = [item for item in bodies if "connected absent_since=" in item]
        self.assertEqual(
            connected, [f"presence {player['member_id']} A connected absent_since={last_seen}"]
        )
        self.assertEqual(self.store.sweep_presence(threshold=3600), [])

    def test_invite_permissions_and_parent_defaults(self):
        conductor = self._join("C", role="conductor")
        player = self._join("A", parent=conductor["member_id"])
        admin = {"kind": "admin"}

        minted = self.store.invite(admin, {"role": "player"})
        self.assertEqual(minted["parent"], conductor["member_id"])
        decoded = decode_invite(minted["invite"])
        self.assertEqual(decoded["role"], "player")
        self.assertEqual(decoded["orchestra_id"], "orc_test")
        self.assertEqual(decoded["hub"]["name"], "test hub")

        child = self.store.invite({"kind": "member", **player["row"]}, {"parent": "self"})
        self.assertEqual(child["parent"], player["member_id"])

        with self.assertRaises(APIError) as promoted:
            self.store.invite(
                {"kind": "member", **player["row"]}, {"role": "conductor", "parent": "self"}
            )
        self.assertEqual(promoted.exception.status, 403)
        with self.assertRaises(APIError) as adopted:
            self.store.invite(
                {"kind": "member", **player["row"]}, {"parent": conductor["member_id"]}
            )
        self.assertEqual(adopted.exception.status, 403)
        with self.assertRaises(APIError) as admin_self:
            self.store.invite(admin, {"parent": "self"})
        self.assertEqual(admin_self.exception.status, 400)
        with self.assertRaises(APIError) as bad_role:
            self.store.invite(admin, {"role": "hub"})
        self.assertEqual(bad_role.exception.status, 400)

        conductor_invite = self.store.invite(
            {"kind": "member", **conductor["row"]},
            {"role": "player", "parent": player["member_id"], "ttl": 5},
        )
        self.assertAlmostEqual(
            conductor_invite["expires_at"] - now(), hub.MIN_INVITE_TTL_SECONDS, delta=5
        )

    def test_conductor_handoff_and_kick(self):
        conductor, player_a, player_b, _d = self._orchestra()
        with self.assertRaises(APIError) as denied:
            self.store.set_conductor({"kind": "member", **player_a["row"]}, player_b["member_id"])
        self.assertEqual(denied.exception.status, 403)

        result = self.store.set_conductor({"kind": "admin"}, player_a["member_id"])
        self.assertEqual(result["conductor_id"], player_a["member_id"])
        self.assertEqual(self.store._member(conductor["member_id"])["role"], "player")
        self.assertEqual(self.store._member(player_a["member_id"])["role"], "conductor")
        handoff = [item["text"] for item in self.store.pending(player_b["row"], 0, 50)]
        self.assertIn(f"conductor {player_a['member_id']} A", handoff)

        promoted = {"kind": "member", **self.store._member(player_a["member_id"])}
        with self.assertRaises(APIError) as itself:
            self.store.kick(promoted, player_a["member_id"])
        self.assertEqual(itself.exception.status, 400)
        kicked = self.store.kick(promoted, player_b["member_id"])
        self.assertTrue(kicked["ok"])
        row = self.store._member(player_b["member_id"])
        self.assertEqual(row["revoked_reason"], "kicked")
        with self.assertRaises(APIError) as gone:
            self.store.kick({"kind": "admin"}, player_b["member_id"])
        self.assertEqual(gone.exception.status, 400)

    def test_status_counts_and_close_marks_hub_json(self):
        conductor, player_a, _b, _d = self._orchestra()
        self._send(player_a, ["conductor"])
        status = self.store.status({"kind": "member", **conductor["row"]})
        self.assertEqual(status["orchestra_id"], "orc_test")
        self.assertEqual(status["self"]["id"], conductor["member_id"])
        # only the message A sent; the three joined events are system mail
        self.assertEqual(status["queued_for_me"], 1)
        self.assertEqual(len(status["members"]), 4)
        sender_status = self.store.status({"kind": "member", **player_a["row"]})
        self.assertEqual(len(sender_status["sent"]), 1)
        self.assertEqual(sender_status["sent"][0]["state"], "queued")
        admin_status = self.store.status({"kind": "admin"})
        self.assertIsNone(admin_status["self"])
        self.assertEqual(admin_status["sent"], [])

        config_path = self.store.path.parent / "hub.json"
        atomic_write_json(config_path, {"orchestra_id": "orc_test", "closed_at": None})
        closed = self.store.close({"kind": "member", **conductor["row"]})
        self.assertTrue(closed["ok"])
        self.assertEqual(read_json(config_path)["closed_at"], closed["closed_at"])
        self.assertEqual(self.store.close({"kind": "admin"})["closed_at"], closed["closed_at"])

    def test_prune_drops_handled_messages_and_spent_invites(self):
        conductor, player_a, _b, _d = self._orchestra()
        kept = self._send(conductor, [player_a["member_id"]])["id"]
        handled = self._send(conductor, [player_a["member_id"]], text=_text("tell", "old"))["id"]
        self.store.mark(player_a["row"], handled, "handled")
        self._invite()

        hub.PRUNE_MESSAGE_SECONDS = 0
        hub.PRUNE_SYSTEM_MESSAGE_SECONDS = 0
        hub.PRUNE_INVITE_SECONDS = 0
        removed = self.store.prune()
        self.assertGreaterEqual(removed["messages"], 1)
        with self.store.lock:
            rows = {
                row["id"]
                for row in self.store.db.execute("SELECT id FROM messages").fetchall()
            }
            orphans = self.store.db.execute(
                "SELECT COUNT(*) AS total FROM deliveries WHERE message_id NOT IN"
                " (SELECT id FROM messages)"
            ).fetchone()["total"]
            invites = self.store.db.execute(
                "SELECT COUNT(*) AS total FROM invites"
            ).fetchone()["total"]
        self.assertIn(kept, rows)
        self.assertNotIn(handled, rows)
        self.assertEqual(orphans, 0)
        self.assertEqual(invites, 1)

    def test_send_rejects_bad_fields_and_oversized_bodies(self):
        conductor, player_a, _b, _d = self._orchestra()
        with self.assertRaises(APIError) as act:
            self._send(player_a, ["conductor"], act="shout")
        self.assertEqual(act.exception.status, 400)
        with self.assertRaises(APIError) as assign:
            self._send(player_a, ["conductor"], act="assign")
        self.assertEqual(assign.exception.status, 400)
        with self.assertRaises(APIError) as empty:
            self._send(player_a, ["conductor"], text="   ")
        self.assertEqual(empty.exception.status, 400)
        with self.assertRaises(APIError) as oversized:
            self._send(player_a, ["conductor"], text=_text("tell", "x" * core.MAX_MESSAGE_BYTES))
        self.assertEqual(oversized.exception.status, 413)
        with self.assertRaises(APIError) as identifier:
            self._send(player_a, ["conductor"], message_id="nope")
        self.assertEqual(identifier.exception.status, 400)
        # nothing was queued, and the three joined events never count
        self.assertEqual(self.store.status({"kind": "member", **conductor["row"]})["queued_for_me"], 0)

    def test_queued_for_me_ignores_system_events(self):
        conductor, player_a, _b, _d = self._orchestra()
        principal = {"kind": "member", **conductor["row"]}
        # the joins already queued three system events for the conductor
        with self.store.lock:
            queued_system = self.store.db.execute(
                "SELECT COUNT(*) AS total FROM deliveries"
                " JOIN messages ON messages.id = deliveries.message_id"
                " WHERE deliveries.recipient=? AND deliveries.state='queued'"
                " AND messages.sender='sys'",
                (conductor["member_id"],),
            ).fetchone()["total"]
        self.assertEqual(queued_system, 3)
        self.assertEqual(self.store.status(principal)["queued_for_me"], 0)

        message_id = self._send(player_a, ["conductor"])["id"]
        self.assertEqual(self.store.status(principal)["queued_for_me"], 1)
        self.store.mark(conductor["row"], message_id, "delivered")
        self.assertEqual(self.store.status(principal)["queued_for_me"], 0)


class HubLifecycleFileTests(TempHomeTests):
    def _write_hub(self, orchestra_id, *, created_at, closed_at=None, port=4443, bind="127.0.0.1"):
        config = {
            "protocol": 1,
            "orchestra_id": orchestra_id,
            "name": orchestra_id,
            "bind": bind,
            "port": port,
            "advertise": ["127.0.0.1"],
            "endpoints": [f"https://127.0.0.1:{port}"],
            "fingerprint": "cd" * 32,
            "admin_token": "admin-token",
            "created_at": created_at,
            "closed_at": closed_at,
        }
        atomic_write_json(core.hub_dir(orchestra_id) / "hub.json", config)
        return config

    def test_local_hubs_skips_bare_directories_and_closed_hubs(self):
        core.hub_dir("orc_leafonly")  # hub_dir() creates the leaf on access
        self.assertEqual(local_hubs(), [])
        self._write_hub("orc_old", created_at=100.0)
        self._write_hub("orc_new", created_at=200.0)
        self._write_hub("orc_gone", created_at=300.0, closed_at=310.0)
        self.assertEqual(
            [item["orchestra_id"] for item in local_hubs()], ["orc_new", "orc_old"]
        )

    def test_select_hub_needs_an_id_when_several_are_open(self):
        with self.assertRaises(OrchestraError):
            select_hub()
        self._write_hub("orc_one", created_at=100.0)
        self.assertEqual(select_hub()["orchestra_id"], "orc_one")
        self._write_hub("orc_two", created_at=200.0)
        with self.assertRaises(OrchestraError):
            select_hub()
        self.assertEqual(select_hub("orc_two")["orchestra_id"], "orc_two")
        core.hub_dir("orc_missing")
        with self.assertRaises(OrchestraError):
            select_hub("orc_missing")

    def test_admin_connection_dials_the_bind_address(self):
        self._write_hub("orc_one", created_at=100.0, port=7777)
        connection = admin_connection("orc_one")
        self.assertEqual(connection["endpoints"], ["https://127.0.0.1:7777"])
        self.assertEqual(connection["token"], "admin-token")
        self.assertEqual(connection["fingerprint"], "cd" * 32)

        self._write_hub("orc_two", created_at=200.0, port=7778, bind="127.0.0.2")
        self.assertEqual(
            admin_connection("orc_two")["endpoints"], ["https://127.0.0.2:7778"]
        )
        for wildcard in ("0.0.0.0", "::", ""):
            self._write_hub("orc_three", created_at=300.0, port=7779, bind=wildcard)
            self.assertEqual(
                admin_connection("orc_three")["endpoints"],
                ["https://127.0.0.1:7779"],
                f"bind {wildcard!r} must fall back to the loopback",
            )

    def test_hub_alive_needs_the_port_to_answer(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        self._write_hub("orc_one", created_at=100.0, port=port)
        # A live pid alone is not a live hub: after a reboot the number can
        # belong to anything, so nothing is listening on the port.
        atomic_write_json(
            core.hub_dir("orc_one") / "ready.json",
            {"pid": os.getpid(), "port": port, "started_at": now()},
        )
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(hub_alive("orc_one"))

        listener.listen(5)
        self.assertTrue(hub_alive("orc_one"))

    def test_hub_unit_is_text_and_installs_nothing(self):
        self._write_hub("orc_one", created_at=100.0)
        unit = hub_unit("orc_one")
        self.assertIn("orc_one", unit)
        self.assertIn("agent_orchestra", unit)
        if sys.platform == "darwin":
            self.assertIn("com.agent-orchestra.orc_one", unit)
            self.assertIn("<key>KeepAlive</key>", unit)
        else:
            self.assertIn("ExecStart=", unit)
            self.assertIn("Restart=on-failure", unit)
        self.assertFalse(hub_alive("orc_one"))

    def test_hub_unit_restarts_on_failure_only_and_pins_the_state_root(self):
        self._write_hub("orc_one", created_at=100.0)
        home = str(core.state_root().resolve())

        with mock.patch.object(sys, "platform", "darwin"):
            plist = hub_unit("orc_one")
        self.assertIn("<key>KeepAlive</key>\n", plist)
        self.assertIn("<key>SuccessfulExit</key><false/>", plist)
        self.assertNotIn("<key>KeepAlive</key><true/>", plist)
        self.assertIn(
            f"<key>AGENT_ORCHESTRA_HOME</key><string>{home}</string>", plist
        )

        with mock.patch.object(sys, "platform", "linux"):
            unit = hub_unit("orc_one")
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("Restart=always", unit)
        self.assertIn(f"Environment=AGENT_ORCHESTRA_HOME={home}", unit)


class HubServerTests(TempHomeTests):
    def setUp(self):
        super().setUp()
        self.previous_entry = hub._SPAWN_ENTRY
        hub._SPAWN_ENTRY = TEST_SPAWN_ENTRY
        self.pids = []
        self.hub_threads: list[threading.Thread] = []
        self.inprocess_hubs: list[str] = []
        self.constants = {
            name: getattr(hub, name)
            for name in ("CLOSE_GRACE_SECONDS", "CLOSE_CHECK_INTERVAL_SECONDS")
        }

    def tearDown(self):
        self._stop_inprocess_hubs()
        for name, value in self.constants.items():
            setattr(hub, name, value)
        hub._SPAWN_ENTRY = self.previous_entry
        _reap_spawned_processes(
            [process.pid for process in hub._BACKGROUND_PROCESSES], timeout=0
        )
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        super().tearDown()

    def _stop_inprocess_hubs(self) -> None:
        """An in-process hub can only be stopped by its own close watcher, so
        collapse the window and let it notice."""
        alive = [thread for thread in self.hub_threads if thread.is_alive()]
        if not alive:
            return
        hub.CLOSE_CHECK_INTERVAL_SECONDS = 0.05
        hub.CLOSE_GRACE_SECONDS = 0.0
        for orchestra_id in self.inprocess_hubs:
            try:
                api_request(admin_connection(orchestra_id), "POST", "/v1/close", {}, timeout=3)
            except OrchestraError:
                pass
        for thread in alive:
            thread.join(timeout=10)

    def _patch_hub(self, name: str, value) -> None:
        setattr(hub, name, value)

    def _serve_in_process(self, created: dict) -> threading.Thread:
        """Re-serve the hub in this process so the close constants a test
        shortens take effect; the spawned hub cannot see them."""
        orchestra_id = created["orchestra_id"]
        os.kill(created["hub_pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(created["hub_pid"]):
            time.sleep(0.05)
        thread = threading.Thread(target=hub.serve, args=(orchestra_id,), daemon=True)
        thread.start()
        self.hub_threads.append(thread)
        self.inprocess_hubs.append(orchestra_id)
        connection = admin_connection(orchestra_id)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                api_request(connection, "GET", "/v1/status", timeout=3)
                return thread
            except OrchestraError:
                time.sleep(0.1)
        self.fail("the in-process hub never became ready")
        return thread

    def _create(self, name="test hub"):
        result = create_hub(
            name=name, bind="127.0.0.1", advertise=["127.0.0.1"], invite_ttl=600
        )
        self.pids.append(result["hub_pid"])
        return result

    def _join(self, invite, name):
        decoded = decode_invite(invite)
        anonymous = {
            "endpoints": decoded["endpoints"],
            "fingerprint": decoded["fingerprint"],
        }
        joined = api_request(
            anonymous,
            "POST",
            "/v1/join",
            {"secret": decoded["secret"], "name": name, "provider": "test"},
            auth=False,
        )
        connection = {
            "endpoints": joined["hub"]["endpoints"],
            "fingerprint": joined["hub"]["fingerprint"],
            "token": joined["token"],
        }
        return joined, connection

    def test_join_invites_and_long_poll_over_https(self):
        created = self._create()
        self.assertTrue(hub_alive(created["orchestra_id"]))
        self.assertEqual(created["endpoints"], [f"https://127.0.0.1:{created['port']}"])

        conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        self.assertEqual(conductor["role"], "conductor")
        self.assertEqual(conductor["conductor_id"], conductor["member_id"])

        with self.assertRaises(APIError) as reused:
            self._join(created["conductor_invite"], "Impostor")
        self.assertEqual(reused.exception.status, 403)

        admin = admin_connection(created["orchestra_id"])
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player"})
        self.assertEqual(minted["parent"], conductor["member_id"])
        player, player_conn = self._join(minted["invite"], "Player A")

        with self.assertRaises(APIError) as promoted:
            api_request(
                player_conn, "POST", "/v1/invite", {"role": "conductor", "parent": "self"}
            )
        self.assertEqual(promoted.exception.status, 403)
        own_child = api_request(player_conn, "POST", "/v1/invite", {"parent": "self"})
        self.assertEqual(own_child["parent"], player["member_id"])

        box = {}

        def poll():
            started = time.monotonic()
            try:
                box["rows"] = api_request(
                    player_conn,
                    "GET",
                    "/v1/messages/pending",
                    query={"wait": 20, "limit": 50},
                    timeout=40,
                )["messages"]
            except Exception as exc:  # surfaced by the assertions below
                box["error"] = exc
            box["elapsed"] = time.monotonic() - started

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        time.sleep(0.5)
        sent = api_request(
            conductor_conn,
            "POST",
            "/v1/messages",
            {
                "id": core.new_message_id(),
                "to": ["children"],
                "act": "assign",
                "re": None,
                "task": "t_build",
                "need": "plan",
                "refs": ["parser.py"],
                "text": "ACT assign\nTO children\nTASK t_build\nNEED plan\n\nship it\n",
            },
        )
        poller.join(timeout=30)
        self.assertFalse(poller.is_alive())
        self.assertIsNone(box.get("error"))
        self.assertLess(box["elapsed"], 15)
        rows = box["rows"]
        self.assertEqual([item["id"] for item in rows], [sent["id"]])
        self.assertEqual(rows[0]["act"], "assign")
        self.assertEqual(rows[0]["task"], "t_build")
        self.assertEqual(rows[0]["need"], "plan")
        self.assertEqual(rows[0]["refs"], ["parser.py"])
        self.assertEqual(rows[0]["from"]["id"], conductor["member_id"])
        self.assertIn("ship it", rows[0]["text"])

        api_request(player_conn, "POST", f"/v1/messages/{sent['id']}/ack")
        handled = api_request(player_conn, "POST", f"/v1/messages/{sent['id']}/handled")
        self.assertEqual(handled["state"], "handled")
        status = api_request(conductor_conn, "GET", f"/v1/messages/{sent['id']}")
        self.assertEqual(status["recipients"][0]["state"], "handled")
        tasks = api_request(admin, "GET", "/v1/tasks")["tasks"]
        self.assertEqual([item["task"] for item in tasks], ["t_build"])

        with self.assertRaises(APIError) as duplicate:
            api_request(
                conductor_conn,
                "POST",
                "/v1/messages",
                {
                    "id": core.new_message_id(),
                    "to": ["children"],
                    "act": "assign",
                    "task": "t_build",
                    "need": "none",
                    "refs": [],
                    "text": "ACT assign\nTASK t_build\n\nagain\n",
                },
            )
        self.assertEqual(duplicate.exception.status, 409)
        self.assertEqual(str(duplicate.exception), "Task already assigned: t_build")

        with self.assertRaises(APIError) as unknown:
            api_request(admin, "GET", "/v1/nope")
        self.assertEqual(unknown.exception.status, 404)
        with self.assertRaises(APIError) as anonymous:
            api_request(
                {"endpoints": admin["endpoints"], "fingerprint": admin["fingerprint"], "token": "x"},
                "GET",
                "/v1/status",
            )
        self.assertEqual(anonymous.exception.status, 401)

        self.assertTrue(api_request(player_conn, "POST", "/v1/heartbeat")["ok"])
        left = api_request(player_conn, "POST", "/v1/leave")
        self.assertTrue(left["ok"])
        with self.assertRaises(APIError) as revoked:
            api_request(player_conn, "GET", "/v1/status")
        self.assertEqual(revoked.exception.status, 410)
        members = api_request(admin, "GET", "/v1/members")["members"]
        self.assertEqual(
            [item["revoked_reason"] for item in members if item["id"] == player["member_id"]],
            ["left"],
        )

    def test_close_serves_410_then_shuts_the_hub_process_down(self):
        created = self._create()
        conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        self.assertEqual(conductor["role"], "conductor")
        closed = api_request(conductor_conn, "POST", "/v1/close")
        self.assertTrue(closed["ok"])
        # The conductor has not called since the close, so the hub is still up.
        self.assertTrue(_pid_alive(created["hub_pid"]))
        self.assertFalse(hub_alive(created["orchestra_id"]))
        config = read_json(core.hub_dir(created["orchestra_id"]) / "hub.json")
        self.assertEqual(config["closed_at"], closed["closed_at"])

        with self.assertRaises(APIError) as gone:
            api_request(conductor_conn, "GET", "/v1/status", timeout=5)
        self.assertEqual(gone.exception.status, 410)
        self.assertEqual(str(gone.exception), "Orchestra is closed")

        # That 410 was the last member owed the news, so the watcher exits.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _pid_alive(created["hub_pid"]):
            time.sleep(0.1)
        self.assertFalse(_pid_alive(created["hub_pid"]))
        self.assertEqual(local_hubs(), [])
        with self.assertRaises(OrchestraError):
            ensure_hub(created["orchestra_id"])

    def test_a_closed_hub_waits_for_an_absent_member_until_the_grace_expires(self):
        created = self._create()
        self._patch_hub("CLOSE_CHECK_INTERVAL_SECONDS", 0.1)
        self._patch_hub("CLOSE_GRACE_SECONDS", 30.0)
        thread = self._serve_in_process(created)
        admin = admin_connection(created["orchestra_id"])
        _conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player", "parent": None})
        self._join(minted["invite"], "Absent")

        closed = api_request(conductor_conn, "POST", "/v1/close")
        with self.assertRaises(APIError) as gone:
            api_request(conductor_conn, "GET", "/v1/status", timeout=5)
        self.assertEqual(gone.exception.status, 410)

        # One member has been told, the other never calls again: keep serving.
        time.sleep(1.0)
        self.assertTrue(thread.is_alive())
        self.assertEqual(
            api_request(admin, "GET", "/v1/status", timeout=5)["closed_at"],
            closed["closed_at"],
        )

        self._patch_hub("CLOSE_GRACE_SECONDS", 1.0)
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())

    def test_a_bare_tcp_connection_never_stalls_the_accept_loop(self):
        created = self._create()
        admin = admin_connection(created["orchestra_id"])
        # No ClientHello ever arrives on these, so the handshake has to happen
        # on the handler thread or the hub freezes here.
        for _ in range(3):
            raw = socket.create_connection(("127.0.0.1", created["port"]), timeout=5)
            self.addCleanup(raw.close)

        started = time.monotonic()
        status = api_request(admin, "GET", "/v1/status", timeout=3)
        elapsed = time.monotonic() - started
        self.assertEqual(status["orchestra_id"], created["orchestra_id"])
        self.assertLess(elapsed, 3)
        self.assertTrue(hub_alive(created["orchestra_id"]))

    def test_a_large_multibyte_message_is_accepted_and_delivered(self):
        created = self._create()
        _conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        admin = admin_connection(created["orchestra_id"])
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player"})
        _player, player_conn = self._join(minted["invite"], "Player A")

        text = "ACT tell\n\n" + "漢" * 52000
        encoded = len(text.encode("utf-8"))
        self.assertGreater(encoded, 150 * 1024)
        self.assertLess(encoded, core.MAX_MESSAGE_BYTES)
        # JSON escapes every one of those to \uXXXX, so the request body is
        # about twice the text.
        sent = api_request(
            conductor_conn,
            "POST",
            "/v1/messages",
            {
                "id": core.new_message_id(),
                "to": ["children"],
                "act": "tell",
                "re": None,
                "task": None,
                "need": "none",
                "refs": [],
                "text": text,
            },
        )
        self.assertEqual(sent["state"], "queued")
        rows = api_request(
            player_conn, "GET", "/v1/messages/pending", query={"wait": 0, "limit": 50}
        )["messages"]
        self.assertEqual([item["id"] for item in rows], [sent["id"]])
        self.assertEqual(rows[0]["text"], text)

        with self.assertRaises(APIError) as oversized:
            api_request(
                conductor_conn,
                "POST",
                "/v1/messages",
                {
                    "id": core.new_message_id(),
                    "to": ["children"],
                    "act": "tell",
                    "need": "none",
                    "refs": [],
                    "text": "ACT tell\n\n" + "漢" * 90000,
                },
            )
        self.assertEqual(oversized.exception.status, 413)

    def test_the_conductor_endpoint_tells_the_promoted_member(self):
        created = self._create()
        conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        admin = admin_connection(created["orchestra_id"])
        minted = api_request(admin, "POST", "/v1/invite", {"role": "player"})
        player, player_conn = self._join(minted["invite"], "Player A")

        promoted = api_request(admin, "POST", "/v1/conductor", {"member_id": player["member_id"]})
        self.assertEqual(promoted["conductor_id"], player["member_id"])
        rows = api_request(
            player_conn, "GET", "/v1/messages/pending", query={"wait": 0, "limit": 50}
        )["messages"]
        self.assertIn(f"conductor {player['member_id']} Player A", [row["text"] for row in rows])
        self.assertEqual(
            api_request(player_conn, "GET", "/v1/status")["self"]["role"], "conductor"
        )
        self.assertEqual(
            api_request(conductor_conn, "GET", "/v1/status")["self"]["role"], "player"
        )

    def test_serve_exits_zero_on_a_closed_orchestra(self):
        created = self._create()
        orchestra_id = created["orchestra_id"]
        _conductor, conductor_conn = self._join(created["conductor_invite"], "Conductor")
        api_request(conductor_conn, "POST", "/v1/close")
        with self.assertRaises(APIError):
            api_request(conductor_conn, "GET", "/v1/status", timeout=5)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _pid_alive(created["hub_pid"]):
            time.sleep(0.1)
        self.assertFalse(_pid_alive(created["hub_pid"]))

        config_path = core.hub_dir(orchestra_id) / "hub.json"
        self.assertEqual(
            hub._serve_main(["serve", "--orchestra-id", orchestra_id]),
            0,
            "a closed hub.json must exit 0 rather than raise into a restart loop",
        )
        # The store is the second source of truth: a hub.json that lost its
        # closed_at must not bring a closed orchestra back either.
        config = read_json(config_path)
        config["closed_at"] = None
        atomic_write_json(config_path, config)
        self.assertEqual(hub._serve_main(["serve", "--orchestra-id", orchestra_id]), 0)
        with self.assertRaises(OrchestraError):
            api_request(admin_connection(orchestra_id), "GET", "/v1/status", timeout=3)

    def test_ensure_hub_restarts_a_killed_hub_on_the_same_port(self):
        created = self._create()
        conductor, _conn = self._join(created["conductor_invite"], "Conductor")
        os.kill(created["hub_pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(created["hub_pid"]):
            time.sleep(0.05)
        self.assertFalse(hub_alive(created["orchestra_id"]))

        admin = admin_connection(created["orchestra_id"])
        with self.assertRaises(OrchestraError):
            api_request(admin, "GET", "/v1/status", timeout=2)

        restarted = ensure_hub(created["orchestra_id"])
        self.pids.append(restarted)
        self.assertNotEqual(restarted, created["hub_pid"])
        self.assertEqual(ensure_hub(created["orchestra_id"]), restarted)
        ready = read_json(core.hub_dir(created["orchestra_id"]) / "ready.json")
        self.assertEqual(int(ready["port"]), created["port"])
        status = api_request(admin, "GET", "/v1/status")
        self.assertEqual(
            [item["id"] for item in status["members"]], [conductor["member_id"]]
        )
        self.assertEqual(status["conductor_id"], conductor["member_id"])


if __name__ == "__main__":
    unittest.main()
