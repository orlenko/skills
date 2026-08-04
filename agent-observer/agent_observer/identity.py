from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from .model import ProjectIdentity


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None


def worktree_root(path: Path) -> Path | None:
    root = _git(path, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def identify_project(path: str | os.PathLike[str]) -> ProjectIdentity:
    selected = Path(path).expanduser()
    if not selected.exists() or not selected.is_dir():
        raise ValueError(f"project directory does not exist: {selected}")
    resolved = selected.resolve()
    root = worktree_root(resolved)
    canonical = str(root or resolved)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return ProjectIdentity(
        project_id=f"project_{digest}",
        display_path=str(selected),
        resolved_path=str(resolved),
        worktree_root=str(root) if root else None,
    )


def current_branch(path: Path) -> str | None:
    branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch:
        return branch
    commit = _git(path, "rev-parse", "--verify", "HEAD")
    return f"detached:{commit[:12]}" if commit else None


def cwd_matches_project(cwd: str | None, project: dict[str, object]) -> bool:
    if not cwd:
        return False
    candidate = Path(cwd).expanduser()
    if not candidate.exists() or not candidate.is_dir():
        return False
    resolved = candidate.resolve()
    project_root = project.get("worktree_root")
    if project_root:
        root = worktree_root(resolved)
        return bool(root and str(root) == project_root)
    if worktree_root(resolved) is not None:
        return False
    watched = Path(str(project["resolved_path"]))
    try:
        resolved.relative_to(watched)
        return True
    except ValueError:
        return False
