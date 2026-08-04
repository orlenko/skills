import http.client
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.presentation import dashboard_projection  # noqa: E402
from agent_observer.reviews import (  # noqa: E402
    PACKET_VERSION,
    next_review,
    prepare_review,
    submit_review,
)
from agent_observer.runtime import (  # noqa: E402
    service_status,
    start_analyzer_service,
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

    def test_supervisor_takeover_fences_submission_and_resumes_cursor(self):
        sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        session = self.config.codex_roots[0] / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-04T10:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:01Z",
                "payload": {"type": "user_message", "message": "Draft the first option."},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:02Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "First proposal: " + ("alpha details " * 55),
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:03Z",
                "payload": {"type": "user_message", "message": "Now compare the second option."},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:04Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Second proposal: " + ("beta details " * 55),
                },
            },
        )
        old = time.time() - 700
        os.utime(session, (old, old))
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            first_lease = observer.db.acquire_supervisor(
                provider="claude",
                model=None,
                session_id="observer-one",
                allow_cross_provider=True,
            )
            first = next_review(observer, first_lease["lease_token"])
            self.assertEqual(first["state"], "work")
            first_job = first["review"]["job_id"]

            second_lease = observer.db.acquire_supervisor(
                provider="claude",
                model=None,
                session_id="observer-two",
                allow_cross_provider=True,
            )
            self.assertGreater(second_lease["epoch"], first_lease["epoch"])
            second = next_review(observer, second_lease["lease_token"])
            self.assertEqual(second["state"], "work")
            self.assertEqual(second["review"]["job_id"], first_job)

            draft_path = Path(second["review"]["draft_path"])
            draft_path.write_text(
                json.dumps(
                    {
                        "schema_version": PACKET_VERSION,
                        "summary": "No prominent loose end selected.",
                        "items": [],
                        "limitations": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "superseded"):
                submit_review(
                    observer,
                    first_job,
                    draft_path,
                    lease_token=first_lease["lease_token"],
                )
            submit_review(
                observer,
                first_job,
                draft_path,
                lease_token=second_lease["lease_token"],
            )
            waiting = next_review(observer, second_lease["lease_token"])
            self.assertEqual(waiting["state"], "waiting")

            append_jsonl(
                session,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:05Z",
                    "payload": {"type": "user_message", "message": "Use alpha."},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:06Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "Implementation pass one: " + ("work details " * 55),
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:07Z",
                    "payload": {"type": "user_message", "message": "Please verify the result."},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:08Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "Verification result: " + ("evidence details " * 55),
                    },
                },
            )
            os.utime(session, (old, old))
            observer.scan()
            later = next_review(observer, second_lease["lease_token"])
            self.assertEqual(later["state"], "work")
            self.assertNotEqual(later["review"]["job_id"], first_job)

    def test_analyzer_is_idle_without_volume_and_runs_only_one_hourly_batch(self):
        sid = "abababab-1111-2222-3333-cdcdcdcdcdcd"
        session = self.config.codex_roots[0] / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            {
                "type": "session_meta",
                "timestamp": "2026-08-04T10:00:00Z",
                "payload": {"id": sid, "cwd": str(self.project)},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:01Z",
                "payload": {"type": "user_message", "message": "One question."},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-04T10:00:02Z",
                "payload": {"type": "agent_message", "message": "One short answer."},
            },
        )
        old = time.time() - 700
        os.utime(session, (old, old))
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))

        root = Path(self.temp.name)
        marker = root / "model-invocations.txt"
        fake = root / "fake-codex"
        fake.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output-last-message') + 1])
out.write_text(json.dumps({'schema_version': 'observer-interactive-review-v1', 'summary': 'No loose ends.', 'items': [], 'limitations': []}))
marker = pathlib.Path(os.environ['OBSERVER_TEST_MARKER'])
with marker.open('a') as handle:
    handle.write('called\\n')
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        environment = {
            "AGENT_OBSERVER_CODEX_COMMAND": str(fake),
            "OBSERVER_TEST_MARKER": str(marker),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            start_analyzer_service(
                self.config,
                provider="codex",
                model=None,
                allow_cross_provider=True,
            )
            time.sleep(0.5)
            self.assertFalse(marker.exists(), "idle source must not invoke the model")
            stop_services(self.config)

            append_jsonl(
                session,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:03Z",
                    "payload": {"type": "user_message", "message": "First follow-up."},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:04Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "First substantial response: " + ("context " * 90),
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:05Z",
                    "payload": {"type": "user_message", "message": "Second follow-up."},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T10:00:06Z",
                    "payload": {
                        "type": "agent_message",
                        "message": "Second substantial response: " + ("evidence " * 90),
                    },
                },
            )
            os.utime(session, (old, old))
            with Observer(self.config) as observer:
                observer.scan()
            start_analyzer_service(
                self.config,
                provider="codex",
                model=None,
                allow_cross_provider=True,
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            time.sleep(0.5)
            self.assertEqual(marker.read_text(encoding="utf-8").splitlines(), ["called"])

    def test_service_start_replaces_an_incompatible_running_sidecar(self):
        first = start_services(self.config)
        first_pid = first["server"]["pid"]
        info_path = self.config.state_dir / "runtime" / "server.json"
        stale = json.loads(info_path.read_text(encoding="utf-8"))
        stale["runtime_root"] = "/old/agent-observer/plugin"
        info_path.write_text(json.dumps(stale), encoding="utf-8")

        second = start_services(self.config)
        self.assertTrue(second["server"]["compatible"])
        self.assertNotEqual(second["server"]["pid"], first_pid)

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

    def test_clipped_start_is_disclosed_and_tampered_packets_are_rejected(self):
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
            self.assertFalse(packet["coverage"]["negative_assessment_blocked"])
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
            submit_review(observer, prepared["job_id"], draft_path)

            tampered_review = prepare_review(
                observer, str(self.project), analyzer_provider="claude"
            )
            packet_path = Path(tampered_review["packet_path"])
            tampered = json.loads(packet_path.read_text(encoding="utf-8"))
            tampered["coverage"]["gaps"] = []
            packet_path.write_text(json.dumps(tampered), encoding="utf-8")
            Path(tampered_review["draft_path"]).write_text(
                json.dumps(draft), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "integrity check"):
                submit_review(
                    observer,
                    tampered_review["job_id"],
                    tampered_review["draft_path"],
                )

    def test_sidecars_serve_only_authenticated_local_dashboard(self):
        sid = "44444444-5555-6666-7777-888888888888"
        append_jsonl(
            self.config.claude_roots[0] / "recent-project" / f"{sid}.jsonl",
            {
                "type": "custom-title",
                "sessionId": sid,
                "cwd": str(self.project),
                "timestamp": "2026-08-04T12:00:00Z",
                "customTitle": "Enrollment card",
            },
            {
                "type": "user",
                "sessionId": sid,
                "uuid": "catalog-user",
                "cwd": str(self.project),
                "timestamp": "2026-08-04T12:00:01Z",
                "message": {"content": "Investigate recent project enrollment"},
            },
            {
                "type": "assistant",
                "sessionId": sid,
                "uuid": "catalog-assistant",
                "cwd": str(self.project),
                "timestamp": "2026-08-04T12:00:02Z",
                "message": {
                    "content": "The enrollment proposal is ready.",
                    "stop_reason": "end_turn",
                },
            },
            {
                "type": "system",
                "subtype": "turn_duration",
                "sessionId": sid,
                "uuid": "catalog-turn",
                "cwd": str(self.project),
                "timestamp": "2026-08-04T12:00:03Z",
            },
        )
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
        self.assertEqual(status["analyzer"]["state"], "detached")

        with opener.open(clean_url + "api/project-candidates", timeout=5) as response:
            candidates = json.loads(response.read())["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["resolved_path"], str(self.project.resolve()))
        self.assertEqual(candidates[0]["session"]["title"], "Enrollment card")
        self.assertIn("recent project enrollment", candidates[0]["session"]["topic"])

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
        self.assertEqual(added["view_counts"]["needs_a_look"], 1)

        dismiss_attention = urllib.request.Request(
            clean_url + "api/projects/dismiss-attention",
            data=json.dumps(
                {"project": added["projects"][0]["project_id"]}
            ).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": clean_url.rstrip("/"),
                "X-Agent-Observer": "1",
            },
        )
        with opener.open(dismiss_attention, timeout=5) as response:
            dismissed = json.loads(response.read())
        self.assertEqual(dismissed["view_counts"]["needs_a_look"], 0)

        with opener.open(clean_url + "api/project-candidates", timeout=5) as response:
            self.assertEqual(json.loads(response.read())["candidates"], [])

        duplicate = urllib.request.Request(
            clean_url + "api/projects",
            data=json.dumps({"path": f"  {self.project}  "}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Origin": clean_url.rstrip("/"),
                "X-Agent-Observer": "1",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as duplicate_error:
            opener.open(duplicate, timeout=5)
        self.assertEqual(duplicate_error.exception.code, 400)
        duplicate_body = json.loads(duplicate_error.exception.read())
        self.assertIn("already watched", duplicate_body["error"])
        self.assertNotIn("does not exist", duplicate_body["error"])
        duplicate_error.exception.close()

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
