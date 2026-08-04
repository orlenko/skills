from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import ObserverDB
from .discovery import TAIL_BYTES, discover_sources
from .identity import current_branch, cwd_matches_project, identify_project
from .jsonl import read_append, read_window
from .model import MEANINGFUL_KINDS, ReadWindow, SourceCandidate
from .providers import normalize_records


@dataclass(frozen=True)
class ObserverConfig:
    state_dir: Path
    claude_roots: tuple[Path, ...]
    codex_roots: tuple[Path, ...]

    @classmethod
    def defaults(cls, state_dir: str | None = None) -> "ObserverConfig":
        home = Path.home()
        state = Path(
            state_dir
            or os.environ.get("AGENT_OBSERVER_HOME")
            or home / ".local" / "state" / "agent-observer"
        ).expanduser()
        claude = Path(
            os.environ.get("AGENT_OBSERVER_CLAUDE_ROOT")
            or home / ".claude" / "projects"
        ).expanduser()
        codex = Path(
            os.environ.get("AGENT_OBSERVER_CODEX_ROOT") or home / ".codex" / "sessions"
        ).expanduser()
        archived = Path(
            os.environ.get("AGENT_OBSERVER_CODEX_ARCHIVE_ROOT")
            or home / ".codex" / "archived_sessions"
        ).expanduser()
        return cls(state, (claude,), (codex, archived))


class Observer:
    def __init__(self, config: ObserverConfig):
        self.config = config
        self.db = ObserverDB(config.state_dir)
        self._cwd_project_cache: dict[str, str | None] = {}

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Observer":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_project(self, path: str) -> dict[str, Any]:
        identity = identify_project(path)
        project = self.db.add_project(identity)
        self._cwd_project_cache.clear()
        self._sample_branch(project)
        discovered = self.rescan_project(identity.project_id)
        return {
            "project": self.db.project(identity.project_id),
            "sources_added": discovered,
        }

    def _resolve_project(self, value: str) -> dict[str, Any]:
        for project in self.db.projects():
            if value in {
                project["project_id"],
                project["display_path"],
                project["resolved_path"],
            }:
                return project
        try:
            identity = identify_project(value)
        except ValueError as exc:
            raise KeyError(f"watched project not found: {value}") from exc
        return self.db.project(identity.project_id)

    def remove_project(self, value: str) -> dict[str, Any]:
        project = self._resolve_project(value)
        self.db.remove_project(str(project["project_id"]))
        self._cwd_project_cache.clear()
        return {"removed": project["project_id"], "path": project["resolved_path"]}

    def rescan_project(self, value: str) -> int:
        project = self._resolve_project(value)
        candidates = discover_sources(
            project,
            claude_roots=self.config.claude_roots,
            codex_roots=self.config.codex_roots,
        )
        added = 0
        for candidate in candidates:
            prior = self.db.source(candidate.source_id)
            source, created = self.db.register_source(
                source_id=candidate.source_id,
                provider=candidate.provider,
                session_id=candidate.session_id,
                path=candidate.path,
                current_cwd=candidate.current_cwd,
                project_id=str(project["project_id"]),
                message_mode=candidate.message_mode,
            )
            if created:
                self._baseline(source, candidate)
                added += 1
            elif prior and not prior.get("monitoring"):
                # Do not parse the unread interval as though the source had
                # remained bound to this project. Re-enter through the same
                # bounded, cwd-aware baseline used for a newly found source.
                self._baseline(source, candidate)
            else:
                self._scan_source(self.db.source(candidate.source_id) or source)
        self._sample_branch(project)
        return added

    def scan(self) -> dict[str, Any]:
        changed = 0
        checked = 0
        for source in self.db.sources(monitoring_only=True):
            checked += 1
            if self._scan_source(source):
                changed += 1
        for project in self.db.projects():
            self._sample_branch(project)
        return {"sources_checked": checked, "sources_changed": changed}

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        moment = now or time.time()
        return self.db.status(moment)

    def _sample_branch(self, project: dict[str, Any]) -> None:
        root = project.get("worktree_root")
        branch = current_branch(Path(str(root))) if root else None
        self.db.set_branch(str(project["project_id"]), branch, time.time())

    def _match_project(self, cwd: str | None) -> dict[str, Any] | None:
        if not cwd:
            return None
        if cwd in self._cwd_project_cache:
            project_id = self._cwd_project_cache[cwd]
            return self.db.project(project_id) if project_id else None
        for project in self.db.projects():
            if cwd_matches_project(cwd, project):
                self._cwd_project_cache[cwd] = str(project["project_id"])
                return project
        self._cwd_project_cache[cwd] = None
        return None

    @staticmethod
    def _observation_id(
        source_id: str,
        generation: int,
        start: int,
        end: int,
        ordinal: int,
        kind: str,
    ) -> str:
        raw = f"{source_id}:{generation}:{start}:{end}:{ordinal}:{kind}"
        return "obs_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _baseline(
        self,
        source: dict[str, Any],
        candidate: SourceCandidate | None = None,
        *,
        generation: int | None = None,
        recovery_detail: str | None = None,
    ) -> None:
        path = Path(str(source["path"]))
        stat = path.stat()
        end = stat.st_size
        start = max(0, end - TAIL_BYTES)
        window = read_window(path, start, end)
        current_cwd = None
        message_mode = str(source.get("message_mode") or "unknown")
        if candidate and candidate.provider == "claude" and start == 0:
            current_cwd = candidate.current_cwd
        self._apply_window(
            source,
            window,
            device=stat.st_dev,
            inode=stat.st_ino,
            generation=generation or int(source["generation"]),
            checkpoint=end,
            current_cwd=current_cwd,
            message_mode=message_mode,
            baseline=True,
            health_detail=recovery_detail
            or (
                "baseline begins inside historical source"
                if window.skipped_prefix
                else None
            ),
        )

    def _scan_source(self, source: dict[str, Any]) -> bool:
        path = Path(str(source["path"]))
        now = time.time()
        try:
            stat = path.stat()
        except OSError as exc:
            with self.db.transaction():
                self.db.update_source_checkpoint(
                    str(source["source_id"]),
                    device=int(source.get("device") or 0),
                    inode=int(source.get("inode") or 0),
                    generation=int(source["generation"]),
                    offset=int(source["committed_offset"]),
                    partial=bytes(source.get("partial") or b""),
                    partial_start=int(source.get("partial_start") or 0),
                    current_cwd=source.get("current_cwd"),
                    current_project_id=source.get("current_project_id"),
                    message_mode=str(source.get("message_mode") or "unknown"),
                    monitoring=True,
                    health="unavailable",
                    health_detail=str(exc),
                    malformed_increment=0,
                    unknown_increment=0,
                    observed_at=None,
                    reconciled_at=now,
                )
            return False

        offset = int(source["committed_offset"])
        same_file = stat.st_dev == source.get("device") and stat.st_ino == source.get(
            "inode"
        )
        if not same_file or stat.st_size < offset:
            self._baseline(
                source,
                generation=int(source["generation"]) + 1,
                recovery_detail="source replacement or truncation started a new generation",
            )
            return True
        if stat.st_size == offset:
            with self.db.transaction():
                self.db.update_source_checkpoint(
                    str(source["source_id"]),
                    device=stat.st_dev,
                    inode=stat.st_ino,
                    generation=int(source["generation"]),
                    offset=offset,
                    partial=bytes(source.get("partial") or b""),
                    partial_start=int(source.get("partial_start") or offset),
                    current_cwd=source.get("current_cwd"),
                    current_project_id=source.get("current_project_id"),
                    message_mode=str(source.get("message_mode") or "unknown"),
                    monitoring=bool(source.get("monitoring")),
                    health=str(source.get("health") or "healthy"),
                    health_detail=source.get("health_detail"),
                    malformed_increment=0,
                    unknown_increment=0,
                    observed_at=None,
                    reconciled_at=now,
                )
            return False

        window = read_append(
            path,
            offset,
            stat.st_size,
            bytes(source.get("partial") or b""),
            int(source.get("partial_start") or offset),
        )
        self._apply_window(
            source,
            window,
            device=stat.st_dev,
            inode=stat.st_ino,
            generation=int(source["generation"]),
            checkpoint=stat.st_size,
            current_cwd=source.get("current_cwd"),
            message_mode=str(source.get("message_mode") or "unknown"),
            baseline=False,
        )
        return True

    def _apply_window(
        self,
        source: dict[str, Any],
        window: ReadWindow,
        *,
        device: int,
        inode: int,
        generation: int,
        checkpoint: int,
        current_cwd: str | None,
        message_mode: str,
        baseline: bool,
        health_detail: str | None = None,
    ) -> None:
        now = time.time()
        malformed = sum(1 for record in window.records if record.error)
        good_records = [record for record in window.records if record.value is not None]
        batches, message_mode = normalize_records(
            str(source["provider"]),
            good_records,
            session_id=str(source["session_id"]),
            generation=generation,
            message_mode=message_mode,
        )
        unknown = sum(1 for batch in batches if not batch.recognized)
        observed_at: float | None = None
        monitoring = True
        current_project = self._match_project(current_cwd)
        final_checkpoint = checkpoint
        final_partial = window.partial
        final_partial_start = window.partial_start

        with self.db.transaction():
            for batch in batches:
                if batch.cwd:
                    current_cwd = batch.cwd
                    current_project = self._match_project(current_cwd)
                    if current_project is None and not baseline:
                        monitoring = False
                        final_checkpoint = batch.record.end
                        final_partial = b""
                        final_partial_start = final_checkpoint
                        break
                if current_project is None:
                    continue
                event_source = {
                    **source,
                    "current_cwd": current_cwd,
                    "current_project_id": current_project["project_id"],
                }
                for event in batch.events:
                    observation_id = self._observation_id(
                        str(source["source_id"]),
                        generation,
                        batch.record.start,
                        batch.record.end,
                        event.ordinal,
                        event.kind,
                    )
                    inserted = self.db.insert_event(
                        observation_id=observation_id,
                        project_id=str(current_project["project_id"]),
                        source=event_source,
                        generation=generation,
                        byte_start=batch.record.start,
                        byte_end=batch.record.end,
                        event=event,
                        observed_at=now,
                    )
                    if inserted and event.kind in MEANINGFUL_KINDS:
                        observed_at = now

            health = "degraded" if malformed >= 3 else "healthy"
            detail = health_detail
            if malformed:
                detail = f"{malformed} malformed complete record(s) in latest scan"
            if unknown >= 20 and not any(batch.recognized for batch in batches[-20:]):
                health = "degraded"
                detail = "no recognized record among latest 20 complete records"
            self.db.update_source_checkpoint(
                str(source["source_id"]),
                device=device,
                inode=inode,
                generation=generation,
                offset=final_checkpoint,
                partial=final_partial,
                partial_start=final_partial_start,
                current_cwd=current_cwd,
                current_project_id=(
                    str(current_project["project_id"]) if current_project else None
                ),
                message_mode=message_mode,
                monitoring=monitoring and current_project is not None,
                health=health,
                health_detail=detail,
                malformed_increment=malformed,
                unknown_increment=unknown,
                observed_at=observed_at,
                reconciled_at=now,
            )
