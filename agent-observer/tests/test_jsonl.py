import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from agent_observer.jsonl import read_append, read_window  # noqa: E402


class JsonlTests(unittest.TestCase):
    def test_partial_record_is_emitted_only_after_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            first = json.dumps({"type": "one"}).encode() + b"\n"
            partial = b'{"type":"two"'
            path.write_bytes(first + partial)

            initial = read_window(path, 0, path.stat().st_size)
            self.assertEqual(
                [record.value["type"] for record in initial.records], ["one"]
            )
            self.assertEqual(initial.partial, partial)
            old_end = path.stat().st_size

            with path.open("ab") as handle:
                handle.write(b"}\n")
            appended = read_append(
                path,
                old_end,
                path.stat().st_size,
                initial.partial,
                initial.partial_start,
            )
            self.assertEqual(
                [record.value["type"] for record in appended.records], ["two"]
            )
            self.assertEqual(appended.records[0].start, len(first))
            self.assertEqual(appended.partial, b"")

    def test_tail_window_discards_a_split_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            rows = [json.dumps({"n": value}) + "\n" for value in range(3)]
            body = "".join(rows).encode()
            path.write_bytes(body)
            start = len(rows[0]) + 3
            result = read_window(path, start, len(body))
            self.assertTrue(result.skipped_prefix)
            self.assertEqual([record.value["n"] for record in result.records], [2])

    def test_state_permissions_are_private(self):
        from agent_observer.db import ObserverDB

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with ObserverDB(state) as database:
                self.assertTrue(database.path.exists())
            self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(state / "observer.sqlite3").st_mode & 0o777, 0o600)

    def test_terminal_text_replaces_control_characters(self):
        from agent_observer.cli import _terminal_text

        rendered = _terminal_text("safe\x1b[31m red\x00\nforged")
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x00", rendered)
        self.assertNotIn("\n", rendered)
        self.assertIn("safe", rendered)


if __name__ == "__main__":
    unittest.main()
