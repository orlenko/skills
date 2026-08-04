from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from .identity import cwd_matches_project
from .jsonl import read_window
from .model import JsonRecord, SourceCandidate
from .providers import source_candidate


HEADER_BYTES = 4 * 1024 * 1024
TAIL_BYTES = 4 * 1024 * 1024
DISCOVERY_DAYS = 30
DISCOVERY_LIMIT = 20


def _provider_files(provider: str, roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            if provider == "claude" and (
                "subagents" in path.parts or path.name.startswith("agent-")
            ):
                continue
            if path.is_file():
                files.append(path)
    return files


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return -1.0


def _metadata_records(
    provider: str, path: Path
) -> tuple[list[JsonRecord], int, int, float]:
    stat = path.stat()
    size = stat.st_size
    tail_start = max(0, size - TAIL_BYTES)
    tail = read_window(path, tail_start, size)
    if provider == "claude":
        return list(tail.records), size, tail_start, stat.st_mtime
    header_end = min(size, HEADER_BYTES)
    header = read_window(path, 0, header_end)
    by_range = {
        (record.start, record.end): record
        for record in (*header.records, *tail.records)
    }
    return list(by_range.values()), size, tail_start, stat.st_mtime


def discover_sources(
    project: dict[str, object],
    *,
    claude_roots: Iterable[Path],
    codex_roots: Iterable[Path],
    now: float | None = None,
) -> list[SourceCandidate]:
    now = now or time.time()
    cutoff = now - DISCOVERY_DAYS * 86400
    candidates: list[SourceCandidate] = []
    entries: list[tuple[float, str, Path]] = []
    for provider, roots in (("claude", claude_roots), ("codex", codex_roots)):
        entries.extend(
            (_mtime(path), provider, path) for path in _provider_files(provider, roots)
        )
    entries.sort(reverse=True, key=lambda entry: entry[0])

    def inspect(entry: tuple[float, str, Path]) -> SourceCandidate | None:
        _mtime_value, provider, path = entry
        try:
            records, size, tail_start, mtime = _metadata_records(provider, path)
            candidate = source_candidate(
                provider,
                path,
                records,
                file_size=size,
                tail_start=tail_start,
                mtime=mtime,
            )
        except OSError:
            return None
        if candidate and cwd_matches_project(candidate.current_cwd, project):
            return candidate
        return None

    recent_entries = [entry for entry in entries if entry[0] >= cutoff]
    for entry in recent_entries:
        candidate = inspect(entry)
        if candidate:
            candidates.append(candidate)
            if len(candidates) >= DISCOVERY_LIMIT:
                break

    if not candidates:
        for entry in entries[len(recent_entries) :]:
            candidate = inspect(entry)
            if candidate:
                candidates.append(candidate)
                break

    unique: list[SourceCandidate] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.source_id in seen:
            continue
        seen.add(candidate.source_id)
        unique.append(candidate)
    return unique[:DISCOVERY_LIMIT]
