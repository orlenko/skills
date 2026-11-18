from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_pair import cli, client, core, herdr_display  # noqa: E402


class HerdrDisplayLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="pair-herdr-display-")
        self.addCleanup(tmp.cleanup)
        self.cwd = str(Path(tmp.name) / "work")
        Path(self.cwd).mkdir()
        environment = mock.patch.dict(os.environ, {
            "AGENT_PAIR_HOME": str(Path(tmp.name) / "state"),
            "HC_TERMINAL_SOCKET": "", "HC_TERMINAL_ID": "",
            "ORLENKO_HERDR_DISPLAY": "",
        })
        environment.start()
        self.addCleanup(environment.stop)
        ownership = mock.patch.object(herdr_display, "owned_by", return_value=False)
        ownership.start()
        self.addCleanup(ownership.stop)
        self.endpoint = {
            "protocol": 1,
            "endpoint_id": "pair_test-p_test",
            "pair_id": "pair_test",
            "peer_id": "p_test",
            "provider": "test",
            "cwd": self.cwd,
            "instance_key": core.instance_key("test", self.cwd),
            "name": "Author",
            "role": "host",
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
            "closed_at": None,
        }
        self.save()

    def save(self) -> None:
        core.atomic_write_json(core.endpoint_path(self.endpoint["endpoint_id"]), self.endpoint)

    def binding(self, *, authorized: bool, legacy: bool = False) -> Path:
        path = client._binding_path("test", self.cwd, "session")
        record = {
            "endpoint_id": self.endpoint["endpoint_id"],
            "provider": "test", "cwd": self.cwd, "session_id": "session",
            "owner_pid": 101, "bound_at": time.time(),
        }
        if not legacy:
            record.update(version=2, herdr_display=authorized)
        core.atomic_write_json(path, record)
        return path

    def test_host_and_accept_enroll_before_the_monitor(self) -> None:
        order = []
        with mock.patch.object(client, "_generate_certificate", return_value="ab" * 32), \
                mock.patch.object(client, "_spawn_module", return_value=123), \
                mock.patch.object(client, "_pid_alive", return_value=True), \
                mock.patch.object(client, "read_json", return_value={"port": 31337}), \
                mock.patch.object(client, "discover_addresses", return_value=["127.0.0.1"]), \
                mock.patch.object(client, "_bind_herdr_display", side_effect=lambda e: order.append("bind")), \
                mock.patch.object(client, "start_monitor", side_effect=lambda e: order.append("monitor")):
            client.create_pair(provider="test", cwd=self.cwd, name="Host")
        self.assertEqual(order, ["bind", "monitor"])
        order.clear()
        with mock.patch.object(client, "decode_invite", return_value={
            "endpoints": [], "fingerprint": "ab" * 32, "secret": "test",
        }), mock.patch.object(client, "api_request", return_value={
            "peer_id": "p_guest", "pair_id": "pair_guest", "token": "test",
            "expires_at": time.time() + 3600,
        }), mock.patch.object(client, "_bind_herdr_display", side_effect=lambda e: order.append("bind")), \
                mock.patch.object(client, "start_monitor", side_effect=lambda e: order.append("monitor")):
            client.accept_pair("ap1.test", provider="test", cwd=self.cwd, name="Guest")
        self.assertEqual(order, ["bind", "monitor"])

    def test_unenrolled_default_does_not_discover_a_process_or_socket(self) -> None:
        with mock.patch.object(client, "_herdr_display_owner") as owner, \
                mock.patch.object(herdr_display, "bind") as bind:
            self.assertFalse(client._bind_herdr_display(self.endpoint))
        owner.assert_not_called()
        bind.assert_not_called()

    def test_explicit_enable_passes_a_fresh_close_predicate(self) -> None:
        with mock.patch.object(client, "_herdr_display_owner", return_value=101), \
                mock.patch.object(herdr_display, "bind", return_value=True) as bind, \
                mock.patch.object(herdr_display, "refresh"):
            self.assertTrue(client.configure_herdr_display(self.endpoint, enabled=True)["enabled"])
        predicate = bind.call_args.kwargs["current"]
        self.assertTrue(predicate())
        self.endpoint["closed_at"] = time.time()
        self.save()
        self.assertFalse(predicate())

    def test_explicit_enable_refuses_another_live_hook_owner(self) -> None:
        self.binding(authorized=True)
        with mock.patch.object(client, "_herdr_display_owner", return_value=202), \
                mock.patch.object(client, "_pid_alive", return_value=True), \
                mock.patch.object(herdr_display, "bind") as bind:
            result = client.configure_herdr_display(self.endpoint, enabled=True)
        self.assertFalse(result["ok"])
        self.assertNotIn("101", result["reason"])
        bind.assert_not_called()

    def test_explicit_enable_marks_only_its_verified_hook_binding(self) -> None:
        path = self.binding(authorized=False, legacy=True)
        with mock.patch.object(client, "_herdr_display_owner", return_value=101), \
                mock.patch.object(herdr_display, "bind", return_value=True), \
                mock.patch.object(herdr_display, "refresh"):
            client.configure_herdr_display(self.endpoint, enabled=True)
        record = core.read_json(path)
        self.assertEqual(record["version"], 2)
        self.assertIs(record["herdr_display"], True)

    def test_explicit_disable_keeps_ownership_checks(self) -> None:
        with mock.patch.object(client, "_herdr_display_owner", return_value=101), \
                mock.patch.object(herdr_display, "bind", return_value=True) as bind, \
                mock.patch.object(herdr_display, "refresh"):
            result = client.configure_herdr_display(self.endpoint, enabled=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertIs(bind.call_args.kwargs["enabled"], False)

    def test_successful_disable_reports_success_without_connecting_to_herdr(self) -> None:
        with mock.patch.object(client, "_herdr_display_owner", return_value=101), \
                mock.patch.object(herdr_display, "_Core") as connection:
            result = client.configure_herdr_display(self.endpoint, enabled=False)
        self.assertTrue(result["ok"])
        self.assertFalse(result["enabled"])
        connection.assert_not_called()
        record = core.read_json(client._herdr_display_path(self.endpoint["endpoint_id"]))
        self.assertFalse(record["enabled"])

    def test_legacy_and_cwd_only_hooks_cannot_gain_display_authority(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                path = self.binding(authorized=False, legacy=legacy)
                with mock.patch.object(client, "_pid_alive", return_value=True), \
                        mock.patch.object(client, "_bind_herdr_display") as bind:
                    client._bound_hook_endpoint("test", self.cwd, "session")
                bind.assert_not_called()
                path.unlink()
        with mock.patch.object(client, "agent_ancestor_pid", return_value=202), \
                mock.patch.object(client, "_pid_alive", return_value=True), \
                mock.patch.object(client, "_bind_herdr_display") as bind:
            client._bound_hook_endpoint("test", self.cwd, "new-session")
            client._bound_hook_endpoint("test", self.cwd, "new-session")
            client._bound_hook_endpoint("test", self.cwd, "")
        bind.assert_not_called()

    def test_authorized_session_rollover_preserves_provenance(self) -> None:
        path = self.binding(authorized=True)
        with mock.patch.object(client, "agent_ancestor_pid", return_value=202), \
                mock.patch.object(client, "_pid_alive", return_value=False), \
                mock.patch.object(client, "_bind_herdr_display") as bind:
            client._bound_hook_endpoint("test", self.cwd, "session")
        bind.assert_called_once_with(mock.ANY, enrolled_only=True)
        record = core.read_json(path)
        self.assertEqual(record["owner_pid"], 202)
        self.assertIs(record["herdr_display"], True)

    def test_missing_enrollment_cannot_be_recreated_by_an_authorized_hook(self) -> None:
        with mock.patch.object(client, "_herdr_display_owner") as owner, \
                mock.patch.object(herdr_display, "bind") as bind:
            self.assertFalse(client._bind_herdr_display(self.endpoint, enrolled_only=True))
        owner.assert_not_called()
        bind.assert_not_called()

    def test_one_shot_processes_cannot_adopt_display(self) -> None:
        for args in ("claude -p task", "claude --print task", "claude --print=task", "codex exec task", "codex review",
                     "agy -p task", "agy --prompt task", "agy --prompt=task"):
            with self.subTest(args=args), \
                    mock.patch.object(client, "agent_ancestor_pid", return_value=101), \
                    mock.patch.object(core, "_ps_field", return_value=args):
                self.assertIsNone(client._herdr_display_owner())

    def test_interactive_sessions_with_prompt_text_own_display(self) -> None:
        settings = '{"hooks":{"Stop":[{"hooks":[{"command":"aiq hook -p x"}]}]}}'
        for args in (
            "agy --dangerously-skip-permissions --model m --prompt-interactive aiq is "
            "handing the previous session's work -p review exec over",
            "agy -i review the PR",
            f"claude --settings {settings} --model opus carry on with the session's review",
            "codex --model m 'fix it'",
        ):
            with self.subTest(args=args), \
                    mock.patch.object(client, "agent_ancestor_pid", return_value=101), \
                    mock.patch.object(core, "_ps_field", return_value=args):
                self.assertEqual(client._herdr_display_owner(), 101)

    def test_one_shot_flags_after_settings_blob_are_still_seen(self) -> None:
        args = 'claude --settings {"hooks":{"x":"y"}} -p task'
        with mock.patch.object(client, "agent_ancestor_pid", return_value=101), \
                mock.patch.object(core, "_ps_field", return_value=args):
            self.assertIsNone(client._herdr_display_owner())

    def test_interactive_agy_session_owns_display(self) -> None:
        self.assertTrue(core._is_agent_command("/Users/me/.local/bin/agy"))
        with mock.patch.object(client, "agent_ancestor_pid", return_value=101), \
                mock.patch.object(core, "_ps_field", return_value="/Users/me/.local/bin/agy"):
            self.assertEqual(client._herdr_display_owner(), 101)

    def test_refresh_uses_current_disk_metadata_and_retires_ended_states(self) -> None:
        with mock.patch.object(herdr_display, "refresh") as refresh:
            client._refresh_herdr_display(self.endpoint["endpoint_id"])
        current = refresh.call_args.args[1]
        self.endpoint["name"] = "Renamed author"
        self.save()
        self.assertEqual(current()["name"], "Renamed author")
        for changes in ({"closed_at": time.time()}, {"expires_at": 1}):
            with self.subTest(changes=changes):
                changed = dict(self.endpoint, **changes)
                core.atomic_write_json(core.endpoint_path(self.endpoint["endpoint_id"]), changed)
                self.assertIsNone(current())

    def test_local_close_persists_before_retiring_display_even_without_network(self) -> None:
        retired = []
        with mock.patch.object(client, "api_request", side_effect=core.AgentPairError("offline")), \
                mock.patch.object(herdr_display, "invalidate", side_effect=lambda *a, **k: retired.append(
                    client.load_endpoint(self.endpoint["endpoint_id"])["closed_at"])):
            result = client.close_pair(self.endpoint)
        self.assertEqual(result["state"], "closed-locally")
        self.assertTrue(retired[0])

    def test_optional_display_failures_do_not_break_mailbox_close(self) -> None:
        with mock.patch.object(client, "api_request", return_value={"ok": True}), \
                mock.patch.object(herdr_display, "invalidate", side_effect=RuntimeError("unavailable")):
            self.assertTrue(client.close_pair(self.endpoint)["ok"])

    def test_remote_close_is_durable_and_later_hooks_cannot_revive_it(self) -> None:
        with mock.patch.object(client, "_refresh_herdr_display") as refresh, \
                mock.patch.object(client, "_wake_codex"), \
                mock.patch.object(client, "flush_handled"), \
                mock.patch.object(client, "flush_outbox"), \
                mock.patch.object(client, "api_request", side_effect=core.APIError(410, "closed")), \
                mock.patch.object(herdr_display, "invalidate") as retire:
            client._monitor_loop(self.endpoint["endpoint_id"])
        refresh.assert_called_once()
        retire.assert_called_once_with(client._herdr_display_path(self.endpoint["endpoint_id"]), disable=True)
        self.assertTrue(client.load_endpoint(self.endpoint["endpoint_id"])["closed_at"])
        with self.assertRaises(core.AgentPairError):
            client._bound_hook_endpoint("test", self.cwd, "new-session")

    def test_monitor_renews_before_network_failure_and_retires_expired_publication(self) -> None:
        order = []
        def end_after_failure(_seconds):
            self.endpoint["expires_at"] = 1
            self.save()
        with mock.patch.object(client, "_refresh_herdr_display", side_effect=lambda e: order.append("renew")), \
                mock.patch.object(client, "_wake_codex"), \
                mock.patch.object(client, "flush_handled"), \
                mock.patch.object(client, "flush_outbox"), \
                mock.patch.object(client, "api_request", side_effect=core.AgentPairError("offline")), \
                mock.patch.object(client.time, "sleep", side_effect=end_after_failure), \
                mock.patch.object(herdr_display, "invalidate", side_effect=lambda *a, **k: order.append("retire")):
            client._monitor_loop(self.endpoint["endpoint_id"])
        self.assertEqual(order, ["renew", "retire"])

    def test_cli_requires_an_explicit_endpoint_and_returns_generic_result(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["herdr-display", "enable", "--provider", "test", "--cwd", self.cwd])
        with self.assertRaisesRegex(core.AgentPairError, "explicit --endpoint-id"):
            cli.run(args)
        for action in ("enable", "disable"):
            with self.subTest(action=action):
                args = parser.parse_args([
                    "herdr-display", action, "--provider", "test", "--cwd", self.cwd,
                    "--endpoint-id", self.endpoint["endpoint_id"], "--json",
                ])
                with mock.patch.object(cli, "configure_herdr_display", return_value={
                    "ok": True, "enabled": action == "enable",
                }) as configure, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.run(args), 0)
                configure.assert_called_once_with(mock.ANY, enabled=action == "enable")


if __name__ == "__main__":
    unittest.main()
