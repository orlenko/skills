from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

from .identity import current_branch, cwd_matches_project, identify_project
from .jsonl import read_window
from .model import JsonRecord, ProjectIdentity, SourceCandidate
from .providers import normalize_records, source_candidate


HEADER_BYTES = 4 * 1024 * 1024
TAIL_BYTES = 4 * 1024 * 1024
DISCOVERY_DAYS = 30
DISCOVERY_LIMIT = 20
CATALOG_HEADER_BYTES = 256 * 1024
CATALOG_TAIL_BYTES = 512 * 1024
CATALOG_CONTEXT_LIMIT = 16 * 1024 * 1024
CATALOG_FILE_LIMIT = 256
CATALOG_PROJECT_LIMIT = 40


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


def _catalog_records(
    provider: str, path: Path
) -> tuple[list[JsonRecord], int, int, float]:
    stat = path.stat()
    size = stat.st_size
    tail_start = max(0, size - CATALOG_TAIL_BYTES)
    tail = read_window(path, tail_start, size)
    records = list(tail.records)
    if provider == "codex" and tail_start:
        header = read_window(path, 0, min(size, CATALOG_HEADER_BYTES))
        records.extend(header.records)
    unique = {
        (record.start, record.end): record
        for record in records
    }
    return list(unique.values()), size, tail_start, stat.st_mtime


def _codex_titles(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    try:
        size = path.stat().st_size
        window = read_window(path, max(0, size - 4 * 1024 * 1024), size)
    except OSError:
        return {}
    titles: dict[str, str] = {}
    for record in window.records:
        value = record.value or {}
        session_id = value.get("id")
        title = value.get("thread_name")
        if isinstance(session_id, str) and isinstance(title, str) and title.strip():
            titles[session_id] = title.strip()
    return titles


def _session_context(
    provider: str,
    path: Path,
    candidate: SourceCandidate,
) -> tuple[str | None, str | None]:
    """Find the latest user topic and a recent Claude title with bounded reads."""
    try:
        size = path.stat().st_size
    except OSError:
        return candidate.title, None
    window_bytes = min(CATALOG_TAIL_BYTES, size)
    title = candidate.title
    latest_user: str | None = None
    latest_agent: str | None = None
    while True:
        window = read_window(path, max(0, size - window_bytes), size)
        records = list(window.records)
        if provider == "claude" and not title:
            context_candidate = source_candidate(
                provider,
                path,
                records,
                file_size=size,
                tail_start=max(0, size - window_bytes),
                mtime=0,
            )
            title = context_candidate.title if context_candidate else None
        normalized, _mode = normalize_records(
            provider,
            sorted(records, key=lambda record: record.start),
            session_id=candidate.session_id,
            generation=1,
            message_mode=candidate.message_mode,
        )
        latest_user = None
        latest_agent = None
        for item in normalized:
            for event in item.events:
                excerpt = event.payload.get("excerpt")
                if not isinstance(excerpt, str) or not excerpt.strip():
                    continue
                compact = " ".join(excerpt.split())[:320]
                if event.kind == "user_message":
                    latest_user = compact
                elif event.kind in {"assistant_message", "turn_completed"}:
                    latest_agent = compact
        if latest_user and (provider != "claude" or title):
            break
        if window_bytes >= size or window_bytes >= CATALOG_CONTEXT_LIMIT:
            break
        window_bytes = min(size, CATALOG_CONTEXT_LIMIT, window_bytes * 2)
    return title, latest_user or latest_agent


def discover_recent_projects(
    *,
    claude_roots: Iterable[Path],
    codex_roots: Iterable[Path],
    watched_project_ids: set[str],
    codex_session_index: Path | None = None,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Return bounded, recent, unwatched project choices for dashboard enrollment."""
    now = now or time.time()
    cutoff = now - DISCOVERY_DAYS * 86400
    entries: list[tuple[float, str, Path]] = []
    for provider, roots in (("claude", claude_roots), ("codex", codex_roots)):
        entries.extend(
            (_mtime(path), provider, path) for path in _provider_files(provider, roots)
        )
    entries.sort(reverse=True, key=lambda entry: entry[0])
    titles = _codex_titles(codex_session_index)
    projects: dict[str, dict[str, object]] = {}
    identity_cache: dict[str, ProjectIdentity | None] = {}

    for mtime, provider, path in entries[:CATALOG_FILE_LIMIT]:
        if mtime < cutoff:
            break
        try:
            records, size, tail_start, observed_mtime = _catalog_records(provider, path)
            candidate = source_candidate(
                provider,
                path,
                records,
                file_size=size,
                tail_start=tail_start,
                mtime=observed_mtime,
            )
        except OSError:
            continue
        if candidate is None or not candidate.current_cwd:
            continue
        cwd = candidate.current_cwd
        if cwd not in identity_cache:
            try:
                identity_cache[cwd] = identify_project(cwd)
            except ValueError:
                identity_cache[cwd] = None
        identity = identity_cache[cwd]
        if identity is None or identity.project_id in watched_project_ids:
            continue
        existing = projects.get(identity.project_id)
        if existing is not None and float(existing["last_activity_at"]) >= mtime:
            providers = set(existing["providers"])
            providers.add(provider)
            existing["providers"] = sorted(providers)
            continue
        providers = set(existing["providers"]) if existing else set()
        providers.add(provider)
        title = candidate.title
        if provider == "codex":
            title = titles.get(candidate.session_id) or title
        resolved = Path(identity.resolved_path)
        projects[identity.project_id] = {
            "project_id": identity.project_id,
            "resolved_path": identity.resolved_path,
            "directory_name": resolved.name,
            "branch": current_branch(resolved),
            "providers": sorted(providers),
            "last_activity_at": mtime,
            "session": {
                "provider": provider,
                "session_id": candidate.session_id,
                "title": title,
                "topic": None,
            },
            "_context_source": (provider, path, candidate),
        }

    result = sorted(
        projects.values(),
        key=lambda item: (-float(item["last_activity_at"]), str(item["resolved_path"])),
    )[:CATALOG_PROJECT_LIMIT]
    for item in result:
        provider, path, candidate = item.pop("_context_source")
        title, topic = _session_context(provider, path, candidate)
        session = item["session"]
        if provider == "claude" and title:
            session["title"] = title
        session["topic"] = topic
    return result
