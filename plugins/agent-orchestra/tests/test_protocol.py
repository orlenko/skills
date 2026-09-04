import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra.core import (  # noqa: E402
    BUCKETS,
    MAX_ERROR_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    MEMBER_ID_RE,
    MESSAGE_ID_RE,
    TASK_ID_RE,
    OrchestraError,
    _read_response_body,
    agent_ancestor_pid,
    bucket_dir,
    decode_invite,
    encode_invite,
    hub_dir,
    instance_key,
    member_path,
    new_member_id,
    new_message_id,
    new_orchestra_id,
    runtime_dir,
    state_root,
)
from agent_orchestra.protocol import (  # noqa: E402
    ACTS,
    ALIASES,
    Envelope,
    ProtocolError,
    parse_message,
    reply_required,
    summarize,
    validate_fields,
)


_MISSING = object()

PLAN_EXAMPLE = """ACT   assign
TO    mb_4c1e, children
TASK  t_i18n-zhtw-fonts
NEED  done: sha + test command by 18:00Z
REF   urban-sky/ops#3380, git:23cf639

Goal: trim client/public/fonts/noto-sans-tc to CJK-only unicode ranges.
Done when: PR builds green and the walk tool reports zero missing glyphs.
Verified: the font CSS is the only file that references the ranges.
Unverified: whether the walk tool runs on Ubuntu without the browser bundle.
"""


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)


class ProtocolTests(HomeTestCase):
    def test_plan_example_round_trip(self):
        envelope = parse_message(PLAN_EXAMPLE)
        self.assertEqual(envelope.act, "assign")
        self.assertEqual(envelope.to, ["mb_4c1e", "children"])
        self.assertEqual(envelope.task, "t_i18n-zhtw-fonts")
        self.assertEqual(envelope.need, "done: sha + test command by 18:00Z")
        self.assertEqual(envelope.refs, ["urban-sky/ops#3380", "git:23cf639"])
        self.assertIsNone(envelope.re)
        self.assertTrue(reply_required(envelope))

    def test_body_is_preserved_byte_for_byte(self):
        envelope = parse_message(PLAN_EXAMPLE)
        self.assertEqual(envelope.text, PLAN_EXAMPLE)
        body = envelope.text.split("\n\n", 1)[1]
        self.assertTrue(body.startswith("Goal: trim client/public/fonts"))
        self.assertTrue(body.endswith("browser bundle.\n"))

    def test_headers_only_message_is_valid(self):
        envelope = parse_message("ACT tell\nTO all")
        self.assertEqual(envelope.act, "tell")
        self.assertEqual(envelope.to, ["all"])
        self.assertEqual(envelope.need, "none")
        self.assertEqual(envelope.refs, [])
        self.assertFalse(reply_required(envelope))

    def test_empty_body_after_blank_line_is_valid(self):
        envelope = parse_message("ACT status\nTO conductor\n\n")
        self.assertEqual(envelope.to, ["conductor"])
        self.assertEqual(envelope.text.split("\n\n", 1)[1], "")

    def test_missing_act_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "ACT header is required"):
            parse_message("TO conductor\n\nbody")

    def test_unknown_key_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "Unknown header key WHEN"):
            parse_message("ACT tell\nWHEN now\nTO all\n\nbody")

    def test_duplicate_key_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "Duplicate TO header"):
            parse_message("ACT tell\nTO all\nTO conductor\n\nbody")

    def test_bad_act_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "Unknown act 'shout'"):
            parse_message("ACT shout\nTO all\n\nbody")

    def test_assign_without_task_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "assign requires a TASK"):
            parse_message("ACT assign\nTO children\n\nbody")

    def test_text_without_a_header_block_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "must start with a header block"):
            parse_message("hello there\nACT tell\n\nbody")
        with self.assertRaisesRegex(ProtocolError, "must start with a header block"):
            parse_message("")

    def test_too_many_header_lines_is_rejected(self):
        lines = ["ACT tell", "TO all"] + [f"REF r{index}" for index in range(25)]
        with self.assertRaisesRegex(ProtocolError, "longer than 20 lines"):
            parse_message("\n".join(lines))

    def test_empty_need_value_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "NEED header has an empty value"):
            parse_message("ACT tell\nTO all\nNEED   \n\nbody")

    def test_extra_to_is_merged_and_deduplicated(self):
        envelope = parse_message(
            "ACT tell\nTO conductor, mb_4c1e\n\nbody",
            ["conductor", "mb_9a7f2b31", "mb_4c1e"],
        )
        self.assertEqual(envelope.to, ["conductor", "mb_4c1e", "mb_9a7f2b31"])

    def test_to_only_from_extra_to(self):
        envelope = parse_message("ACT tell\n\nbody", ["all"])
        self.assertEqual(envelope.to, ["all"])

    def test_missing_recipients_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "at least one recipient"):
            parse_message("ACT tell\n\nbody")

    def test_unknown_recipient_token_is_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "'everyone' is not a member id"):
            parse_message("ACT tell\nTO everyone\n\nbody")

    def test_bad_re_and_task_ids_are_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "RE 'nope' is not a message id"):
            parse_message("ACT tell\nTO all\nRE nope\n\nbody")
        with self.assertRaisesRegex(ProtocolError, "TASK 'fonts' is not a task id"):
            parse_message("ACT tell\nTO all\nTASK fonts\n\nbody")

    def test_reply_required_reads_envelopes_and_rows(self):
        self.assertFalse(reply_required(Envelope(act="tell", to=["all"])))
        self.assertTrue(reply_required(Envelope(act="ask", to=["all"], need="ack")))
        self.assertTrue(reply_required({"need": "sha by 18:00Z"}))
        self.assertFalse(reply_required({"need": "none"}))
        self.assertFalse(reply_required({"id": "m_1234abcd"}))

    def test_summarize_one_line(self):
        line = summarize(
            {
                "id": "m_" + "a" * 32,
                "from": {"id": "mb_4c1e", "name": "ubuntu-codex"},
                "act": "assign",
                "task": "t_x",
                "need": "done: sha",
            }
        )
        self.assertEqual(
            line, "act=assign task=t_x need=done: sha from=ubuntu-codex id=m_" + "a" * 32
        )
        self.assertEqual(
            summarize({"id": "m_1234abcd", "from": "sys"}), "act=tell need=none from=sys id=m_1234abcd"
        )

    def test_validate_fields_matches_the_parser(self):
        validate_fields("assign", ["children"], None, "t_x", "none", [])
        validate_fields("tell", ["mb_4c1e"], "m_1234abcd", None, "ack", ["git:23cf639"])
        with self.assertRaisesRegex(ProtocolError, "Unknown act"):
            validate_fields("shout", ["all"], None, None, "none", [])
        with self.assertRaisesRegex(ProtocolError, "at least one recipient"):
            validate_fields("tell", [], None, None, "none", [])
        with self.assertRaisesRegex(ProtocolError, "not a member id"):
            validate_fields("tell", ["everyone"], None, None, "none", [])
        with self.assertRaisesRegex(ProtocolError, "assign requires a task"):
            validate_fields("assign", ["all"], None, None, "none", [])
        with self.assertRaisesRegex(ProtocolError, "NEED must be a non-empty string"):
            validate_fields("tell", ["all"], None, None, "  ", [])
        with self.assertRaisesRegex(ProtocolError, "REF must be a list"):
            validate_fields("tell", ["all"], None, None, "none", "git:23cf639")

    def test_constants(self):
        self.assertEqual(ACTS, ("ask", "tell", "done", "block", "dissent", "assign", "status"))
        self.assertEqual(ALIASES, ("conductor", "parent", "children", "siblings", "all"))
        self.assertTrue(issubclass(ProtocolError, OrchestraError))


class CoreTests(HomeTestCase):
    def _invite(self, **overrides):
        payload = {
            "orchestra_id": "orc_1234567890abcdef",
            "endpoints": ["https://127.0.0.1:1234"],
            "fingerprint": "ab" * 32,
            "secret": "single-use-secret",
            "expires_at": time.time() + 60,
            "role": "conductor",
            "parent": None,
        }
        payload.update(overrides)
        for key in [key for key, value in overrides.items() if value is _MISSING]:
            payload.pop(key)
        return encode_invite(payload)

    def test_invite_round_trip(self):
        invite = self._invite()
        self.assertTrue(invite.startswith("or1."))
        decoded = decode_invite(invite)
        self.assertEqual(decoded["orchestra_id"], "orc_1234567890abcdef")
        self.assertEqual(decoded["secret"], "single-use-secret")
        self.assertEqual(decoded["role"], "conductor")
        self.assertIsNone(decoded["parent"])
        self.assertEqual(decoded["v"], 1)

    def test_expired_invite_is_rejected(self):
        with self.assertRaisesRegex(OrchestraError, "expired"):
            decode_invite(self._invite(expires_at=time.time() - 1))

    def test_invite_missing_a_required_field_is_rejected(self):
        with self.assertRaisesRegex(OrchestraError, "missing required fields: role"):
            decode_invite(self._invite(role=_MISSING))

    def test_invite_prefix_is_checked(self):
        with self.assertRaisesRegex(OrchestraError, "must start with or1."):
            decode_invite("ap1." + self._invite()[4:])

    def test_crafted_invite_values_are_malformed_not_a_traceback(self):
        for overrides in (
            {"expires_at": "not-a-number"},
            {"expires_at": None},
            {"expires_at": {"soon": True}},
            {"expires_at": []},
            {"expires_at": float("nan")},
            {"fingerprint": 17},
            {"secret": None},
            {"role": ""},
            {"parent": ["mb_1234"]},
            {"endpoints": ["https://127.0.0.1:1234", 5]},
            {"orchestra_id": "orc_../../etc"},
            {"orchestra_id": 99},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(OrchestraError, "Invite is malformed"):
                    decode_invite(self._invite(**overrides))



    def test_instance_key_is_provider_and_directory_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(instance_key("codex", directory), instance_key("codex", directory))
            self.assertNotEqual(instance_key("codex", directory), instance_key("claude", directory))
            self.assertNotEqual(instance_key("codex", directory), instance_key("codex", self.home.name))

    def test_state_paths_live_under_the_home(self):
        root = state_root()
        member = "mb_" + "0" * 12
        self.assertEqual(hub_dir("orc_abc").parent, root / "hubs")
        self.assertEqual(member_path(member), root / "members" / member / "member.json")
        self.assertEqual(runtime_dir(), root / "runtime")
        for bucket in BUCKETS:
            path = bucket_dir(member, bucket)
            self.assertEqual(path, root / "members" / member / bucket)
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_bucket_dir_rejects_an_unknown_bucket(self):
        with self.assertRaisesRegex(OrchestraError, "Invalid mailbox bucket: archive"):
            bucket_dir("mb_" + "0" * 12, "archive")

    def test_generated_ids_match_the_exported_regexes(self):
        orchestra_id = new_orchestra_id()
        member_id = new_member_id()
        message_id = new_message_id()
        self.assertRegex(orchestra_id, r"^orc_[0-9a-f]{16}$")
        self.assertTrue(re.fullmatch(MEMBER_ID_RE, member_id))
        self.assertTrue(re.fullmatch(MESSAGE_ID_RE, message_id))
        self.assertEqual(member_id[:3], "mb_")
        self.assertEqual(len(message_id), 34)
        self.assertIsNone(re.fullmatch(MESSAGE_ID_RE, member_id))
        self.assertTrue(re.fullmatch(TASK_ID_RE, "t_i18n-zhtw-fonts"))
        self.assertIsNone(re.fullmatch(TASK_ID_RE, "i18n"))


class _FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200

    def read(self, amount: int) -> bytes:
        chunk, self.body = self.body[:amount], self.body[amount:]
        return chunk


class ResponseBodyTests(unittest.TestCase):
    def test_a_body_under_the_cap_is_read_whole(self):
        body = b"x" * (600 * 1024)
        self.assertEqual(_read_response_body(_FakeResponse(body), MAX_RESPONSE_BYTES), body)

    def test_a_body_over_the_cap_is_an_explicit_size_error(self):
        body = b"y" * (MAX_ERROR_RESPONSE_BYTES + 10)
        with self.assertRaisesRegex(OrchestraError, "larger than"):
            _read_response_body(_FakeResponse(body), MAX_ERROR_RESPONSE_BYTES)

    def test_the_success_cap_is_far_above_one_message(self):
        self.assertEqual(MAX_RESPONSE_BYTES, 8 * 1024 * 1024)
        self.assertGreater(MAX_RESPONSE_BYTES, MAX_ERROR_RESPONSE_BYTES)


class AgentAncestorTests(unittest.TestCase):
    def test_agent_ancestor_pid_answers_on_this_machine(self):
        value = agent_ancestor_pid()
        self.assertTrue(value is None or isinstance(value, int))
        if isinstance(value, int):
            self.assertGreater(value, 1)

    def test_a_nonsense_pid_never_raises(self):
        self.assertIsNone(agent_ancestor_pid(-5))
        self.assertIsNone(agent_ancestor_pid(0))


if __name__ == "__main__":
    unittest.main()
