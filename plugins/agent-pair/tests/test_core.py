import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(PLUGIN_ROOT))

from agent_pair import client  # noqa: E402
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

    def test_stop_hook_nudge_caps_body_without_claiming(self):
        message_id = "m_12345678"
        body = "🙂" * 1025 + "tail"
        rows = [
            {
                "id": message_id,
                "from": {
                    "id": "p_peer",
                    "name": "Peer Claude",
                    "provider": "claude",
                },
                "text": body,
                "local_state": "pending",
            }
        ]
        endpoint = {"endpoint_id": "pair_example-p_local"}
        with patch.object(client, "local_messages", return_value=rows) as peek:
            nudge = client._hook_message_nudge(
                endpoint, "claude", " while you were working"
            )

        self.assertIsNotNone(nudge)
        assert nudge is not None
        self.assertIn("Peer Claude", nudge)
        self.assertIn(f"claim_token: {message_id}", nudge)
        self.assertIn("first 4096 UTF-8 bytes", nudge)
        self.assertNotIn("tail", nudge)
        self.assertIn(f"pending/{message_id}.json", nudge)
        self.assertIn("finish --json --provider claude", nudge)
        peek.assert_called_once_with(endpoint, claim=False)


if __name__ == "__main__":
    unittest.main()
