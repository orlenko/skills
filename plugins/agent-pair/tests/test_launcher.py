"""The launcher every hook starts, and where each host finds its hooks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "agent-pair"


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="agent-pair-launcher-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.marker = self.root / "shim-ran"
        self.shims = self.root / "pyenv" / "shims"
        self.shims.mkdir(parents=True)
        shim = self.shims / "python3"
        shim.write_text(f"#!/bin/sh\n: > '{self.marker}'\nexit 97\n")
        shim.chmod(0o755)
        self.real = self.root / "real"
        self.real.mkdir()
        (self.real / "python3").symlink_to(sys.executable)

    def run_launcher(self, path: str, **env: str) -> subprocess.CompletedProcess:
        environ = {key: value for key, value in os.environ.items() if key != "AGENT_PAIR_PYTHON"}
        environ.update(PATH=path, **env)
        return subprocess.run([str(LAUNCHER), "--version"], env=environ,
                              capture_output=True, text=True, timeout=30)

    def test_a_version_manager_shim_first_on_path_is_skipped(self) -> None:
        # pyenv's shim spent 0.4-3 s per hook start on a loaded machine.
        result = self.run_launcher(f"{self.shims}:{self.real}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("agent-pair "))
        self.assertFalse(self.marker.exists())

    def test_the_shim_is_the_last_resort(self) -> None:
        result = self.run_launcher(str(self.shims))
        self.assertEqual(result.returncode, 97)
        self.assertTrue(self.marker.exists())

    def test_the_environment_override_wins(self) -> None:
        result = self.run_launcher(str(self.shims), AGENT_PAIR_PYTHON=sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())


class HookLayoutTests(unittest.TestCase):
    def test_claude_cannot_load_the_codex_hooks(self) -> None:
        # Claude Code loads hooks/hooks.json beside its manifest's file, so the
        # Codex hooks ran in every Claude turn and bound a Claude session to a
        # Codex seat. Codex reads the path its own manifest names.
        self.assertFalse((ROOT / "hooks" / "hooks.json").exists())
        codex = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(codex["hooks"], "./hooks/codex-hooks.json")
        self.assertEqual(claude["hooks"], "./hooks/claude-hooks.json")
        for manifest in (codex, claude):
            self.assertTrue((ROOT / manifest["hooks"]).is_file())

    def test_turn_hooks_allow_for_a_loaded_machine(self) -> None:
        for name in ("claude-hooks.json", "codex-hooks.json"):
            config = json.loads((ROOT / "hooks" / name).read_text())
            for groups in config["hooks"].values():
                for hook in (hook for group in groups for hook in group["hooks"]):
                    if hook.get("asyncRewake"):
                        continue
                    with self.subTest(file=name, command=hook["command"]):
                        self.assertGreaterEqual(hook["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
