import os
import tempfile
import time
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_pair.core import (  # noqa: E402
    AgentPairError,
    atomic_write_json,
    decode_invite,
    encode_invite,
    instance_key,
    read_json,
)


class CoreTests(unittest.TestCase):
    def test_invite_round_trip(self):
        invite = encode_invite(
            {
                "pair_id": "pair_12345678",
                "endpoints": ["https://127.0.0.1:1234"],
                "fingerprint": "ab" * 32,
                "secret": "single-use-secret",
                "expires_at": time.time() + 60,
            }
        )
        self.assertTrue(invite.startswith("ap1."))
        decoded = decode_invite(invite)
        self.assertEqual(decoded["pair_id"], "pair_12345678")
        self.assertEqual(decoded["secret"], "single-use-secret")

    def test_expired_invite_is_rejected(self):
        invite = encode_invite(
            {
                "pair_id": "pair_12345678",
                "endpoints": ["https://127.0.0.1:1234"],
                "fingerprint": "ab" * 32,
                "secret": "secret",
                "expires_at": time.time() - 1,
            }
        )
        with self.assertRaisesRegex(AgentPairError, "expired"):
            decode_invite(invite)

    def test_instance_key_is_provider_and_directory_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(instance_key("codex", directory), instance_key("codex", directory))
            self.assertNotEqual(instance_key("codex", directory), instance_key("claude", directory))

    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            atomic_write_json(path, {"answer": 42})
            self.assertEqual(read_json(path), {"answer": 42})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
