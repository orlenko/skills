"""done/ markers for mail the hub has not recorded, and mail retention."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import member as member_module  # noqa: E402
from agent_orchestra.core import OrchestraError, atomic_write_json, bucket_dir  # noqa: E402

MEMBER = "mb_retain0001"


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-orchestra-retention-")
        self.addCleanup(temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.done = bucket_dir(MEMBER, "done")
        self.sent = bucket_dir(MEMBER, "sent")
        self.member = {"member_id": MEMBER}

    def record(self, message_id: str, state: str, age: float = 0.0, index: bool = True) -> Path:
        done = {"id": message_id, "sync_state": state, "last_sync_attempt_at": 0}
        if index:
            member_module._write_done(MEMBER, done)
        else:
            atomic_write_json(self.done / f"{message_id}.json", done)
        path = self.done / f"{message_id}.json"
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    def test_only_marked_records_are_read(self):
        member_module._ensure_unsynced_index(MEMBER, self.done)
        for index in range(30):
            self.record(f"m_synced{index:08d}", "synced")
        self.record("m_owed00000001", "unsynced")
        opened: list[str] = []
        original = member_module.read_json

        def counting(path):
            opened.append(Path(path).name)
            return original(path)

        with mock.patch.object(member_module, "read_json", counting):
            rows = member_module.unsynced_handled(MEMBER)
        self.assertEqual([row["id"] for row in rows], ["m_owed00000001"])
        self.assertEqual([name for name in opened if name.startswith("m_")],
                         ["m_owed00000001.json"])

    def test_a_sync_that_lands_removes_the_marker(self):
        self.record("m_owed00000002", "unsynced")
        self.assertTrue((self.done / "m_owed00000002.unsynced").exists())
        with mock.patch.object(member_module, "api_request", return_value={"handled_at": 1.0}):
            member_module.flush_handled(self.member)
        self.assertFalse((self.done / "m_owed00000002.unsynced").exists())
        self.assertEqual(member_module.unsynced_handled(MEMBER), [])

    def test_a_failed_sync_keeps_the_marker(self):
        self.record("m_owed00000003", "unsynced")
        with mock.patch.object(member_module, "api_request", side_effect=OrchestraError("down")):
            member_module.flush_handled(self.member)
        self.assertTrue((self.done / "m_owed00000003.unsynced").exists())

    def test_records_from_an_older_version_are_marked_once(self):
        self.record("m_legacy000001", "unsynced", index=False)
        self.record("m_legacy000002", "synced", index=False)
        rows = member_module.unsynced_handled(MEMBER)
        self.assertEqual([row["id"] for row in rows], ["m_legacy000001"])
        self.assertTrue(member_module._index_path(MEMBER).exists())
        self.assertFalse((self.done / "m_legacy000002.unsynced").exists())

    def test_pruning_keeps_what_is_owed_and_what_is_recent(self):
        member_module._ensure_unsynced_index(MEMBER, self.done)
        month = 30 * 86400
        old_synced = self.record("m_oldsynced0001", "synced", age=month)
        old_owed = self.record("m_oldowed00001", "unsynced", age=month)
        fresh = self.record("m_fresh0000001", "synced")
        old_sent = self.sent / "m_oldsent00001.json"
        atomic_write_json(old_sent, {"id": "m_oldsent00001"})
        os.utime(old_sent, (time.time() - month, time.time() - month))
        new_sent = self.sent / "m_newsent00001.json"
        atomic_write_json(new_sent, {"id": "m_newsent00001"})

        self.assertEqual(member_module.prune_mail(MEMBER), 2)
        self.assertFalse(old_synced.exists())
        self.assertFalse(old_sent.exists())
        for kept in (old_owed, fresh, new_sent):
            self.assertTrue(kept.exists(), kept.name)

    def test_nothing_is_pruned_before_the_index_exists(self):
        old = self.record("m_oldowed00002", "unsynced", age=30 * 86400, index=False)
        self.assertEqual(member_module.prune_mail(MEMBER), 0)
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
