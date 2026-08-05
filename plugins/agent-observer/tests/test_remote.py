import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.presentation import dashboard_projection  # noqa: E402
from agent_observer.reviews import PACKET_VERSION, prepare_review, submit_review  # noqa: E402
from agent_observer.remote import (  # noqa: E402
    RemoteError,
    accept_invite,
    accept_listener_invite,
    claim_uploader,
    decode_invite,
    enable_home_remote,
    enable_remote_listener,
    encode_invite,
    issue_invite,
    issue_listener_invite,
    load_connection,
    load_pull_connections,
    pull_snapshot,
    push_snapshot,
    remote_connection_status,
    remote_pull_status,
    revoke_pull_connection,
    snapshot_projection,
)
from agent_observer.runtime import (  # noqa: E402
    start_remote_services,
    start_services,
    stop_services,
)
from agent_observer.service import Observer, ObserverConfig  # noqa: E402


def append_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


class RemoteObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.home = ObserverConfig(
            root / "home-state",
            (root / "home-claude",),
            (root / "home-codex",),
        )
        self.remote = ObserverConfig(
            root / "remote-state",
            (root / "remote-claude",),
            (root / "remote-codex",),
        )
        self.project = root / "remote-project"
        self.project.mkdir()

    def tearDown(self):
        stop_services(self.remote)
        stop_services(self.home)
        self.temp.cleanup()

    def _enroll(self) -> str:
        home = enable_home_remote(
            self.home,
            bind="127.0.0.1",
            advertise=["127.0.0.1"],
        )
        services = start_services(self.home)
        self.assertTrue(services["ingest"]["running"])
        with Observer(self.home) as observer:
            enrollment = issue_invite(observer, home)
        accept_invite(self.remote, enrollment["invite"], provider="codex")
        claim_uploader(self.remote)
        return enrollment["invite"]

    def test_enrollment_is_single_use_and_tls_pinned(self):
        invite = self._enroll()
        second = ObserverConfig(
            Path(self.temp.name) / "second-state",
            (Path(self.temp.name) / "second-claude",),
            (Path(self.temp.name) / "second-codex",),
        )
        with self.assertRaisesRegex(RemoteError, "already used"):
            accept_invite(second, invite, provider="claude")

        decoded = decode_invite(invite)
        decoded["fingerprint"] = "00" * 32
        decoded.pop("v", None)
        tampered = encode_invite(decoded)
        with self.assertRaisesRegex(RemoteError, "fingerprint"):
            accept_invite(second, tampered, provider="claude")

    def test_remote_snapshot_appears_with_host_and_local_dismissal(self):
        self._enroll()
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        session = self.remote.codex_roots[0] / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-04T12:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T12:00:01Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Remote work is ready for review.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T12:00:02Z",
                "payload": {"type": "task_complete"},
            },
        )
        with Observer(self.remote) as observer:
            observer.add_project(str(self.project))
            prepared = prepare_review(
                observer,
                str(self.project),
                analyzer_provider="codex",
            )
            origin = next(
                message
                for message in prepared["packet"]["messages"]
                if message["role"] == "assistant"
            )
            draft = {
                "schema_version": PACKET_VERSION,
                "summary": "The rollout choice still needs a response.",
                "items": [
                    {
                        "type": "decision",
                        "assessment": "no_later_handling_observed",
                        "title": "Choose rollout color",
                        "detail": "No later supplied message selects blue or green.",
                        "session_id": sid,
                        "message_ref": origin["message_ref"],
                        "evidence_excerpt": origin["text"],
                    }
                ],
                "limitations": ["Only the bounded remote packet was reviewed."],
            }
            draft_path = Path(prepared["draft_path"])
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            submit_review(observer, prepared["job_id"], draft_path)
            exported = snapshot_projection(observer)
            self.assertNotIn("path", exported["projects"][0]["sources"][0])
        remote_services = start_remote_services(self.remote)
        self.assertTrue(remote_services["daemon"]["running"])
        self.assertFalse(remote_services["server"]["running"])
        uploaded = push_snapshot(self.remote)
        self.assertEqual(uploaded["state"], "uploaded")
        self.assertEqual(remote_connection_status(self.remote)["state"], "connected")

        with Observer(self.home) as observer:
            projected = dashboard_projection(observer.status())
            project = projected["projects"][0]
            self.assertEqual(project["origin"], "remote")
            self.assertEqual(project["node"]["transport_state"], "connected")
            self.assertTrue(project["project_id"].startswith("remote:node_"))
            self.assertIn("needs_a_look", project["views"])
            self.assertEqual(project["review"]["items"][0]["title"], "Choose rollout color")
            self.assertTrue(observer.dismiss_project_attention(project["project_id"]))
            dismissed = dashboard_projection(observer.status())["projects"][0]
            self.assertNotIn("needs_a_look", dismissed["views"])

    def test_new_uploader_epoch_fences_a_cloned_connection(self):
        self._enroll()
        stale = dict(load_connection(self.remote) or {})
        newer = claim_uploader(self.remote)
        self.assertGreater(newer["upload_epoch"], stale["upload_epoch"])
        stale_path = Path(self.temp.name) / "stale-state" / "remote" / "connection.json"
        stale_path.parent.mkdir(parents=True)
        stale_path.write_text(json.dumps(stale), encoding="utf-8")
        stale_config = ObserverConfig(
            Path(self.temp.name) / "stale-state",
            (Path(self.temp.name) / "stale-claude",),
            (Path(self.temp.name) / "stale-codex",),
        )
        result = push_snapshot(stale_config)
        self.assertEqual(result["state"], "disconnected")
        self.assertIn("superseded", result["error"])

    def test_dashboard_pulls_from_a_listening_full_observer_peer(self):
        sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        session = self.remote.codex_roots[0] / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-04T15:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T15:00:01Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "The listening peer finished its local analysis.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T15:00:02Z",
                "payload": {"type": "task_complete"},
            },
        )
        with Observer(self.remote) as observer:
            observer.add_project(str(self.project))
            listener = enable_remote_listener(
                self.remote,
                bind="127.0.0.1",
                advertise=["127.0.0.1"],
                display_name="ubuntu-observer",
            )
            enrollment = issue_listener_invite(observer, listener)
        remote_services = start_remote_services(self.remote)
        self.assertTrue(remote_services["listener"]["running"])
        self.assertFalse(remote_services["server"]["running"])

        connection = accept_listener_invite(
            self.home, enrollment["invite"], provider="codex"
        )
        pulled = pull_snapshot(self.home, connection)
        self.assertEqual(pulled["state"], "received")
        self.assertEqual(remote_pull_status(self.home)[0]["state"], "connected")

        with Observer(self.home) as observer:
            projected = dashboard_projection(observer.status())
        project = projected["projects"][0]
        self.assertEqual(project["origin"], "remote")
        self.assertEqual(project["node"]["display_name"], "ubuntu-observer")
        self.assertEqual(project["sessions"][0]["provider"], "codex")

        stop_services(self.remote)
        restarted = start_remote_services(self.remote)
        self.assertTrue(restarted["listener"]["running"])
        time.sleep(0.21)  # Respect the dashboard's 200 ms snapshot flood limit.
        resumed = pull_snapshot(self.home, load_pull_connections(self.home)[0])
        self.assertEqual(resumed["state"], "received", resumed)
        self.assertGreater(resumed["revision"], pulled["revision"])

        second_home = ObserverConfig(
            Path(self.temp.name) / "second-home-state",
            (Path(self.temp.name) / "second-home-claude",),
            (Path(self.temp.name) / "second-home-codex",),
        )
        with self.assertRaisesRegex(RemoteError, "already used"):
            accept_listener_invite(
                second_home, enrollment["invite"], provider="claude"
            )

        revoked = revoke_pull_connection(self.home, connection["node_id"])
        self.assertTrue(revoked["connection_removed"])
        self.assertEqual(load_pull_connections(self.home), [])
        with Observer(self.home) as observer:
            observer.db.revoke_remote_node(connection["node_id"])
            raw = observer.status()
            projected = dashboard_projection(raw)
        self.assertEqual(raw["projects"], [])
        self.assertEqual(projected["projects"], [])
        self.assertEqual(projected["view_counts"]["observer_issues"], 0)
        self.assertEqual(projected["remote_nodes"][0]["transport_state"], "revoked")


if __name__ == "__main__":
    unittest.main()
