from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JsonRecord:
    start: int
    end: int
    value: dict[str, Any] | None
    error: str | None = None


@dataclass(frozen=True)
class ReadWindow:
    records: tuple[JsonRecord, ...]
    partial: bytes
    partial_start: int
    skipped_prefix: bool = False


@dataclass(frozen=True)
class ProjectIdentity:
    project_id: str
    display_path: str
    resolved_path: str
    worktree_root: str | None


@dataclass(frozen=True)
class SourceCandidate:
    provider: str
    session_id: str
    path: Path
    current_cwd: str | None
    message_mode: str = "unknown"
    title: str | None = None
    mtime: float = 0.0

    @property
    def source_id(self) -> str:
        return f"{self.provider}:{self.session_id}"


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    source_at: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    ordinal: int = 0


MEANINGFUL_KINDS = {
    "user_message",
    "assistant_message",
    "tool_started",
    "tool_finished",
    "turn_started",
    "turn_completed",
    "turn_aborted",
    "decision_requested",
    "decision_response",
    "child_activity",
}
