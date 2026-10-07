from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_orchestra import podium  # noqa: E402
from agent_orchestra.core import OrchestraError  # noqa: E402


PLAN = {
    "summary": "QC first; chores in a smaller lane.",
    "members": [
        {
            "id": "mb_conductor",
            "name": "Maestro",
            "role": "Conductor",
            "task": "Coordinate priorities",
            "status": "working",
            "reported_at": 1791404638.2,
            "result": "Ledger reconciled.",
            "next": "Review plans.",
            "links": [{"label": "ops #1", "url": "https://example.com/pr/1"}],
            "queue": [],
        },
        {
            "id": "mb_player",
            "name": "Trumpet",
            "role": "Player",
            "task": "Repair the reported blocker",
            "status": "working",
            "reported_at": 1791404000,
            "queue": [
                {"item": "#4123", "activity": "QC / APRS", "status": "in_progress",
                 "stage": "One repair required", "next": "Fix and verify", "blocker": "",
                 "eta": "Unknown", "url": "https://example.com/pr/4123"},
                {"item": "#4124", "activity": "Review", "status": "blocked",
                 "stage": "Waiting on a decision", "blocker": "Needs conductor call", "eta": "Unknown"},
            ],
        },
    ],
    "notes": [{"title": "Explicit queues", "body": "No inferred ETAs."}],
}


class ValidateTest(unittest.TestCase):
    def broken(self, mutate) -> str:
        plan = copy.deepcopy(PLAN)
        mutate(plan)
        with self.assertRaises(OrchestraError) as caught:
            podium.validate(plan)
        return str(caught.exception)

    def test_valid_plan_counts_queue_items(self) -> None:
        self.assertEqual(podium.validate(copy.deepcopy(PLAN)), 2)

    def test_report_needs_members_and_summary(self) -> None:
        self.assertIn("members", self.broken(lambda p: p.pop("members")))
        self.assertIn("summary", self.broken(lambda p: p.update(summary="  ")))

    def test_member_fields_and_unique_ids(self) -> None:
        self.assertIn("task", self.broken(lambda p: p["members"][0].pop("task")))
        self.assertIn("duplicate", self.broken(lambda p: p["members"][1].update(id="mb_conductor")))

    def test_evidence_time_is_required_and_real(self) -> None:
        for bad in (None, True, 0, -5, "1791404000", float("nan"), float("inf")):
            with self.subTest(reported_at=bad):
                self.assertIn("reported_at", self.broken(lambda p: p["members"][0].update(reported_at=bad)))

    def test_queue_must_be_explicit(self) -> None:
        self.assertIn("explicit queue", self.broken(lambda p: p["members"][0].pop("queue")))

    def test_queue_item_needs_activity_stage_eta_and_known_status(self) -> None:
        for field in ("item", "activity", "stage", "eta"):
            with self.subTest(field=field):
                self.assertIn(field, self.broken(lambda p: p["members"][1]["queue"][0].pop(field)))
        self.assertIn("status", self.broken(lambda p: p["members"][1]["queue"][0].update(status="done")))

    def test_blocked_item_names_its_blocker(self) -> None:
        self.assertIn("reason", self.broken(lambda p: p["members"][1]["queue"][1].update(blocker=" ")))

    def test_links_must_be_http(self) -> None:
        self.assertIn("http", self.broken(
            lambda p: p["members"][1]["queue"][0].update(url="javascript:alert(1)")))
        self.assertIn("links", self.broken(
            lambda p: p["members"][0]["links"].append({"label": "x", "url": "file:///etc/passwd"})))

    def test_deployments_and_the_legacy_heap_block(self) -> None:
        plan = copy.deepcopy(PLAN)
        plan["heap"] = {"summary": "33-part pile is running.", "code": "c58432c615"}
        self.assertEqual(podium.validate(plan), 2)
        plan["deployments"] = [{"name": "Staging", "summary": "Running."}]
        self.assertEqual(podium.validate(plan), 2)
        self.assertIn("summary", self.broken(lambda p: p.update(deployments=[{"name": "Staging"}])))
        self.assertIn("summary", self.broken(lambda p: p.update(heap={"code": "abc"})))
        self.assertIn("list", self.broken(lambda p: p.update(deployments={"summary": "x"})))

    def test_notes_need_title_and_body(self) -> None:
        self.assertIn("note", self.broken(lambda p: p["notes"].append({"title": "x"})))


class PublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.plan = self.root / "work-plan.json"
        self.plan.write_text(json.dumps(PLAN))
        self.out = self.root / "data"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_publish_writes_a_private_status_file_and_keeps_evidence_time(self) -> None:
        result = podium.publish(self.plan, self.out)
        written = self.out / podium.STATUS_FILE
        self.assertEqual(result, {"items": 2, "members": 2, "published": str(written)})
        self.assertEqual(os.stat(written).st_mode & 0o777, 0o600)
        report = json.loads(written.read_text())
        self.assertIn("updated_at", report)
        self.assertEqual(report["members"][0]["reported_at"], 1791404638.2)
        self.assertEqual([m["name"] for m in report["members"]], ["Maestro", "Trumpet"])
        self.assertEqual([p.name for p in self.out.iterdir()], [podium.STATUS_FILE])

    def test_check_writes_nothing(self) -> None:
        result = podium.publish(self.plan, self.out, check=True)
        self.assertIsNone(result["published"])
        self.assertFalse(self.out.exists())

    def test_a_rejected_plan_leaves_the_live_report_alone(self) -> None:
        podium.publish(self.plan, self.out)
        live = (self.out / podium.STATUS_FILE).read_text()
        broken = copy.deepcopy(PLAN)
        broken["members"][1]["queue"][1]["blocker"] = ""
        self.plan.write_text(json.dumps(broken))
        with self.assertRaises(OrchestraError):
            podium.publish(self.plan, self.out)
        self.assertEqual((self.out / podium.STATUS_FILE).read_text(), live)

    def test_non_json_constants_are_refused(self) -> None:
        self.plan.write_text(json.dumps(PLAN).replace('"Ledger reconciled."', "NaN"))
        with self.assertRaises(OrchestraError) as caught:
            podium.publish(self.plan, self.out)
        self.assertIn("NaN", str(caught.exception))

    def test_unreadable_plan_is_an_orchestra_error(self) -> None:
        with self.assertRaises(OrchestraError):
            podium.publish(self.root / "missing.json", self.out)


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        (self.data / podium.STATUS_FILE).write_text('{"summary": "ok"}')
        (self.data / "work-plan.json").write_text('{"private": true}')
        self.httpd = podium.server(self.data, 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def get(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_binds_loopback_only(self) -> None:
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_serves_the_bundled_page_without_caching(self) -> None:
        for path in ("/", "/index.html", "/?layout=x#work-status"):
            with self.subTest(path=path):
                status, headers, body = self.get(path)
                self.assertEqual(status, 200)
                self.assertIn(b"<title>The Podium</title>", body)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn("no-store", headers["Cache-Control"])
                self.assertNotIn("Last-Modified", headers)

    def test_serves_the_published_report(self) -> None:
        status, headers, body = self.get(f"/{podium.STATUS_FILE}?t=123")
        self.assertEqual((status, json.loads(body)), (200, {"summary": "ok"}))
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_head_sends_no_body(self) -> None:
        status, headers, body = self.get("/", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["Content-Length"]), 0)

    def test_nothing_else_in_the_directory_is_served(self) -> None:
        for path in ("/work-plan.json", "/state.json", "/../podium.py", "/%2e%2e/etc/passwd", "/logs/"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 404)


class HubViewTest(unittest.TestCase):
    """orchestra.json comes from the monitor's snapshots, never from the hub."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = self.tmp.name
        from agent_orchestra import member as member_api

        self.member_api = member_api
        self.member = {"member_id": "mb_conductor", "orchestra_id": "orc_1", "conductor_id": "mb_conductor"}
        member_api.save_member(self.member)
        self.at = time.time()

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("AGENT_ORCHESTRA_HOME", None)
        else:
            os.environ["AGENT_ORCHESTRA_HOME"] = self.previous_home
        self.tmp.cleanup()

    def roster(self) -> dict:
        return {
            "conductor_id": "mb_conductor",
            "members": [
                {"id": "mb_conductor", "name": "Maestro", "role": "conductor", "presence": "connected",
                 "last_seen_at": self.at - 5, "seat": "held"},
                {"id": "mb_player", "name": "Trumpet", "role": "player", "presence": "connected",
                 "last_seen_at": self.at - 4000, "seat": "empty", "unhandled": 3,
                 "oldest_unhandled_at": self.at - 600},
            ],
        }

    def task(self, name: str, state: str, *, at: float, owner_state: str | None = None) -> dict:
        return {
            "task": name, "state": state, "sender": "mb_conductor", "created_at": at,
            "message_id": "m_" + name, "recipients": ["mb_player"],
            "owners": [{"id": "mb_player", "state": owner_state or state, "delivery": "handled",
                        "state_at": at, "last_report_at": at, "history": "complete"}],
        }

    def test_no_snapshot_yet_is_reported_as_missing(self) -> None:
        view = podium.orchestra_view("mb_conductor")
        self.assertIsNone(view["roster"])
        self.assertIsNone(view["tasks"])
        self.assertFalse(view["monitor_alive"])

    def test_roster_ages_come_from_the_hub_timestamps(self) -> None:
        self.member_api._save_roster_snapshot(self.member, self.roster())
        roster = podium.orchestra_view("mb_conductor")["roster"]
        by_id = {row["id"]: row for row in roster["members"]}
        self.assertEqual(by_id["mb_conductor"]["presence"], "connected")
        self.assertTrue(by_id["mb_conductor"]["is_conductor"])
        # Connected at the hub, but silent past the presence window.
        self.assertEqual(by_id["mb_player"]["presence"], "stale")
        self.assertEqual(by_id["mb_player"]["seat"], "empty")
        self.assertEqual(by_id["mb_player"]["unhandled"], 3)
        self.assertLess(roster["age"], 5)

    def test_open_tasks_only_newest_first_with_attention(self) -> None:
        rows = [
            self.task("t_done", "done", at=self.at - 60),
            self.task("t_old", "started", at=self.at - 3 * 86400),
            self.task("t_new", "started", at=self.at - 30),
            self.task("t_stuck", "blocked", at=self.at - 120),
        ]
        self.member_api._save_task_snapshot(self.member, rows)
        tasks = podium.orchestra_view("mb_conductor")["tasks"]
        self.assertEqual([row["task"] for row in tasks["open"]], ["t_new", "t_stuck", "t_old"])
        self.assertEqual([row["recent"] for row in tasks["open"]], [True, True, False])
        self.assertEqual(tasks["closed_count"], 1)
        self.assertEqual(tasks["lifecycle"], "derived")
        self.assertEqual({(a["task"], a["kind"]) for a in tasks["attention"]},
                         {("t_stuck", "blocked"), ("t_old", "stale")})

    def test_view_never_dials_the_hub(self) -> None:
        self.member_api._save_roster_snapshot(self.member, self.roster())
        with mock.patch.object(self.member_api, "api_request", side_effect=AssertionError("dialed the hub")):
            podium.orchestra_view("mb_conductor")

    def test_server_answers_orchestra_json_only_for_a_membership(self) -> None:
        data = Path(self.tmp.name) / "data"
        data.mkdir()
        self.member_api._save_roster_snapshot(self.member, self.roster())
        for member_id, expected in ((None, 404), ("mb_conductor", 200), ("mb_missing", 404)):
            with self.subTest(member_id=member_id):
                httpd = podium.server(data, 0, member_id)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
                    connection.request("GET", "/orchestra.json")
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join()
                self.assertEqual(response.status, expected)
                if expected == 200:
                    self.assertEqual(json.loads(body)["roster"]["members"][0]["name"], "Maestro")


class BackgroundServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_home = os.environ.get("AGENT_ORCHESTRA_HOME")
        os.environ["AGENT_ORCHESTRA_HOME"] = self.tmp.name
        self.member = {"member_id": "mb_conductor", "orchestra_id": "orc_1"}
        from agent_orchestra import member as member_api

        member_api.save_member(self.member)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.data = Path(self.tmp.name) / "data"

    def tearDown(self) -> None:
        podium.stop("mb_conductor")
        if self.previous_home is None:
            os.environ.pop("AGENT_ORCHESTRA_HOME", None)
        else:
            os.environ["AGENT_ORCHESTRA_HOME"] = self.previous_home
        self.tmp.cleanup()

    def test_start_is_idempotent_and_stop_ends_it(self) -> None:
        self.assertFalse(podium.status("mb_conductor")["running"])
        started = podium.start(self.member, self.data, self.port)
        self.assertEqual(started["state"], "started")
        self.assertEqual(started["url"], f"http://127.0.0.1:{self.port}/")
        again = podium.start(self.member, self.data, self.port)
        self.assertEqual((again["state"], again["pid"]), ("already-running", started["pid"]))
        self.assertFalse(podium.status("mb_conductor")["stale_version"])
        stopped = podium.stop("mb_conductor")
        self.assertEqual(stopped["pid"], started["pid"])
        self.assertFalse(podium.status("mb_conductor")["running"])

    def test_a_busy_port_is_refused_without_recording_a_server(self) -> None:
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", self.port))
            busy.listen()
            with self.assertRaises(OrchestraError) as caught:
                podium.start(self.member, self.data, self.port)
        self.assertIn("--port", str(caught.exception))
        self.assertFalse(podium.status("mb_conductor")["running"])


if __name__ == "__main__":
    unittest.main()
