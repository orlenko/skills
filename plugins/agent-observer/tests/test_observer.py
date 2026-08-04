import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.cli import _config  # noqa: E402
from agent_observer.service import Observer, ObserverConfig  # noqa: E402
from agent_observer.identity import cwd_matches_project, identify_project  # noqa: E402


def append_jsonl(path: Path, *records: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def claude_record(kind: str, cwd: Path, content, **extra):
    record = {
        "type": kind,
        "sessionId": "11111111-2222-3333-4444-555555555555",
        "uuid": extra.pop("uuid", f"uuid-{kind}-{len(str(content))}"),
        "cwd": str(cwd),
        "timestamp": extra.pop("timestamp", "2026-08-03T10:00:00Z"),
        "message": {"content": content, **extra.pop("message", {})},
    }
    record.update(extra)
    return record


def codex_session(cwd: Path, sid: str) -> dict:
    return {
        "type": "session_meta",
        "timestamp": "2026-08-03T10:00:00Z",
        "payload": {"id": sid, "cwd": str(cwd)},
    }


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.project = root / "project"
        self.project.mkdir()
        self.state = root / "state"
        self.claude_root = root / "claude"
        self.codex_root = root / "codex"
        self.config = ObserverConfig(
            self.state, (self.claude_root,), (self.codex_root,)
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_cli_preserves_the_codex_session_index_configuration(self):
        index = Path(self.temp.name) / "custom-session-index.jsonl"
        args = argparse.Namespace(
            state_dir=str(self.state),
            claude_root=None,
            codex_root=None,
        )
        with patch.dict(
            os.environ,
            {"AGENT_OBSERVER_CODEX_SESSION_INDEX": str(index)},
        ):
            self.assertEqual(_config(args).codex_session_index, index)

    def test_workspace_roots_private_state_inside_observer_directory(self):
        workspace = Path(self.temp.name) / "dash"
        workspace.mkdir()
        args = argparse.Namespace(
            state_dir=None,
            workspace=str(workspace),
            claude_root=None,
            codex_root=None,
        )
        config = _config(args)
        self.assertEqual(config.state_dir, workspace.resolve() / ".agent-observer")
        self.assertEqual(
            (config.state_dir / ".gitignore").read_text(encoding="utf-8"), "*\n"
        )
        with Observer(config) as observer:
            with self.assertRaisesRegex(ValueError, "cannot be watched"):
                observer.add_project(str(workspace))

    def test_claude_structured_questions_are_item_correlated(self):
        session = (
            self.claude_root
            / "encoded-project"
            / "11111111-2222-3333-4444-555555555555.jsonl"
        )
        append_jsonl(
            session,
            claude_record(
                "user", self.project, "Please propose the migration", uuid="u1"
            ),
            claude_record(
                "assistant",
                self.project,
                [
                    {"type": "text", "text": "I need two choices."},
                    {
                        "type": "tool_use",
                        "id": "ask-1",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Choose the database",
                                    "options": [
                                        {"label": "SQLite", "description": "Local"}
                                    ],
                                },
                                {
                                    "question": "Choose the port",
                                    "options": [
                                        {"label": "8765", "description": "Default"}
                                    ],
                                },
                            ]
                        },
                    },
                ],
                uuid="a1",
            ),
            claude_record(
                "user",
                self.project,
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "ask-1",
                        "answers": {"0": "SQLite"},
                    }
                ],
                uuid="u2",
            ),
            claude_record(
                "assistant",
                self.project,
                [{"type": "text", "text": "Thanks; the port is still open."}],
                uuid="a2",
                message={"stop_reason": "end_turn"},
            ),
            {
                "type": "system",
                "subtype": "turn_duration",
                "sessionId": "11111111-2222-3333-4444-555555555555",
                "uuid": "system-1",
                "cwd": str(self.project),
                "timestamp": "2026-08-03T10:01:00Z",
            },
        )

        with Observer(self.config) as observer:
            result = observer.add_project(str(self.project))
            self.assertEqual(result["sources_added"], 1)
            status = observer.status()
            project = status["projects"][0]
            decisions = [
                finding
                for finding in project["findings"]
                if finding["kind"] == "decision_requested"
            ]
            self.assertEqual(len(decisions), 1)
            items = decisions[0]["details"]["items"]
            self.assertEqual(items["0"]["state"], "resolved")
            self.assertEqual(items["1"]["state"], "open")
            self.assertEqual(len(project["sessions"]), 1)
            self.assertEqual(
                project["sessions"][0]["last_message_excerpt"],
                "Thanks; the port is still open.",
            )
            stored_messages = observer.db.connection.execute(
                """
                SELECT payload_json FROM observations
                WHERE kind IN ('user_message', 'assistant_message')
                """
            ).fetchall()
            self.assertTrue(stored_messages)
            self.assertTrue(
                all("excerpt" not in json.loads(row[0]) for row in stored_messages)
            )
            self.assertEqual(
                len(
                    [
                        item
                        for item in project["findings"]
                        if item["kind"] == "turn_completed"
                    ]
                ),
                1,
            )

            append_jsonl(
                session,
                claude_record("user", self.project, "Use port 8765", uuid="u3"),
            )
            observer.scan()
            status = observer.status()
            project = status["projects"][0]
            decisions = [
                finding
                for finding in project["findings"]
                if finding["kind"] == "decision_requested"
            ]
            self.assertEqual(decisions[0]["details"]["items"]["1"]["state"], "open")
            completed = [
                finding
                for finding in project["findings"]
                if finding["kind"] == "turn_completed"
            ]
            self.assertEqual(completed, [])

    def test_codex_display_messages_win_over_response_item_duplicates(self):
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        session = self.codex_root / f"rollout-2026-08-03-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "response_item",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>ignore</environment_context>",
                        }
                    ],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:02Z",
                "payload": {"type": "user_message", "message": "Implement it"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:03Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "Working",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-03T10:00:03Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Working"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:04Z",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": "Done",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-03T10:00:04Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:05Z",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "Done",
                },
            },
        )

        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            observations = observer.db.connection.execute(
                "SELECT kind, payload_json FROM observations ORDER BY byte_start, ordinal"
            ).fetchall()
            message_kinds = [
                row["kind"] for row in observations if row["kind"].endswith("_message")
            ]
            self.assertEqual(
                message_kinds,
                ["user_message", "assistant_message", "assistant_message"],
            )
            source = observer.db.sources()[0]
            self.assertEqual(source["message_mode"], "display")
            status = observer.status()
            session_row = status["projects"][0]["sessions"][0]
            self.assertEqual(session_row["last_message_excerpt"], "Done")
            self.assertEqual(session_row["last_turn_state"], "completed")

    def test_dismissing_project_attention_clears_backlog_until_new_finding(self):
        first_sid = "99999999-aaaa-bbbb-cccc-dddddddddddd"
        second_sid = "99999999-aaaa-bbbb-cccc-eeeeeeeeeeee"

        def completed_turn(session: Path, sid: str, turn: str, message: str) -> None:
            append_jsonl(
                session,
                codex_session(self.project, sid),
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T12:00:01Z",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": message,
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T12:00:02Z",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn,
                        "last_agent_message": message,
                    },
                },
            )

        first = self.codex_root / f"rollout-{first_sid}.jsonl"
        second = self.codex_root / f"rollout-{second_sid}.jsonl"
        completed_turn(first, first_sid, "turn-1", "First completed turn")
        completed_turn(second, second_sid, "turn-2", "Second completed turn")

        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            current = observer.status()["projects"][0]["findings"]
            self.assertEqual(len(current), 2)
            project_id = observer.status()["projects"][0]["project_id"]
            self.assertTrue(observer.db.dismiss_project_findings(project_id))
            dismissed = observer.status()["projects"][0]["findings"]
            self.assertTrue(all(finding["seen"] for finding in dismissed))

            append_jsonl(
                first,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T12:01:00Z",
                    "payload": {"type": "user_message", "message": "One more thing"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T12:01:01Z",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "A newer completed turn",
                    },
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-04T12:01:02Z",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-3",
                        "last_agent_message": "A newer completed turn",
                    },
                },
            )
            observer.scan()
            current = observer.status()["projects"][0]["findings"]
            unseen = [finding for finding in current if not finding["seen"]]
            self.assertEqual(len(unseen), 1)
            self.assertEqual(unseen[0]["summary"], "A newer completed turn")

    def test_legacy_codex_response_messages_are_supported(self):
        sid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        session = self.codex_root / f"rollout-legacy-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "response_item",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Real prompt"}],
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-08-03T10:00:02Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Legacy answer"}],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:03Z",
                "payload": {"type": "task_complete", "turn_id": "legacy-turn"},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            status = observer.status()
            row = status["projects"][0]["sessions"][0]
            self.assertEqual(row["last_message_excerpt"], "Legacy answer")
            self.assertEqual(row["last_turn_state"], "completed")

    def test_incremental_checkpoint_survives_restart_and_partial_append(self):
        sid = "cccccccc-dddd-eeee-ffff-000000000000"
        session = self.codex_root / f"rollout-{sid}.jsonl"
        append_jsonl(session, codex_session(self.project, sid))
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            before = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]

        partial = json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T11:00:00Z",
                "payload": {"type": "user_message", "message": "New work"},
            }
        )
        split = len(partial) // 2
        with session.open("a", encoding="utf-8") as handle:
            handle.write(partial[:split])
        with Observer(self.config) as observer:
            observer.scan()
            middle = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            self.assertEqual(middle, before)

        with session.open("a", encoding="utf-8") as handle:
            handle.write(partial[split:] + "\n")
        with Observer(self.config) as observer:
            observer.scan()
            after = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            self.assertEqual(after, before + 1)
            observer.scan()
            final = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            self.assertEqual(final, after)

    def test_branch_change_is_sampled_without_agent_attribution(self):
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "checkout", "-q", "-b", "first"],
            check=True,
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            subprocess.run(
                ["git", "-C", str(self.project), "checkout", "-q", "-b", "second"],
                check=True,
            )
            observer.scan()
            changes = observer.status()["projects"][0]["changes"]
            self.assertEqual(changes[0]["kind"], "git_branch_changed")
            self.assertEqual(changes[0]["old_value"], "first")
            self.assertEqual(changes[0]["new_value"], "second")

    def test_codex_session_index_names_are_synced_and_changes_are_recorded(self):
        sid = "12345678-abcd-efab-cdef-123456789abc"
        session = self.codex_root / f"rollout-{sid}.jsonl"
        index = Path(self.temp.name) / "session_index.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {"type": "user_message", "message": "Named work"},
            },
        )
        append_jsonl(
            index,
            {
                "id": sid,
                "thread_name": "First session name",
                "updated_at": "2026-08-03T10:00:02Z",
            },
        )
        config = ObserverConfig(
            self.state,
            (self.claude_root,),
            (self.codex_root,),
            index,
        )
        with Observer(config) as observer:
            observer.add_project(str(self.project))
            row = observer.status()["projects"][0]["sessions"][0]
            self.assertEqual(row["title"], "First session name")

            append_jsonl(
                index,
                {
                    "id": sid,
                    "thread_name": "Renamed session",
                    "updated_at": "2026-08-03T10:05:00Z",
                },
            )
            result = observer.scan()
            self.assertEqual(result["sources_changed"], 1)
            project = observer.status()["projects"][0]
            self.assertEqual(project["sessions"][0]["title"], "Renamed session")
            self.assertEqual(project["changes"][0]["kind"], "session_title_changed")
            self.assertEqual(project["changes"][0]["old_value"], "First session name")
            self.assertEqual(project["changes"][0]["new_value"], "Renamed session")

    def test_historical_baseline_uses_source_time_for_activity_age(self):
        sid = "12345678-aaaa-bbbb-cccc-123456789abc"
        session = self.codex_root / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:05Z",
                "payload": {"type": "user_message", "message": "Old work"},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            status = observer.status(now=1785754805.0)
            age = status["projects"][0]["sessions"][0]["activity_age_seconds"]
            self.assertEqual(age, 3600.0)

    def test_nested_repository_is_not_absorbed_by_non_git_project(self):
        nested = self.project / "nested"
        nested.mkdir()
        subprocess.run(["git", "init", "-q", str(nested)], check=True)
        identity = identify_project(self.project)
        project = {
            "resolved_path": identity.resolved_path,
            "worktree_root": identity.worktree_root,
        }
        self.assertFalse(cwd_matches_project(str(nested), project))

    def test_remove_and_readd_performs_a_new_baseline(self):
        sid = "dddddddd-eeee-ffff-0000-111111111111"
        session = self.codex_root / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {"type": "user_message", "message": "First prompt"},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            self.assertEqual(len(observer.status()["projects"][0]["sessions"]), 1)
            observer.remove_project(str(self.project))
            self.assertEqual(observer.status()["projects"], [])
            result = observer.add_project(str(self.project))
            self.assertEqual(result["sources_added"], 1)
            self.assertEqual(len(observer.status()["projects"][0]["sessions"]), 1)

    def test_codex_turn_context_leaving_watchlist_stops_content_reads(self):
        sid = "eeeeeeee-ffff-0000-1111-222222222222"
        other = Path(self.temp.name) / "other"
        other.mkdir()
        session = self.codex_root / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {"type": "user_message", "message": "Watched prompt"},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            before = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            append_jsonl(
                session,
                {
                    "type": "turn_context",
                    "timestamp": "2026-08-03T10:01:00Z",
                    "payload": {"cwd": str(other), "turn_id": "turn-2"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-03T10:01:01Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Private other work",
                    },
                },
            )
            observer.scan()
            after = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            self.assertEqual(after, before)
            source = observer.db.sources()[0]
            self.assertEqual(source["monitoring"], 0)
            self.assertIsNone(source["current_project_id"])

            append_jsonl(
                session,
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-03T10:01:02Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Still private other work",
                    },
                },
                {
                    "type": "turn_context",
                    "timestamp": "2026-08-03T10:02:00Z",
                    "payload": {"cwd": str(self.project), "turn_id": "turn-3"},
                },
                {
                    "type": "event_msg",
                    "timestamp": "2026-08-03T10:02:01Z",
                    "payload": {
                        "type": "user_message",
                        "message": "Back in watched work",
                    },
                },
            )
            observer.rescan_project(str(self.project))
            row = observer.status()["projects"][0]["sessions"][0]
            self.assertEqual(row["last_message_excerpt"], "Back in watched work")
            messages = observer.db.connection.execute(
                "SELECT COUNT(*) FROM observations WHERE kind = 'user_message'"
            ).fetchone()[0]
            self.assertEqual(messages, 2)

    def test_truncation_starts_a_new_source_generation(self):
        sid = "ffffffff-0000-1111-2222-333333333333"
        session = self.codex_root / f"rollout-{sid}.jsonl"
        append_jsonl(
            session,
            codex_session(self.project, sid),
            {
                "type": "event_msg",
                "timestamp": "2026-08-03T10:00:01Z",
                "payload": {"type": "user_message", "message": "A" * 2000},
            },
        )
        with Observer(self.config) as observer:
            observer.add_project(str(self.project))
            session.write_text(
                json.dumps(codex_session(self.project, sid))
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-08-03T10:02:00Z",
                        "payload": {"type": "user_message", "message": "Replacement"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            observer.scan()
            source = observer.db.sources()[0]
            self.assertEqual(source["generation"], 2)
            self.assertIn("new generation", source["health_detail"])
            row = observer.status()["projects"][0]["sessions"][0]
            self.assertEqual(row["last_message_excerpt"], "Replacement")


if __name__ == "__main__":
    unittest.main()
