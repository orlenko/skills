#!/usr/bin/env python3
"""Install only this mod into an explicitly selected Herdr configuration home."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


FILES = ("main.js", "model.js", "mod.json")
MANIFEST = ".orlenko-install.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install(home: Path, source: Path | None = None) -> Path:
    home = home.expanduser().absolute()
    source = source or Path(__file__).resolve().parent / "collaborations"
    mods = home / "mods"
    destination = mods / "collaborations"
    for path in (home, mods, destination):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"Refusing non-directory or symlink: {path}")
    for name in FILES:
        if not (source / name).is_file() or (source / name).is_symlink():
            raise ValueError(f"Missing regular mod source: {name}")

    previous: dict[str, str] = {}
    if destination.exists():
        manifest = destination / MANIFEST
        if manifest.is_symlink():
            raise ValueError("Refusing a symlinked install manifest")
        if not manifest.exists():
            if any(destination.iterdir()):
                raise ValueError("Existing collaborations mod is unmanaged; keep or rename it first")
        else:
            try:
                record = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Invalid install manifest; leaving the mod unchanged") from exc
            previous = record.get("files") if isinstance(record, dict) and record.get("v") == 1 else None
            if not isinstance(previous, dict) or not previous:
                raise ValueError("Unknown install manifest; leaving the mod unchanged")
            for name, expected in previous.items():
                if (not isinstance(name, str) or name not in FILES
                        or not isinstance(expected, str)):
                    raise ValueError("Invalid managed file record")
                target = destination / name
                if target.is_symlink() or not target.is_file() or _digest(target) != expected:
                    raise ValueError(f"Locally changed managed file: {name}; leaving the mod unchanged")
            for name in FILES:
                if name not in previous and ((destination / name).exists() or (destination / name).is_symlink()):
                    raise ValueError(f"New mod file would overwrite an unmanaged file: {name}")

    mods.mkdir(parents=True, exist_ok=True)
    # Stage first; a failed preflight never changes existing app files.
    stage = Path(tempfile.mkdtemp(prefix=".collaborations-stage-", dir=mods))
    backup: Path | None = None
    installed = False
    try:
        if destination.exists():
            shutil.copytree(destination, stage, dirs_exist_ok=True, symlinks=True)
        for name in FILES:
            shutil.copyfile(source / name, stage / name)
        record = {"v": 1, "files": {name: _digest(stage / name) for name in FILES}}
        (stage / MANIFEST).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=".collaborations-backup-", dir=mods))
            backup.rmdir()
            os.replace(destination, backup)
        try:
            os.replace(stage, destination)
            installed = True
        except OSError:
            if backup is not None:
                try:
                    os.replace(backup, destination)
                except OSError as exc:
                    raise OSError(f"Restore failed; original mod retained at {backup}") from exc
                backup = None
            raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if backup is not None and installed:
            shutil.rmtree(backup)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True, type=Path,
                        help="Herdr app configuration home on this viewing machine")
    args = parser.parse_args()
    try:
        destination = install(args.home)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Not installed: {exc}\n")
    print(f"Installed {destination}")
    print("Herdr docks the new mod on the left when it discovers the folder.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
