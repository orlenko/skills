from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .model import MEANINGFUL_KINDS, NormalizedEvent, ProjectIdentity


def _source_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        numeric = float(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return numeric / 1000.0 if numeric > 10_000_000_000 else numeric


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_path TEXT NOT NULL,
    resolved_path TEXT NOT NULL UNIQUE,
    worktree_root TEXT,
    added_at REAL NOT NULL,
    current_branch TEXT,
    branch_sampled_at REAL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    path TEXT NOT NULL,
    device INTEGER,
    inode INTEGER,
    generation INTEGER NOT NULL DEFAULT 1,
    committed_offset INTEGER NOT NULL DEFAULT 0,
    partial BLOB NOT NULL DEFAULT X'',
    partial_start INTEGER NOT NULL DEFAULT 0,
    current_cwd TEXT,
    current_project_id TEXT REFERENCES projects(project_id) ON DELETE SET NULL,
    message_mode TEXT NOT NULL DEFAULT 'unknown',
    monitoring INTEGER NOT NULL DEFAULT 1,
    health TEXT NOT NULL DEFAULT 'healthy',
    health_detail TEXT,
    malformed_count INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    last_observation_at REAL,
    last_reconciled_at REAL,
    UNIQUE(provider, session_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    title TEXT,
    current_cwd TEXT,
    last_activity_at REAL,
    last_source_at TEXT,
    last_kind TEXT,
    last_message_role TEXT,
    last_message_excerpt TEXT,
    last_turn_state TEXT,
    awaiting_completion INTEGER NOT NULL DEFAULT 0,
    awaiting_since REAL,
    PRIMARY KEY(project_id, provider, session_id)
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    source_at TEXT,
    observed_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS observations_session_time
ON observations(project_id, provider, session_id, observed_at);

CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    evidence_observation_id TEXT,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS findings_project_state
ON findings(project_id, state, seen, updated_at);

CREATE TABLE IF NOT EXISTS changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    observed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS review_jobs (
    job_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    analyzer_provider TEXT NOT NULL,
    analyzer_model TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    submitted_at REAL,
    packet_meta_json TEXT NOT NULL,
    summary TEXT,
    items_json TEXT NOT NULL DEFAULT '[]',
    limitations_json TEXT NOT NULL DEFAULT '[]',
    error TEXT
);

CREATE INDEX IF NOT EXISTS review_jobs_project_time
ON review_jobs(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS excluded_sessions (
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(provider, session_id)
);
"""


class ObserverDB:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(state_dir, 0o700)
        self.path = state_dir / "observer.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA)
        self.connection.commit()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ObserverDB":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def add_project(self, identity: ProjectIdentity) -> dict[str, Any]:
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO projects(
                    project_id, display_path, resolved_path, worktree_root, added_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id) DO UPDATE SET
                    display_path = excluded.display_path,
                    resolved_path = excluded.resolved_path,
                    worktree_root = excluded.worktree_root
                """,
                (
                    identity.project_id,
                    identity.display_path,
                    identity.resolved_path,
                    identity.worktree_root,
                    now,
                ),
            )
        return self.project(identity.project_id)

    def project(self, project_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            raise KeyError(project_id)
        return dict(row)

    def projects(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM projects ORDER BY added_at, project_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def remove_project(self, project_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM sources WHERE current_project_id = ?",
                (project_id,),
            )
            self.connection.execute(
                "DELETE FROM projects WHERE project_id = ?", (project_id,)
            )
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def mark_finding_seen(self, finding_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE findings SET seen = 1 WHERE finding_id = ?",
                (finding_id,),
            )
        return bool(cursor.rowcount)

    def dismiss_project_findings(self, project_id: str) -> bool:
        with self.connection:
            row = self.connection.execute(
                "SELECT 1 FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if not row:
                return False
            self.connection.execute(
                """
                UPDATE findings SET seen = 1
                WHERE project_id = ? AND state = 'open' AND seen = 0
                """,
                (project_id,),
            )
        return True

    def exclude_session(self, provider: str, session_id: str, reason: str) -> None:
        """Persistently keep an analyzer session out of collection and projection."""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO excluded_sessions(provider, session_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, session_id) DO UPDATE SET reason = excluded.reason
                """,
                (provider, session_id, reason, time.time()),
            )
            self.connection.execute(
                "DELETE FROM findings WHERE provider = ? AND session_id = ?",
                (provider, session_id),
            )
            self.connection.execute(
                "DELETE FROM sources WHERE provider = ? AND session_id = ?",
                (provider, session_id),
            )

    def include_session(self, provider: str, session_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM excluded_sessions WHERE provider = ? AND session_id = ?",
                (provider, session_id),
            )
        return bool(cursor.rowcount)

    def session_is_excluded(self, provider: str, session_id: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM excluded_sessions
            WHERE provider = ? AND session_id = ?
            """,
            (provider, session_id),
        ).fetchone()
        return row is not None

    def create_review_job(
        self,
        *,
        job_id: str,
        project_id: str,
        analyzer_provider: str,
        analyzer_model: str | None,
        created_at: float,
        packet_meta: dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO review_jobs(
                    job_id, project_id, analyzer_provider, analyzer_model,
                    status, created_at, packet_meta_json
                ) VALUES (?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    job_id,
                    project_id,
                    analyzer_provider,
                    analyzer_model,
                    created_at,
                    json.dumps(packet_meta, separators=(",", ":"), sort_keys=True),
                ),
            )

    def review_job(self, job_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"review job not found: {job_id}")
        return dict(row)

    def expire_prepared_reviews(self, cutoff: float) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE review_jobs SET status = 'expired', error = 'review packet expired'
                WHERE status = 'prepared' AND created_at < ?
                """,
                (cutoff,),
            )
        return int(cursor.rowcount)

    def submit_review_job(
        self,
        job_id: str,
        *,
        submitted_at: float,
        summary: str,
        items: list[dict[str, Any]],
        limitations: list[str],
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE review_jobs SET
                    status = 'current', submitted_at = ?, summary = ?,
                    items_json = ?, limitations_json = ?, error = NULL
                WHERE job_id = ? AND status = 'prepared'
                """,
                (
                    submitted_at,
                    summary,
                    json.dumps(items, separators=(",", ":"), sort_keys=True),
                    json.dumps(limitations, separators=(",", ":"), sort_keys=True),
                    job_id,
                ),
            )
        if not cursor.rowcount:
            raise ValueError("review job is not awaiting a submission")

    def latest_reviews(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT job.* FROM review_jobs AS job
            JOIN (
                SELECT project_id, MAX(created_at) AS newest
                FROM review_jobs
                WHERE status = 'current'
                GROUP BY project_id
            ) AS latest
              ON latest.project_id = job.project_id
             AND latest.newest = job.created_at
            WHERE job.status = 'current'
            ORDER BY job.project_id, job.job_id DESC
            """
        ).fetchall()
        reviews: dict[str, dict[str, Any]] = {}
        for row in rows:
            project_id = str(row["project_id"])
            if project_id in reviews:
                continue
            review = dict(row)
            review["packet_meta"] = json.loads(review.pop("packet_meta_json"))
            review["items"] = json.loads(review.pop("items_json"))
            review["limitations"] = json.loads(review.pop("limitations_json"))
            reviews[project_id] = review
        return reviews

    def source(self, source_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM sources WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    def sources(self, *, monitoring_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE monitoring = 1" if monitoring_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM sources {where} ORDER BY provider, session_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def register_source(
        self,
        *,
        source_id: str,
        provider: str,
        session_id: str,
        path: Path,
        current_cwd: str | None,
        project_id: str,
        message_mode: str,
    ) -> tuple[dict[str, Any], bool]:
        stat = path.stat()
        existing = self.source(source_id)
        created = existing is None
        with self.connection:
            if created:
                self.connection.execute(
                    """
                    INSERT INTO sources(
                        source_id, provider, session_id, path, device, inode,
                        current_cwd, current_project_id, message_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        provider,
                        session_id,
                        str(path),
                        stat.st_dev,
                        stat.st_ino,
                        current_cwd,
                        project_id,
                        message_mode,
                    ),
                )
            else:
                old_path = Path(str(existing["path"]))
                archival_move = not old_path.exists() and stat.st_size >= int(
                    existing["committed_offset"]
                )
                self.connection.execute(
                    """
                    UPDATE sources SET
                        path = ?,
                        device = CASE WHEN ? THEN ? ELSE device END,
                        inode = CASE WHEN ? THEN ? ELSE inode END,
                        current_cwd = COALESCE(?, current_cwd),
                        current_project_id = ?,
                        message_mode = CASE
                            WHEN message_mode = 'unknown' THEN ? ELSE message_mode END,
                        monitoring = 1,
                        health = 'healthy',
                        health_detail = NULL
                    WHERE source_id = ?
                    """,
                    (
                        str(path),
                        archival_move,
                        stat.st_dev,
                        archival_move,
                        stat.st_ino,
                        current_cwd,
                        project_id,
                        message_mode,
                        source_id,
                    ),
                )
        return self.source(source_id) or {}, created

    def set_branch(self, project_id: str, branch: str | None, now: float) -> None:
        project = self.project(project_id)
        old = project.get("current_branch")
        with self.connection:
            if old is not None and branch != old:
                self.connection.execute(
                    """
                    INSERT INTO changes(project_id, kind, old_value, new_value, observed_at)
                    VALUES (?, 'git_branch_changed', ?, ?, ?)
                    """,
                    (project_id, old, branch, now),
                )
            self.connection.execute(
                """
                UPDATE projects SET current_branch = ?, branch_sampled_at = ?
                WHERE project_id = ?
                """,
                (branch, now, project_id),
            )

    def set_session_title(
        self,
        provider: str,
        session_id: str,
        title: str,
        observed_at: float,
    ) -> int:
        rows = self.connection.execute(
            """
            SELECT project_id, title FROM sessions
            WHERE provider = ? AND session_id = ?
            """,
            (provider, session_id),
        ).fetchall()
        changed = [row for row in rows if row["title"] != title]
        if not changed:
            return 0
        with self.connection:
            for row in changed:
                if row["title"] is not None:
                    self.connection.execute(
                        """
                        INSERT INTO changes(
                            project_id, kind, old_value, new_value, observed_at
                        ) VALUES (?, 'session_title_changed', ?, ?, ?)
                        """,
                        (
                            row["project_id"],
                            row["title"],
                            title,
                            observed_at,
                        ),
                    )
            self.connection.execute(
                """
                UPDATE sessions SET title = ?
                WHERE provider = ? AND session_id = ?
                """,
                (title, provider, session_id),
            )
        return len(changed)

    def update_source_checkpoint(
        self,
        source_id: str,
        *,
        device: int,
        inode: int,
        generation: int,
        offset: int,
        partial: bytes,
        partial_start: int,
        current_cwd: str | None,
        current_project_id: str | None,
        message_mode: str,
        monitoring: bool,
        health: str,
        health_detail: str | None,
        malformed_increment: int,
        unknown_increment: int,
        observed_at: float | None,
        reconciled_at: float,
    ) -> None:
        self.connection.execute(
            """
            UPDATE sources SET
                device = ?, inode = ?, generation = ?, committed_offset = ?,
                partial = ?, partial_start = ?, current_cwd = ?,
                current_project_id = ?, message_mode = ?, monitoring = ?,
                health = ?, health_detail = ?,
                malformed_count = malformed_count + ?,
                unknown_count = unknown_count + ?,
                last_observation_at = COALESCE(?, last_observation_at),
                last_reconciled_at = ?
            WHERE source_id = ?
            """,
            (
                device,
                inode,
                generation,
                offset,
                partial,
                partial_start,
                current_cwd,
                current_project_id,
                message_mode,
                int(monitoring),
                health,
                health_detail,
                malformed_increment,
                unknown_increment,
                observed_at,
                reconciled_at,
                source_id,
            ),
        )

    def insert_event(
        self,
        *,
        observation_id: str,
        project_id: str,
        source: dict[str, Any],
        generation: int,
        byte_start: int,
        byte_end: int,
        event: NormalizedEvent,
        observed_at: float,
    ) -> bool:
        persisted_payload = dict(event.payload)
        if event.kind in {"user_message", "assistant_message"}:
            persisted_payload.pop("excerpt", None)
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO observations(
                observation_id, project_id, source_id, provider, session_id,
                generation, byte_start, byte_end, ordinal, kind, source_at,
                observed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                project_id,
                source["source_id"],
                source["provider"],
                source["session_id"],
                generation,
                byte_start,
                byte_end,
                event.ordinal,
                event.kind,
                event.source_at,
                observed_at,
                json.dumps(persisted_payload, separators=(",", ":"), sort_keys=True),
            ),
        )
        if not cursor.rowcount:
            return False
        event_at = _source_epoch(event.source_at) or observed_at
        self._project_session(project_id, source, event, event_at)
        self._project_finding(observation_id, project_id, source, event, event_at)
        return True

    def _project_session(
        self,
        project_id: str,
        source: dict[str, Any],
        event: NormalizedEvent,
        observed_at: float,
    ) -> None:
        role = None
        excerpt = None
        if event.kind in ("user_message", "assistant_message"):
            role = "user" if event.kind == "user_message" else "assistant"
            excerpt = str(event.payload.get("excerpt") or "")
        turn_state = {
            "turn_started": "started",
            "turn_completed": "completed",
            "turn_aborted": "aborted",
        }.get(event.kind)
        awaiting: int | None = None
        if event.kind in {
            "user_message",
            "turn_started",
            "tool_started",
            "tool_finished",
        }:
            awaiting = 1
        elif event.kind in {"assistant_message", "turn_completed", "turn_aborted"}:
            awaiting = 0
        meaningful = event.kind in MEANINGFUL_KINDS
        self.connection.execute(
            """
            INSERT INTO sessions(
                project_id, provider, session_id, source_id, current_cwd,
                last_activity_at, last_source_at, last_kind, last_message_role,
                last_message_excerpt, last_turn_state, awaiting_completion,
                awaiting_since
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, 0),
                      CASE WHEN ? = 1 THEN ? ELSE NULL END)
            ON CONFLICT(project_id, provider, session_id) DO UPDATE SET
                source_id = excluded.source_id,
                current_cwd = COALESCE(excluded.current_cwd, sessions.current_cwd),
                last_activity_at = CASE WHEN ? THEN excluded.last_activity_at
                    ELSE sessions.last_activity_at END,
                last_source_at = CASE WHEN ? THEN excluded.last_source_at
                    ELSE sessions.last_source_at END,
                last_kind = CASE WHEN ? THEN excluded.last_kind
                    ELSE sessions.last_kind END,
                last_message_role = COALESCE(excluded.last_message_role,
                    sessions.last_message_role),
                last_message_excerpt = COALESCE(excluded.last_message_excerpt,
                    sessions.last_message_excerpt),
                last_turn_state = COALESCE(excluded.last_turn_state,
                    sessions.last_turn_state),
                awaiting_completion = COALESCE(?, sessions.awaiting_completion),
                awaiting_since = CASE
                    WHEN ? = 1 THEN COALESCE(sessions.awaiting_since, ?)
                    WHEN ? = 0 THEN NULL
                    ELSE sessions.awaiting_since END
            """,
            (
                project_id,
                source["provider"],
                source["session_id"],
                source["source_id"],
                source.get("current_cwd"),
                observed_at if meaningful else None,
                event.source_at,
                event.kind,
                role,
                excerpt,
                turn_state,
                awaiting,
                awaiting,
                observed_at,
                meaningful,
                meaningful,
                meaningful,
                awaiting,
                awaiting,
                observed_at,
                awaiting,
            ),
        )
        if event.kind == "session_title_changed":
            self.connection.execute(
                """
                UPDATE sessions SET title = ?
                WHERE project_id = ? AND provider = ? AND session_id = ?
                """,
                (
                    event.payload.get("title"),
                    project_id,
                    source["provider"],
                    source["session_id"],
                ),
            )

    def _project_finding(
        self,
        observation_id: str,
        project_id: str,
        source: dict[str, Any],
        event: NormalizedEvent,
        observed_at: float,
    ) -> None:
        provider = str(source["provider"])
        session_id = str(source["session_id"])
        if event.kind in MEANINGFUL_KINDS:
            self.connection.execute(
                """
                UPDATE findings SET state = 'superseded', updated_at = ?
                WHERE project_id = ? AND provider = ? AND session_id = ?
                  AND kind = 'no_completion_observed' AND state = 'open'
                """,
                (observed_at, project_id, provider, session_id),
            )
        if event.kind == "user_message":
            self.connection.execute(
                """
                UPDATE findings SET state = 'superseded', updated_at = ?
                WHERE project_id = ? AND provider = ? AND session_id = ?
                  AND kind = 'turn_completed' AND state = 'open'
                """,
                (observed_at, project_id, provider, session_id),
            )
            return

        if event.kind == "decision_requested":
            request_id = str(event.payload.get("request_id") or observation_id)
            item_id = str(event.payload.get("item_id") or "0")
            finding_id = f"decision:{provider}:{session_id}:{request_id}"
            row = self.connection.execute(
                "SELECT details_json FROM findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            details = json.loads(row[0]) if row else {"items": {}}
            details["items"][item_id] = {
                "question": event.payload.get("question"),
                "options": event.payload.get("options", []),
                "state": "open",
            }
            self.connection.execute(
                """
                INSERT INTO findings(
                    finding_id, project_id, provider, session_id, kind, state,
                    created_at, updated_at, evidence_observation_id, summary,
                    details_json
                ) VALUES (?, ?, ?, ?, 'decision_requested', 'open', ?, ?, ?, ?, ?)
                ON CONFLICT(finding_id) DO UPDATE SET
                    updated_at = excluded.updated_at,
                    evidence_observation_id = excluded.evidence_observation_id,
                    summary = excluded.summary,
                    details_json = excluded.details_json
                """,
                (
                    finding_id,
                    project_id,
                    provider,
                    session_id,
                    observed_at,
                    observed_at,
                    observation_id,
                    str(event.payload.get("question") or "Decision requested")[:4096],
                    json.dumps(details, separators=(",", ":"), sort_keys=True),
                ),
            )
            return

        if event.kind == "decision_response":
            request_id = str(event.payload.get("request_id") or "")
            finding_id = f"decision:{provider}:{session_id}:{request_id}"
            row = self.connection.execute(
                "SELECT details_json FROM findings WHERE finding_id = ?",
                (finding_id,),
            ).fetchone()
            if not row:
                return
            details = json.loads(row[0])
            for item_id, answer in dict(event.payload.get("answers") or {}).items():
                item = details.get("items", {}).get(str(item_id))
                if item is not None:
                    item["state"] = "resolved"
                    item["answer"] = str(answer)[:4096]
            states = [item["state"] for item in details.get("items", {}).values()]
            state = (
                "resolved"
                if states and all(value == "resolved" for value in states)
                else "open"
            )
            self.connection.execute(
                """
                UPDATE findings SET state = ?, updated_at = ?, details_json = ?
                WHERE finding_id = ?
                """,
                (
                    state,
                    observed_at,
                    json.dumps(details, separators=(",", ":"), sort_keys=True),
                    finding_id,
                ),
            )
            return

        if event.kind not in {"turn_completed", "turn_aborted"}:
            return
        turn_id = str(event.payload.get("turn_id") or observation_id)
        finding_id = f"{event.kind}:{provider}:{session_id}:{turn_id}"
        summary = str(
            event.payload.get("excerpt")
            or ("Turn completed" if event.kind == "turn_completed" else "Turn aborted")
        )[:4096]
        if event.kind == "turn_completed" and summary == "Turn completed":
            row = self.connection.execute(
                """
                SELECT last_message_excerpt FROM sessions
                WHERE project_id = ? AND provider = ? AND session_id = ?
                """,
                (project_id, provider, session_id),
            ).fetchone()
            if row and row[0]:
                summary = str(row[0])[:4096]
        self.connection.execute(
            """
            INSERT OR IGNORE INTO findings(
                finding_id, project_id, provider, session_id, kind, state,
                created_at, updated_at, evidence_observation_id, summary
            ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
            """,
            (
                finding_id,
                project_id,
                provider,
                session_id,
                event.kind,
                observed_at,
                observed_at,
                observation_id,
                summary,
            ),
        )

    def status(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        reviews = self.latest_reviews()
        projects: list[dict[str, Any]] = []
        for project in self.projects():
            sessions = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT * FROM sessions WHERE project_id = ?
                    ORDER BY last_activity_at DESC, provider, session_id
                    """,
                    (project["project_id"],),
                ).fetchall()
            ]
            for session in sessions:
                last = session.get("last_activity_at")
                session["activity_age_seconds"] = now - last if last else None
            findings = [
                {
                    **dict(row),
                    "details": json.loads(row["details_json"]),
                }
                for row in self.connection.execute(
                    """
                    SELECT * FROM findings
                    WHERE project_id = ? AND state = 'open'
                    ORDER BY seen, updated_at DESC
                    """,
                    (project["project_id"],),
                ).fetchall()
            ]
            sources = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM sources WHERE current_project_id = ?",
                    (project["project_id"],),
                ).fetchall()
            ]
            for source in sources:
                source.pop("partial", None)
            changes = [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT * FROM changes WHERE project_id = ?
                    ORDER BY observed_at DESC LIMIT 20
                    """,
                    (project["project_id"],),
                ).fetchall()
            ]
            projects.append(
                {
                    **project,
                    "sessions": sessions,
                    "findings": findings,
                    "sources": sources,
                    "changes": changes,
                    "review": reviews.get(str(project["project_id"])),
                }
            )
        return {"generated_at": now, "projects": projects}
