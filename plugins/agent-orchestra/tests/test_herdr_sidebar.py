from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
INTEGRATION = ROOT / "integrations" / "herdr"


class HerdrSidebarTests(unittest.TestCase):
    def test_node_model_navigation_and_native_description(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required by the repository CI gate")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("HC_")}
        result = subprocess.run(
            [node, str(INTEGRATION / "tests" / "sidebar.mjs")],
            capture_output=True, text=True, timeout=30, env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class HerdrInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-sidebar-install-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "herdr"
        spec = importlib.util.spec_from_file_location("herdr_sidebar_installer", INTEGRATION / "install.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_install_repeat_update_and_unrelated_files(self) -> None:
        self.home.mkdir()
        (self.home / "settings.json").write_text("unrelated", encoding="utf-8")
        destination = self.module.install(self.home)
        (destination / "notes.txt").write_text("custom note", encoding="utf-8")
        self.assertEqual(self.module.install(self.home), destination)
        source = Path(self.tmp.name) / "source"
        shutil.copytree(INTEGRATION / "collaborations", source)
        (source / "main.js").write_text("// updated\n", encoding="utf-8")
        self.module.install(self.home, source)
        self.assertEqual((destination / "main.js").read_text(), "// updated\n")
        self.assertEqual((destination / "notes.txt").read_text(), "custom note")
        self.assertEqual((self.home / "settings.json").read_text(), "unrelated")
        self.assertEqual(sorted(p.name for p in (self.home / "mods").iterdir()), ["collaborations"])

    def test_edited_deleted_and_symlinked_managed_files_refused(self) -> None:
        for kind in ("edited", "deleted", "symlink"):
            with self.subTest(kind=kind):
                home = self.home / kind
                destination = self.module.install(home)
                target = destination / "main.js"
                before = (destination / "model.js").read_bytes()
                if kind == "edited":
                    target.write_text("custom", encoding="utf-8")
                else:
                    target.unlink()
                    if kind == "symlink":
                        target.symlink_to(Path(self.tmp.name) / "missing-target")
                with self.assertRaisesRegex(ValueError, "Locally changed"):
                    self.module.install(home)
                self.assertEqual((destination / "model.js").read_bytes(), before)
                if kind == "edited":
                    self.assertEqual(target.read_text(), "custom")

    def test_unmanaged_or_symlinked_mod_refused(self) -> None:
        destination = self.home / "mods" / "collaborations"
        destination.mkdir(parents=True)
        (destination / "main.js").write_text("custom", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.module.install(self.home)
        self.assertEqual((destination / "main.js").read_text(), "custom")
        shutil.rmtree(destination)
        destination.symlink_to(Path(self.tmp.name) / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.module.install(self.home)

    def test_new_file_cannot_follow_unmanaged_dangling_symlink(self) -> None:
        destination = self.module.install(self.home)
        manifest = destination / self.module.MANIFEST
        data = json.loads(manifest.read_text())
        data["files"].pop("model.js")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        (destination / "model.js").unlink()
        outside = Path(self.tmp.name) / "outside.js"
        (destination / "model.js").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.module.install(self.home)
        self.assertFalse(outside.exists())

    def test_home_required_by_cli(self) -> None:
        result = subprocess.run(
            [os.sys.executable, str(INTEGRATION / "install.py")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--home", result.stderr)

    def test_failed_replace_restores_or_retains_the_original_mod(self) -> None:
        for fail_restore in (False, True):
            with self.subTest(fail_restore=fail_restore):
                home = self.home / str(fail_restore)
                destination = self.module.install(home)
                (destination / "notes.txt").write_text("original", encoding="utf-8")
                replace = os.replace

                def fail(source, target):
                    if Path(source).name.startswith(".collaborations-stage-"):
                        raise OSError("stage rename refused")
                    if fail_restore and Path(source).name.startswith(".collaborations-backup-"):
                        raise OSError("restore refused")
                    return replace(source, target)

                with mock.patch.object(self.module.os, "replace", side_effect=fail):
                    with self.assertRaises(OSError):
                        self.module.install(home)
                if fail_restore:
                    retained = list((home / "mods").glob(".collaborations-backup-*"))
                    self.assertEqual(len(retained), 1)
                    self.assertEqual((retained[0] / "notes.txt").read_text(), "original")
                else:
                    self.assertEqual((destination / "notes.txt").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
