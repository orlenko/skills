import http.client
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.presentation import dashboard_projection  # noqa: E402
from agent_observer.reviews import (  # noqa: E402
    PACKET_VERSION,
    prepare_review,
    submit_review,
)
from agent_observer.runtime import (  # noqa: E402
    service_status,
    start_services,
    stop_services,
)
from agent_observer.service import Observer, ObserverConfig  # noqa: E402


def append_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


class ReviewAndRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.config = ObserverConfig(
            root / "state",
            (root / "claude",),
            (root / "codex",),
        )

    def tearDown(self):
        stop_services(self.config)
        self.temp.cleanup()

    def test_interactive_review_validates_exact_packet_evidence(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        session = self.config.codex_roots[0] / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-03T10:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Choose SQLite or Postgres before I continue.",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:02Z",
                "payload": {"type": "user_message", "message": "What about backups?"},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self_review = prepare_review(
                observer,
                str(self.project),
                analyzer_provider="codex",
                exclude_session_id=sid,
            )
            self.assertEqual(self_review["packet"]["messages"], [])
            self.assertIn(
                "invoking analyzer session",
                self_review["packet"]["coverage"]["gaps"][0],
            )
            prepared = prepare_review(
                observer, str(self.project), analyzer_provider="codex"
            )
            packet = prepared["packet"]
            origin = next(
                message
                for message in packet["messages"]
                if message["role"] == "assistant"
            )
            draft = {
                "schema_version": PACKET_VERSION,
                "summary": "One earlier decision appears unanswered in the supplied range.",
                "items": [
                    {
                        "type": "decision",
                        "assessment": "no_later_handling_observed",
                        "title": "Choose the database",
                        "detail": "The later user message changes topic without choosing either option.",
                        "session_id": sid,
                        "message_ref": origin["message_ref"],
                        "evidence_excerpt": "Choose SQLite or Postgres",
                    }
                ],
                "limitations": [
                    "Only the bounded visible-message packet was reviewed."
                ],
            }
            path = Path(prepared["draft_path"])
            path.write_text(json.dumps(draft), encoding="utf-8")
            result = submit_review(observer, prepared["job_id"], path)
            self.assertEqual(result["items"], 1)
            projected = dashboard_projection(observer.status())
            review = projected["projects"][0]["review"]
            self.assertEqual(review["status"], "current")
            self.assertEqual(review["items"][0]["title"], "Choose the database")
            self.assertFalse(Path(prepared["packet_path"]).exists())

            rejected = prepare_review(
                observer, str(self.project), analyzer_provider="claude"
            )
            still_current = dashboard_projection(observer.status())["projects"][0][
                "review"
            ]
            self.assertEqual(still_current["job_id"], prepared["job_id"])
            bad_origin = next(
                message
                for message in rejected["packet"]["messages"]
                if message["role"] == "assistant"
            )
            draft["items"][0]["message_ref"] = bad_origin["message_ref"]
            draft["items"][0]["evidence_excerpt"] = "text absent from the packet"
            bad_path = Path(rejected["draft_path"])
            bad_path.write_text(json.dumps(draft), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "evidence is absent"):
                submit_review(observer, rejected["job_id"], bad_path)

    def test_review_targets_one_session_and_exclusion_survives_rescan(self):
        older = "11111111-1111-1111-1111-111111111111"
        newer = "22222222-2222-2222-2222-222222222222"
        for sid, timestamp, message in (
            (older, "2026-08-03T10:00:00Z", "This belongs to the older session."),
            (newer, "2026-08-03T10:01:00Z", "This belongs to the newer session."),
        ):
            session = self.config.codex_roots[0] / f"rollout-{sid}.jsonl"
            append_jsonl(
                session,
                {
                    "type": "session_meta",
                    "timestamp": timestamp,
                    "payload": {"id": sid, "cwd": str(self.project)},
                },
                {
                    "type": "event_msg",
                    "timestamp": timestamp,
                    "payload": {"type": "agent_message", "message": message},
                },
            )

        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            selected = prepare_review(
                observer,
                str(self.project),
                analyzer_provider="claude",
                target_session_id=older,
            )
            self.assertEqual(selected["packet"]["target_session"]["session_id"], older)
            self.assertEqual(
                {message["session_id"] for message in selected["packet"]["messages"]},
                {older},
            )

            observer.db.exclude_session("codex", newer, "test analyzer")
            observer.rescan_project(str(self.project))
            self.assertNotIn(
                newer, {row["session_id"] for row in observer.db.sources()}
            )
            self.assertTrue(observer.db.include_session("codex", newer))
            observer.rescan_project(str(self.project))
            self.assertIn(newer, {row["session_id"] for row in observer.db.sources()})

    def test_incomplete_or_tampered_packets_cannot_publish_negative_claims(self):
        sid = "33333333-3333-3333-3333-333333333333"
        session = self.config.codex_roots[0] / f"rollout-{sid}.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps({"padding": "x" * (4 * 1024 * 1024)}) + "\n",
            encoding="utf-8",
        )
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-03T10:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {
                    "type": "agent_message",
                    "message": "Please decide whether to ship today.",
                },
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            prepared = prepare_review(
                observer, str(self.project), analyzer_provider="claude"
            )
            packet = prepared["packet"]
            self.assertTrue(packet["coverage"]["gaps"])
            origin = packet["messages"][0]
            draft = {
                "schema_version": PACKET_VERSION,
                "summary": "The bounded tail contains a possible decision.",
                "items": [
                    {
                        "type": "decision",
                        "assessment": "no_later_handling_observed",
                        "title": "Choose whether to ship",
                        "detail": "No answer appears in the incomplete supplied range.",
                        "session_id": sid,
                        "message_ref": origin["message_ref"],
                        "evidence_excerpt": "decide whether to ship today",
                    }
                ],
                "limitations": ["The packet begins inside a bounded tail."],
            }
            draft_path = Path(prepared["draft_path"])
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must be indeterminate"):
                submit_review(observer, prepared["job_id"], draft_path)

            packet_path = Path(prepared["packet_path"])
            tampered = json.loads(packet_path.read_text(encoding="utf-8"))
            tampered["coverage"]["gaps"] = []
            packet_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "integrity check"):
                submit_review(observer, prepared["job_id"], draft_path)

    def test_sidecars_serve_only_authenticated_local_dashboard(self):
        services = start_services(self.config)
        self.assertTrue(services["server"]["running"])
        self.assertTrue(services["daemon"]["running"])
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        with opener.open(services["dashboard_url"], timeout=5) as response:
            self.assertIn(b"Agent Observer", response.read())
        second_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        with self.assertRaises(urllib.error.HTTPError) as reused_error:
            second_opener.open(services["dashboard_url"], timeout=5)
        self.assertEqual(reused_error.exception.code, 401)
        reused_error.exception.close()
        clean_url = services["dashboard_url"].split("?", 1)[0]
        with opener.open(clean_url + "api/status", timeout=5) as response:
            status = json.loads(response.read())
        self.assertEqual(status["schema_version"], "agent-observer-dashboard-v1")

        request = urllib.request.Request(
            clean_url + "api/projects",
            data=json.dumps({"path": str(self.project)}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": clean_url.rstrip("/"),
                "X-Agent-Observer": "1",
            },
        )
        with opener.open(request, timeout=5) as response:
            added = json.loads(response.read())
        self.assertEqual(len(added["projects"]), 1)

        rejected = urllib.request.Request(
            clean_url + "api/rescan",
            data=json.dumps({"project": added["projects"][0]["project_id"]}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": "http://attacker.invalid",
                "X-Agent-Observer": "1",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected_error:
            opener.open(rejected, timeout=5)
        self.assertEqual(rejected_error.exception.code, 403)
        rejected_error.exception.close()

        remove = urllib.request.Request(
            clean_url + "api/projects/remove",
            data=json.dumps({"project": added["projects"][0]["project_id"]}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": clean_url.rstrip("/"),
                "X-Agent-Observer": "1",
            },
        )
        with opener.open(remove, timeout=5) as response:
            removed = json.loads(response.read())
        self.assertEqual(removed["projects"], [])

        port = int(services["server"]["port"])
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/api/status", headers={"Host": "attacker.invalid"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

        stop_services(self.config)
        stopped = service_status(self.config)
        self.assertFalse(stopped["server"]["running"])
        self.assertFalse(stopped["daemon"]["running"])


if __name__ == "__main__":
    unittest.main()
