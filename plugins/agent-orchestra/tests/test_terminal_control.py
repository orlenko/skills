"""Invented transports only: never discover, capture or type in user panes."""

from __future__ import annotations

import copy
import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_orchestra import core, keys, member, terminal_control as native  # noqa: E402
from agent_orchestra.protocol import TypeOptions  # noqa: E402


def paint(text: str, *, kitty: int = 0) -> bytes:
    return (
        "\x18\x1b[!p\x1b[H\x1b[2J\x1b[?2004h" + text + f"\x1b[2;3H\x1b[={kitty};1u"
    ).encode()


def composer(draft: str = "", *, codex: bool = False, popup: bool = False) -> str:
    if codex:
        menu = "enter insert · esc close\r\n" if popup else ""
        return (
            menu
            + "› "
            + (draft or "\x1b[2mAsk something\x1b[0m")
            + "\r\n\r\nfixture-model · context left"
            + ("" if popup else "\r\n? for shortcuts")
        )
    return (
        "─" * 30
        + "\r\n❯ "
        + (draft or "\x1b[2msuggestion\x1b[0m")
        + "\r\n"
        + "─" * 30
        + "\r\n? for shortcuts"
    )


class ScreenTests(unittest.TestCase):
    def test_codex_warning_footer_and_continuation_glyph_are_not_a_new_prompt(self):
        raw = paint(
            "› \r\n  › a human draft\r\n\r\nmodel · workspace\r\n  ⚠ 1 warning · f2 to view"
        )
        result = native.render(raw, 100, 20).prompt("codex")
        self.assertTrue(result.shown)
        self.assertEqual(result.typed, "› a human draft")

    def test_dim_and_truecolor_are_independent_and_multiline_draft_is_not_empty(self):
        screen = native.render(paint(composer()), 100, 20)
        self.assertTrue(screen.prompt("claude").idle)
        screen = native.render(
            paint(composer("\x1b[38;2;10;20;30mreal draft\x1b[0m")), 100, 20
        )
        self.assertEqual(screen.prompt("claude").typed, "real draft")
        for codex in (True, False):
            screen = native.render(
                paint(composer("\r\n  second line", codex=codex)), 100, 20
            )
            self.assertEqual(
                screen.prompt("codex" if codex else "claude").typed, "second line"
            )

    def test_cross_row_style_and_alternate_screen_repaint(self):
        raw = paint(
            "old transcript\x1b[2m\r\nold dim\x1b[?1049h\x1b[!p" + composer("real")
        )
        screen = native.render(raw, 100, 20)
        self.assertNotIn("old transcript", screen.plain)
        self.assertEqual(screen.prompt("claude").typed, "real")

    def test_working_dialog_and_unknown_screen_refuse(self):
        self.assertTrue(
            native.render(paint(composer()), 100, 20, "working")
            .prompt("claude")
            .working
        )
        self.assertFalse(
            native.render(paint(composer()), 100, 20, "blocked").prompt("claude").shown
        )
        trust = "Accessing workspace\r\n❯ No, exit\r\nYes, trust\r\nEnter to confirm"
        self.assertFalse(native.render(paint(trust), 100, 20).prompt("claude").shown)
        with self.assertRaises(keys.KeysError):
            native.render(paint("\x1b[4:3munknown style"), 100, 20)
        with self.assertRaises(keys.KeysError):
            native.render(b"", 1000, 1000)

    def test_keys_are_prevalidated_and_follow_supported_modes(self):
        legacy = native.render(paint(composer()), 100, 20)
        kitty = native.render(paint(composer(), kitty=5), 100, 20)
        for name in (
            "Enter",
            "Escape",
            "C-c",
            "Up",
            "F1",
            "F12",
            "Tab",
            "BTab",
            "Delete",
        ):
            self.assertTrue(native.encode(name, legacy))
            self.assertTrue(native.encode(name, kitty))
        self.assertEqual(native.encode("C-c", legacy), b"\x03")
        self.assertEqual(native.encode("C-c", kitty), b"\x1b[99;5u")
        with self.assertRaises(keys.KeysError):
            native.encode("F99", legacy)
        with self.assertRaises(keys.KeysError):
            native.encode("Enter", native.render(paint(composer(), kitty=8), 100, 20))


class Transport:
    def __init__(self, seat: dict):
        self.seat, self.draft, self.transcript = seat, "", ""
        self.frames: list[bytes] = []
        self.codex = self.popup = self.hold = self.rollover = self.lose_reply = False
        self.state = "idle"
        self.channel = 1
        self.closed = 0
        self.terminal = {
            "address": "t1",
            "pid": 100,
            "created_ms": 1,
            "state": "live",
            "cols": 100,
            "rows": 20,
            "foreground": {"pid": 200},
        }

    def session(self, *_args, **_kwargs):
        transport = self

        class FakeSession:
            identity = "invented-core"

            def request(self, method, **params):
                if transport.lose_reply and transport.frames:
                    raise OSError("invented lost acknowledgement")
                terminal = {**transport.terminal, "status": {"state": transport.state}}
                if method == "terminal.get":
                    return terminal
                if method == "terminal.attach":
                    self.assert_safe(params)
                    return {"terminal": terminal, "channel": transport.channel}
                raise AssertionError(method)

            def assert_safe(self, params):
                assert params["terminal"] == "t1" and params["history"] == 0
                assert params["resumed"] is True and "display" not in params

            def paint(self, _channel):
                return paint(
                    transport.transcript
                    + composer(
                        transport.draft, codex=transport.codex, popup=transport.popup
                    )
                )

            def frame(self, channel, kind, data):
                assert channel == 1 and kind == 0  # Never AUTOMATIC_INPUT.
                transport.frames.append(data)
                if transport.rollover:
                    transport.seat["owner_pid"] = 201
                if transport.hold:
                    return
                if data.startswith(b"\x1b[200~"):
                    transport.draft += data[6:-6].decode().replace("\n", "\r\n")
                elif data == b"\r":
                    if transport.popup:
                        transport.popup = False
                    else:
                        transport.transcript += (
                            "❯ " + transport.draft + "\r\nINVENTED DONE\r\n"
                        )
                        transport.draft = ""
                elif data == b"\x03":
                    transport.draft = ""
                else:
                    raise AssertionError(data)

            def close(self):
                transport.closed += 1

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        return FakeSession()


class NativeOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.seat = {
            "member_id": "mb_fixture01",
            "provider": "claude",
            "owner_pid": 200,
            "conductor_id": "mb_conductor1",
            "terminal_control": {
                "socket": "/invented/socket",
                "core": "invented-core",
                "address": "t1",
                "pid": 100,
                "created_ms": 1,
                "owner_pid": 200,
                "owner_start": [2],
                "root_start": [1],
            },
        }
        self.transport = Transport(self.seat)
        processes = {
            100: native.Process(1, 100, 7, 200, (1,)),
            200: native.Process(100, 200, 7, 200, (2,)),
        }

        def info(pid):
            if pid not in processes:
                raise keys.KeysError("invented missing process")
            return processes[pid]

        for patcher in (
            mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": temporary.name}),
            mock.patch.object(keys, "find_pane", return_value=None),
            mock.patch.object(keys, "sleep", lambda _: None),
            mock.patch.object(native, "Session", self.transport.session),
            mock.patch.object(native, "process", side_effect=info),
            mock.patch.object(member, "_pid_alive", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        member.save_member(self.seat)
        # Simulate current persisted ownership without putting fake live pids in real state.
        patcher = mock.patch.object(
            member, "load_member", side_effect=lambda _id: copy.deepcopy(self.seat)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.serial = 0

    def handle(
        self, text="/inert fixture", options=None, sender="mb_conductor1", ident=None
    ):
        self.serial += 1
        envelope = {
            "id": ident or f"m_fixture{self.serial:08d}",
            "from": {"id": sender},
            "act": "type",
            "text": f"ACT type\nTO mb_fixture01\nTYPE {(options or TypeOptions()).header()}\n\n{text}",
        }
        return member._handle_type(self.seat, envelope)

    def test_interactive_submit_multiline_no_enter_and_key_only(self):
        self.assertEqual(self.handle("first line\n\nthird line"), "typed")
        self.assertEqual(
            self.transport.frames,
            [b"\x1b[200~first line\n\nthird line\x1b[201~", b"\r"],
        )
        self.transport.frames.clear()
        self.assertEqual(
            self.handle("/inert fixture", TypeOptions(submit=False)), "typed"
        )
        self.assertEqual(len(self.transport.frames), 1)
        self.assertEqual(self.handle("", TypeOptions(keys=("Enter",))), "typed")
        self.assertEqual(self.transport.frames[-1], b"\r")

    def test_codex_completion_selection_is_a_positive_separate_stage(self):
        self.seat["provider"] = "codex"
        self.transport.codex = self.transport.popup = True
        self.assertEqual(self.handle("$inert"), "typed")
        self.assertEqual(
            self.transport.frames, [b"\x1b[200~$inert\x1b[201~", b"\r", b"\r"]
        )

    def test_authority_draft_busy_and_unknown_key_refuse_before_effect(self):
        self.assertEqual(self.handle(sender="mb_other0001"), "not-allowed")
        self.transport.draft = "\r\n  a human's second line"
        self.assertEqual(self.handle(), "refused-busy")
        self.transport.draft, self.transport.state = "", "working"
        self.assertEqual(self.handle(), "refused-busy")
        self.transport.state = "idle"
        self.assertEqual(
            self.handle(options=TypeOptions(keys=("F99",))), "terminal-error"
        )
        self.assertEqual(self.transport.frames, [])
        self.assertIsNone(member.quiet_until(self.seat["member_id"]))

    def test_reserved_rpc_channel_cannot_receive_typed_text(self):
        self.transport.channel = 0
        self.assertEqual(self.handle('{"method":"server.stop"}'), "terminal-error")
        self.assertEqual(self.transport.frames, [])

    def test_uncertain_wire_receipt_remains_held_for_old_hubs_but_reports_uncertain(
        self,
    ):
        receipt = {
            "id": "m_reply00001",
            "re": "m_request00001",
            "text": "ACT tell\nTO conductor\n\ntype held: delivery-uncertain: may still arrive; do not resend",
        }
        with (
            mock.patch.object(
                member, "send", return_value={"id": "m_request00001", "state": "queued"}
            ),
            mock.patch.object(member, "local_messages", return_value=[receipt]),
            mock.patch.object(member, "finish_messages") as finished,
        ):
            result = member.type_into(
                self.seat, "mb_other0001", "/inert", options=TypeOptions(), wait=1
            )
        self.assertEqual(result["outcome"], "delivery-uncertain")
        finished.assert_called_once_with(self.seat, ["m_reply00001"])

    def test_anytime_skips_busy_check_but_not_identity_checks(self):
        self.transport.state = "working"
        self.assertEqual(
            self.handle(options=TypeOptions(submit=False, anytime=True)), "typed"
        )
        self.transport.frames.clear()
        self.seat["terminal_control"]["owner_start"] = [999]
        self.assertEqual(self.handle(options=TypeOptions(anytime=True)), "no-pane")
        self.assertEqual(self.transport.frames, [])

    def test_uncertain_held_or_lost_input_keeps_quiet_and_never_sends_enter(self):
        for mode in ("hold", "lose_reply", "rollover"):
            with self.subTest(mode=mode):
                self.transport.frames.clear()
                self.transport.draft = ""
                self.seat["owner_pid"] = 200
                self.transport.hold = self.transport.lose_reply = (
                    self.transport.rollover
                ) = False
                setattr(self.transport, mode, True)
                self.assertEqual(self.handle(), "delivery-uncertain")
                self.assertEqual(len(self.transport.frames), 1)
                self.assertIsNotNone(member.quiet_until(self.seat["member_id"]))
                replies = [
                    json.loads(p.read_text())
                    for p in core.bucket_dir(self.seat["member_id"], "outbox").glob(
                        "*.json"
                    )
                ]
                self.assertTrue(
                    any("type held: delivery-uncertain:" in r["text"] for r in replies)
                )

    def test_duplicate_unknown_intent_is_not_typed_again_and_tmux_has_priority(self):
        self.transport.hold = True
        self.assertEqual(self.handle(ident="m_same00000001"), "delivery-uncertain")
        self.assertEqual(self.handle(ident="m_same00000001"), "duplicate")
        self.assertEqual(len(self.transport.frames), 1)
        with mock.patch.object(
            keys, "find_pane", return_value=keys.Pane("%2", 90, "invented")
        ):
            control = native.resolve(self.seat, lambda: self.seat)
        self.assertIsInstance(control, native.Tmux)
        self.assertGreater(self.transport.closed, 0)

    def test_native_agy_notice_wraps_and_is_deduplicated_and_quiet_honored(self):
        self.seat["provider"] = "agy"
        ident = "m_invented00001"
        core.atomic_write_json(
            core.bucket_dir(self.seat["member_id"], "pending") / f"{ident}.json",
            {"id": ident, "act": "ask", "need": "reply", "text": "fixture"},
        )
        with mock.patch.object(member, "quiet_until", return_value=1e12):
            member._wake_agy(self.seat)
        self.assertEqual(self.transport.frames, [])
        member._wake_agy(self.seat)
        member._wake_agy(self.seat)
        self.assertEqual(len(self.transport.frames), 2)
        self.assertIn(b"Agent Orchestra local inbox notice", self.transport.frames[0])
        self.assertEqual(self.transport.frames[1], b"\r")
        self.assertIn(
            "herdr", member.wake_capability("agy", self.seat["member_id"])["via"]
        )

    def test_spoofed_client_socket_is_not_a_binding_source(self):
        with mock.patch.dict(
            os.environ,
            {
                "HC_SOCKET": "/invented/client",
                "HC_TERMINAL_SOCKET": "",
                "HC_TERMINAL_ID": "t1",
            },
        ):
            self.assertIsNone(native.capture_binding(200))

    def test_tmux_ancestor_on_another_tty_does_not_capture_native_input(self):
        def info(pid):
            return {
                90: native.Process(1, 90, 9, 90, (9,)),
                100: native.Process(1, 100, 7, 200, (1,)),
                200: native.Process(100, 200, 7, 200, (2,)),
            }[pid]

        with (
            mock.patch.object(
                keys, "find_pane", return_value=keys.Pane("%2", 90, "outer")
            ),
            mock.patch.object(native, "process", side_effect=info),
        ):
            self.assertIsInstance(
                native.resolve(self.seat, lambda: self.seat), native.Herdr
            )

    def test_unbound_legacy_member_needs_its_own_cli_to_record_binding(self):
        binding = self.seat.pop("terminal_control")
        with mock.patch.object(
            native, "capture_binding", return_value=binding
        ) as capture:
            self.assertIsNone(native.resolve(self.seat, lambda: self.seat))
            capture.assert_not_called()
            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_SOCKET": "/invented/socket"}),
                mock.patch.object(core, "agent_session_pid", return_value=201),
            ):
                self.assertNotIn("terminal_control", member.claim_ownership(self.seat))
                capture.assert_not_called()
            with (
                mock.patch.dict(os.environ, {"HC_TERMINAL_SOCKET": "/invented/socket"}),
                mock.patch.object(core, "agent_session_pid", return_value=200),
            ):
                self.assertEqual(
                    member.claim_ownership(self.seat)["terminal_control"], binding
                )
                capture.assert_called_once_with(200)


class FramingTests(unittest.TestCase):
    def connection(
        self, frames=b"", *, peer=None, deadline=None, core_id="invented-core"
    ):
        hello = (
            json.dumps(
                {
                    "id": 0,
                    "result": {
                        "generation": 1,
                        "framed": True,
                        "core": {"id": "invented-core"},
                    },
                }
            ).encode()
            + b"\n"
        )

        class FakeSocket:
            def __init__(self):
                self.incoming, self.sent, self.closed = hello + frames, [], False

            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def recv(self, size):
                result, self.incoming = self.incoming[:size], self.incoming[size:]
                return result

            def sendall(self, data):
                self.sent.append(data)

            def close(self):
                self.closed = True

        sock = FakeSocket()
        for patcher in (
            mock.patch.object(native.socket, "socket", return_value=sock),
            mock.patch.object(
                native, "_peer_uid", return_value=os.getuid() if peer is None else peer
            ),
            mock.patch.object(native, "_owned_socket", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sock = sock
        return native.Session(
            "/invented/socket", deadline or time.monotonic() + 5, core_id
        )

    @staticmethod
    def frame(payload, channel=1):
        return struct.pack("<IBI", channel, 0, len(payload)) + payload

    def test_response_identity_and_split_utf8_paint_with_title_lookalike(self):
        reply = self.frame(b'{"id":1,"result":{"address":"t1"}}', channel=0)
        first = b"\x18\x1b]2;title \x1b[?9001l\x1b\\\xe2"
        second = b"\x9d\xaf\x1b[?9001l"
        with self.connection(reply + self.frame(first) + self.frame(second)) as session:
            self.assertEqual(
                session.request("terminal.get", terminal="t1"), {"address": "t1"}
            )
            self.assertTrue(session.paint(1).endswith("❯".encode()))
        self.assertTrue(self.sock.closed)
        kinds = [struct.unpack("<IBI", data[:9])[1] for data in self.sock.sent[1:]]
        self.assertEqual(kinds, [0, 1, 1])

    def test_wrong_peer_core_deadline_and_oversized_frame_close(self):
        with self.assertRaises(keys.KeysError):
            self.connection(peer=os.getuid() + 1)
        self.assertTrue(self.sock.closed)
        self.assertEqual(self.sock.sent, [])
        with self.assertRaises(keys.KeysError):
            self.connection(core_id="wrong-core")
        self.assertTrue(self.sock.closed)
        with self.assertRaises(keys.KeysError):
            self.connection(deadline=time.monotonic() - 1)
        self.assertTrue(self.sock.closed)
        with self.connection(
            struct.pack("<IBI", 1, 0, native.MAX_BYTES + 1)
        ) as session:
            with self.assertRaises(keys.KeysError):
                session.receive()

    def test_native_process_proof_uses_current_os_foreground_not_cached_sample(self):
        processes = {
            100: native.Process(1, 100, 7, 200, (1,)),
            200: native.Process(100, 200, 7, 200, (2,)),
        }
        with mock.patch.object(native, "process", side_effect=processes.__getitem__):
            native._prove(200, {"pid": 100, "foreground": None})
            processes[200] = native.Process(100, 200, 8, 200, (2,))
            with self.assertRaises(keys.KeysError):
                native._prove(200, {"pid": 100})
            processes[200] = native.Process(100, 200, 7, 300, (2,))
            with self.assertRaises(keys.KeysError):
                native._prove(200, {"pid": 100})


class OwnershipPersistenceTests(unittest.TestCase):
    def test_roster_refresh_cannot_restore_an_old_account_owner_or_binding(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": directory}),
        ):
            old = {
                "member_id": "mb_fixture01",
                "owner_pid": 100,
                "role": "player",
                "terminal_control": {"owner_pid": 100, "owner_start": [1]},
            }
            fresh = {
                **old,
                "owner_pid": 200,
                "terminal_control": {"owner_pid": 200, "owner_start": [2]},
            }
            member.save_member(fresh)
            member._apply_self_row(old, {"role": "conductor"}, "mb_fixture01")
            loaded = member.load_member("mb_fixture01")
            self.assertEqual(loaded["owner_pid"], 200)
            self.assertEqual(loaded["terminal_control"], fresh["terminal_control"])
            self.assertEqual(loaded["role"], "conductor")
            self.assertEqual(old["owner_pid"], 200)


@unittest.skipUnless(
    sys.platform == "darwin" or sys.platform.startswith("linux"), "native Unix ABI"
)
class NativeAbiTests(unittest.TestCase):
    def test_process_identity_matches_independent_os_parent_and_group(self):
        first, second = native.process(os.getpid()), native.process(os.getpid())
        self.assertEqual(first.parent, os.getppid())
        self.assertEqual(first.group, os.getpgrp())
        self.assertEqual(first.start, second.start)
        self.assertGreater(first.start[0], 0)

    def test_connected_peer_identity_comes_from_private_socket_pair(self):
        left, right = socket.socketpair()
        try:
            self.assertEqual(native._peer_uid(left), os.getuid())
            self.assertEqual(native._peer_uid(right), os.getuid())
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main()
