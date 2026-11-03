from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

from agent_orchestra import member  # noqa: E402


class PidLockTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="orchestra-pid-lock-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / "waiter.json"
        self.guard = self.path.with_name(self.path.name + ".guard")
        environment = mock.patch.dict(os.environ, {"AGENT_ORCHESTRA_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def child_code(self, body: str) -> str:
        return (
            "import os, sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
            "from agent_orchestra.member import pid_lock\n"
            "path = Path(sys.argv[1])\n" + body
        )

    def child_environment(self) -> dict[str, str]:
        return {"PATH": os.defpath, "LC_ALL": "C", "AGENT_ORCHESTRA_HOME": str(self.home)}

    def test_paused_publication_excludes_threads_and_processes(self) -> None:
        # A contender used to unlink a PID file while its writer was publishing.
        self.path.write_text("{", encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        failures: list[BaseException] = []
        real_write = member.atomic_write_json

        def held_write(path: Path, value: object) -> None:
            entered.set()
            if not release.wait(timeout=10):
                raise TimeoutError("Publication barrier was not released")
            real_write(path, value)

        def owner() -> None:
            try:
                with member.pid_lock(self.path) as acquired:
                    self.assertTrue(acquired)
            except BaseException as exc:
                failures.append(exc)

        with mock.patch.object(member, "atomic_write_json", side_effect=held_write):
            worker = threading.Thread(target=owner)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                with member.pid_lock(self.path) as acquired:
                    self.assertFalse(acquired)
                child = subprocess.run(
                    [sys.executable, "-B", "-I", "-S", "-c", self.child_code(
                        "with pid_lock(path) as acquired:\n    print(int(acquired))\n"
                    ), str(self.path)],
                    env=self.child_environment(), capture_output=True, text=True,
                    timeout=10, check=True,
                )
                self.assertEqual(child.stdout.strip(), "0")
                self.assertEqual(self.path.read_text(encoding="utf-8"), "{")
            finally:
                release.set()
                worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertFalse(self.path.exists())

    def test_nested_contention_exception_and_stable_inode(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "invented operation failure"):
            with member.pid_lock(self.path) as acquired:
                self.assertTrue(acquired)
                identity = (self.guard.stat().st_dev, self.guard.stat().st_ino)
                with member.pid_lock(self.path) as nested:
                    self.assertFalse(nested)
                with member.pid_lock(self.path) as contender:
                    self.assertFalse(contender)
                raise RuntimeError("invented operation failure")
        self.assertFalse(self.path.exists())
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual((self.guard.stat().st_dev, self.guard.stat().st_ino), identity)

    def test_live_legacy_owner_is_preserved(self) -> None:
        original = json.dumps({"pid": os.getpid(), "started_at": 1})
        self.path.write_text(original, encoding="utf-8")
        with member.pid_lock(self.path) as acquired:
            self.assertFalse(acquired)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_dead_legacy_owner_is_replaced(self) -> None:
        child = subprocess.run(
            [sys.executable, "-B", "-I", "-S", "-c", "import os; print(os.getpid())"],
            env=self.child_environment(), capture_output=True, text=True,
            timeout=10, check=True,
        )
        self.path.write_text(json.dumps({"pid": int(child.stdout), "started_at": 1}))
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(json.loads(self.path.read_text())["pid"], os.getpid())

    def test_process_death_releases_guard_without_metadata_cleanup(self) -> None:
        child = subprocess.run(
            [sys.executable, "-B", "-I", "-S", "-c", self.child_code(
                "with pid_lock(path) as acquired:\n"
                "    assert acquired\n"
                "    print('owned', flush=True)\n"
                "    os._exit(0)\n"
            ), str(self.path)],
            env=self.child_environment(), capture_output=True, text=True,
            timeout=10, check=True,
        )
        self.assertEqual(child.stdout.strip(), "owned")
        self.assertTrue(self.path.exists())
        inode = self.guard.stat().st_ino
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.guard.stat().st_ino, inode)
        self.assertFalse(self.path.exists())

    def test_cleanup_error_does_not_strand_a_live_process(self) -> None:
        context = member.pid_lock(self.path)
        self.assertTrue(context.__enter__())
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("invented cleanup failure")):
            with self.assertRaises(PermissionError):
                context.__exit__(None, None, None)
        self.assertEqual(json.loads(self.path.read_text())["pid"], os.getpid())
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)
        self.assertFalse(self.path.exists())

    def test_cleanup_preserves_a_different_acquisition_token(self) -> None:
        other = {"pid": os.getpid(), "lock_protocol": "flock-v1", "lock_token": "other"}
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)
            member.atomic_write_json(self.path, other)
        self.assertEqual(json.loads(self.path.read_text()), other)
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)

    def test_publication_and_read_errors_never_grant_unlocked_success(self) -> None:
        with mock.patch.object(member, "atomic_write_json", side_effect=OSError("invented write failure")):
            with self.assertRaises(OSError):
                with member.pid_lock(self.path):
                    self.fail("Failed publication entered the operation")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("invented read failure")):
            with self.assertRaises(PermissionError):
                with member.pid_lock(self.path):
                    self.fail("Failed metadata read entered the operation")
        with member.pid_lock(self.path) as acquired:
            self.assertTrue(acquired)

    def test_executed_child_does_not_prolong_the_guard(self) -> None:
        child = None
        try:
            with member.pid_lock(self.path) as acquired:
                self.assertTrue(acquired)
                child = subprocess.Popen(
                    [sys.executable, "-B", "-I", "-S", "-c",
                     "import sys; print('ready', flush=True); sys.stdin.read(1)"],
                    env=self.child_environment(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=False, text=True,
                )
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(timeout=10), "Child did not reach exec")
                self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertIsNone(child.poll())
            with member.pid_lock(self.path) as acquired:
                self.assertTrue(acquired)
        finally:
            if child is not None:
                try:
                    child.communicate(input="x", timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
