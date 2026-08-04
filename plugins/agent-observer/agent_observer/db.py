from __future__ import annotations

import json
import hashlib
import hmac
import os
import secrets
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
    error TEXT,
    source_id TEXT REFERENCES sources(source_id) ON DELETE CASCADE,
    source_generation INTEGER,
    source_offset INTEGER,
    input_hash TEXT,
    lease_epoch INTEGER
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

CREATE TABLE IF NOT EXISTS supervisor_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    epoch INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    provider TEXT,
    model TEXT,
    session_id TEXT,
    state TEXT NOT NULL DEFAULT 'detached',
    acquired_at REAL,
    heartbeat_at REAL,
    current_job_id TEXT,
    stop_reason TEXT,
    allow_cross_provider INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO supervisor_state(singleton, epoch, state)
VALUES (1, 0, 'detached');

CREATE TABLE IF NOT EXISTS analysis_cursors (
    source_id TEXT PRIMARY KEY REFERENCES sources(source_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    committed_offset INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    last_message_ref TEXT,
    job_id TEXT,
    accepted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_invites (
    secret_hash TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);

CREATE TABLE IF NOT EXISTS remote_nodes (
    node_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    provider TEXT,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_seen_at REAL,
    revoked_at REAL,
    upload_epoch INTEGER NOT NULL DEFAULT 0,
    last_revision INTEGER NOT NULL DEFAULT 0,
    snapshot_hash TEXT,
    snapshot_json TEXT,
    snapshot_generated_at REAL,
    snapshot_received_at REAL
);

CREATE TABLE IF NOT EXISTS remote_project_state (
    node_id TEXT NOT NULL REFERENCES remote_nodes(node_id) ON DELETE CASCADE,
    remote_project_id TEXT NOT NULL,
    dismissed_fingerprint TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(node_id, remote_project_id)
);
"""


SUPERVISOR_STALE_SECONDS = 120.0


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
        self._migrate_schema()
        self.connection.commit()
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _secret_hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def __enter__(self) -> "ObserverDB":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def create_remote_invite(self, secret: str, expires_at: float) -> None:
        now = time.time()
        with self.connection:
            self.connection.execute(
                "DELETE FROM remote_invites WHERE expires_at < ? OR used_at IS NOT NULL",
                (now,),
            )
            self.connection.execute(
                """
                INSERT INTO remote_invites(secret_hash, created_at, expires_at)
                VALUES (?, ?, ?)
                """,
                (self._secret_hash(secret), now, expires_at),
            )

    def accept_remote_invite(
        self,
        secret: str,
        *,
        display_name: str,
        provider: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        secret_hash = self._secret_hash(secret)
        token = secrets.token_urlsafe(32)
        node_id = f"node_{secrets.token_hex(10)}"
        with self.connection:
            invite = self.connection.execute(
                "SELECT * FROM remote_invites WHERE secret_hash = ?",
                (secret_hash,),
            ).fetchone()
            if (
                invite is None
                or invite["used_at"] is not None
                or float(invite["expires_at"]) <= now
            ):
                raise ValueError("remote enrollment key is invalid, expired, or already used")
            updated = self.connection.execute(
                """
                UPDATE remote_invites SET used_at = ?
                WHERE secret_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (now, secret_hash, now),
            )
            if updated.rowcount != 1:
                raise ValueError("remote enrollment key is invalid, expired, or already used")
            self.connection.execute(
                """
                INSERT INTO remote_nodes(
                    node_id, display_name, provider, token_hash, created_at,
                    last_seen_at, upload_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    node_id,
                    display_name.strip()[:160] or "remote observer",
                    provider,
                    self._secret_hash(token),
                    now,
                    now,
                ),
            )
        return {
            "node_id": node_id,
            "token": token,
            "upload_epoch": 1,
            "accepted_at": now,
        }

    def authenticate_remote_node(self, token: str) -> dict[str, Any]:
        digest = self._secret_hash(token)
        row = self.connection.execute(
            "SELECT * FROM remote_nodes WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise ValueError("remote node credential is invalid or revoked")
        # Keep a constant-time comparison even though the indexed digest selected
        # the row; this avoids making later authentication refactors surprising.
        if not hmac.compare_digest(str(row["token_hash"]), digest):
            raise ValueError("remote node credential is invalid or revoked")
        return dict(row)

    def claim_remote_uploader(self, token: str) -> dict[str, Any]:
        node = self.authenticate_remote_node(token)
        now = time.time()
        with self.connection:
            self.connection.execute(
                """
                UPDATE remote_nodes
                SET upload_epoch = upload_epoch + 1, last_seen_at = ?
                WHERE node_id = ? AND revoked_at IS NULL
                """,
                (now, node["node_id"]),
            )
        claimed = self.connection.execute(
            "SELECT * FROM remote_nodes WHERE node_id = ?",
            (node["node_id"],),
        ).fetchone()
        assert claimed is not None
        return {
            "node_id": str(claimed["node_id"]),
            "display_name": str(claimed["display_name"]),
            "upload_epoch": int(claimed["upload_epoch"]),
            "claimed_at": now,
        }

    def import_remote_snapshot(
        self,
        token: str,
        snapshot: dict[str, Any],
        *,
        canonical_json: str,
        snapshot_hash: str,
    ) -> dict[str, Any]:
        node = self.authenticate_remote_node(token)
        node_id = str(snapshot["node_id"])
        if node_id != node["node_id"]:
            raise ValueError("snapshot node identity does not match its credential")
        epoch = int(snapshot["upload_epoch"])
        revision = int(snapshot["revision"])
        now = time.time()
        current_epoch = int(node["upload_epoch"])
        current_revision = int(node["last_revision"])
        if epoch != current_epoch:
            raise ValueError("remote uploader lease was superseded")
        if revision < current_revision:
            raise ValueError("remote snapshot revision is stale")
        if revision == current_revision:
            if hmac.compare_digest(str(node["snapshot_hash"] or ""), snapshot_hash):
                return {
                    "node_id": node_id,
                    "upload_epoch": epoch,
                    "revision": revision,
                    "duplicate": True,
                    "received_at": node["snapshot_received_at"],
                }
            raise ValueError("remote snapshot revision conflicts with stored content")
        last_received = float(node.get("snapshot_received_at") or 0)
        if last_received and now - last_received < 0.2:
            raise ValueError("remote snapshot rate limit exceeded")
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE remote_nodes
                SET display_name = ?, provider = ?, last_seen_at = ?,
                    last_revision = ?, snapshot_hash = ?, snapshot_json = ?,
                    snapshot_generated_at = ?, snapshot_received_at = ?
                WHERE node_id = ? AND revoked_at IS NULL
                  AND upload_epoch = ? AND last_revision < ?
                """,
                (
                    str(snapshot["display_name"])[:160],
                    snapshot.get("provider"),
                    now,
                    revision,
                    snapshot_hash,
                    canonical_json,
                    float(snapshot["generated_at"]),
                    now,
                    node_id,
                    epoch,
                    revision,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("remote snapshot lost an uploader or revision race")
        return {
            "node_id": node_id,
            "upload_epoch": epoch,
            "revision": revision,
            "duplicate": False,
            "received_at": now,
        }

    def remote_nodes(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT node_id, display_name, provider, created_at, last_seen_at,
                   revoked_at, upload_epoch, last_revision, snapshot_generated_at,
                   snapshot_received_at
            FROM remote_nodes ORDER BY created_at, node_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_remote_node(self, node_id: str) -> dict[str, Any]:
        now = time.time()
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE remote_nodes SET revoked_at = ?, upload_epoch = upload_epoch + 1
                WHERE node_id = ? AND revoked_at IS NULL
                """,
                (now, node_id),
            )
        if updated.rowcount != 1:
            raise KeyError(f"active remote node not found: {node_id}")
        return {"node_id": node_id, "revoked_at": now}

    @staticmethod
    def _remote_key(node_id: str, value: Any) -> str:
        return f"remote:{node_id}:{value}"

    @staticmethod
    def _finding_fingerprint(findings: list[dict[str, Any]]) -> str:
        current = sorted(
            (
                str(item.get("finding_id") or ""),
                float(item.get("updated_at") or 0),
            )
            for item in findings
            if item.get("state") == "open" and not item.get("seen")
        )
        return hashlib.sha256(
            json.dumps(current, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def remote_status(self, now: float | None = None) -> dict[str, Any]:
        moment = now or time.time()
        rows = self.connection.execute(
            "SELECT * FROM remote_nodes ORDER BY created_at, node_id"
        ).fetchall()
        projects: list[dict[str, Any]] = []
        nodes: list[dict[str, Any]] = []
        for row in rows:
            node = dict(row)
            last_seen = float(node.get("last_seen_at") or 0)
            transport_state = (
                "revoked"
                if node.get("revoked_at") is not None
                else "connected"
                if last_seen and moment - last_seen <= 20
                else "disconnected"
            )
            public_node = {
                key: node.get(key)
                for key in (
                    "node_id",
                    "display_name",
                    "provider",
                    "created_at",
                    "last_seen_at",
                    "revoked_at",
                    "upload_epoch",
                    "last_revision",
                    "snapshot_generated_at",
                    "snapshot_received_at",
                )
            }
            public_node["transport_state"] = transport_state
            snapshot_generated = float(node.get("snapshot_generated_at") or 0)
            snapshot_received = float(node.get("snapshot_received_at") or 0)
            public_node["clock_skew_seconds"] = (
                snapshot_received - snapshot_generated
                if snapshot_generated and snapshot_received
                else None
            )
            nodes.append(public_node)
            raw = node.get("snapshot_json")
            if not raw:
                continue
            try:
                snapshot = json.loads(str(raw))
                projection = snapshot["projection"]
                remote_projects = projection["projects"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            public_node["analyzer"] = snapshot.get("analyzer")
            if not isinstance(remote_projects, list):
                continue
            for source_project in remote_projects:
                if not isinstance(source_project, dict):
                    continue
                project = json.loads(json.dumps(source_project))
                remote_project_id = str(project.get("project_id") or "")
                if not remote_project_id:
                    continue
                project_id = self._remote_key(str(node["node_id"]), remote_project_id)
                project["project_id"] = project_id
                project["remote_project_id"] = remote_project_id
                project["origin"] = "remote"
                project["node"] = public_node
                sessions = project.get("sessions")
                project["sessions"] = sessions if isinstance(sessions, list) else []
                for session in project["sessions"]:
                    if not isinstance(session, dict):
                        continue
                    session["project_id"] = project_id
                    if session.get("source_id"):
                        session["source_id"] = self._remote_key(
                            str(node["node_id"]), session["source_id"]
                        )
                    last_activity = session.get("last_activity_at")
                    source_activity = float(last_activity) if last_activity else None
                    skew = public_node["clock_skew_seconds"]
                    corrected_activity = (
                        source_activity + float(skew)
                        if source_activity is not None and skew is not None
                        else source_activity
                    )
                    session["source_activity_at"] = source_activity
                    session["last_activity_at"] = corrected_activity
                    session["activity_age_seconds"] = (
                        max(0.0, moment - corrected_activity)
                        if corrected_activity is not None
                        else None
                    )
                findings = project.get("findings")
                project["findings"] = findings if isinstance(findings, list) else []
                fingerprint = self._finding_fingerprint(project["findings"])
                state = self.connection.execute(
                    """
                    SELECT dismissed_fingerprint FROM remote_project_state
                    WHERE node_id = ? AND remote_project_id = ?
                    """,
                    (node["node_id"], remote_project_id),
                ).fetchone()
                dismissed = bool(
                    fingerprint
                    and state
                    and hmac.compare_digest(
                        str(state["dismissed_fingerprint"] or ""), fingerprint
                    )
                )
                for finding in project["findings"]:
                    if not isinstance(finding, dict):
                        continue
                    if finding.get("finding_id"):
                        finding["finding_id"] = self._remote_key(
                            str(node["node_id"]), finding["finding_id"]
                        )
                    finding["project_id"] = project_id
                    if finding.get("evidence_observation_id"):
                        finding["evidence_observation_id"] = self._remote_key(
                            str(node["node_id"]), finding["evidence_observation_id"]
                        )
                    if dismissed and finding.get("state") == "open":
                        finding["seen"] = 1
                project["remote_attention_dismissed"] = dismissed
                sources = project.get("sources")
                project["sources"] = sources if isinstance(sources, list) else []
                for source in project["sources"]:
                    if not isinstance(source, dict):
                        continue
                    if source.get("source_id"):
                        source["source_id"] = self._remote_key(
                            str(node["node_id"]), source["source_id"]
                        )
                    source["current_project_id"] = project_id
                review = project.get("review")
                if isinstance(review, dict):
                    if review.get("job_id"):
                        review["job_id"] = self._remote_key(
                            str(node["node_id"]), review["job_id"]
                        )
                    review["project_id"] = project_id
                    if review.get("source_id"):
                        review["source_id"] = self._remote_key(
                            str(node["node_id"]), review["source_id"]
                        )
                    packet_meta = review.get("packet_meta")
                    if isinstance(packet_meta, dict):
                        coverage = packet_meta.get("coverage")
                        if isinstance(coverage, dict):
                            for checkpoint in coverage.get("source_checkpoints") or []:
                                if isinstance(checkpoint, dict) and checkpoint.get("source_id"):
                                    checkpoint["source_id"] = self._remote_key(
                                        str(node["node_id"]), checkpoint["source_id"]
                                    )
                projects.append(project)
        return {"projects": projects, "nodes": nodes}

    def dismiss_remote_project_findings(
        self, node_id: str, remote_project_id: str
    ) -> bool:
        row = self.connection.execute(
            "SELECT snapshot_json FROM remote_nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None or not row["snapshot_json"]:
            return False
        try:
            snapshot = json.loads(str(row["snapshot_json"]))
            project = next(
                item
                for item in snapshot["projection"]["projects"]
                if str(item.get("project_id")) == remote_project_id
            )
        except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
            return False
        findings = project.get("findings")
        if not isinstance(findings, list):
            findings = []
        fingerprint = self._finding_fingerprint(findings)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO remote_project_state(
                    node_id, remote_project_id, dismissed_fingerprint, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id, remote_project_id) DO UPDATE SET
                    dismissed_fingerprint = excluded.dismissed_fingerprint,
                    updated_at = excluded.updated_at
                """,
                (node_id, remote_project_id, fingerprint, time.time()),
            )
        return True

    def _migrate_schema(self) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(review_jobs)")
        }
        additions = {
            "source_id": "TEXT REFERENCES sources(source_id) ON DELETE CASCADE",
            "source_generation": "INTEGER",
            "source_offset": "INTEGER",
            "input_hash": "TEXT",
            "lease_epoch": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.connection.execute(
                    f"ALTER TABLE review_jobs ADD COLUMN {name} {declaration}"
                )

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

    def acquire_supervisor(
        self,
        *,
        provider: str,
        model: str | None,
        session_id: str | None,
        allow_cross_provider: bool,
    ) -> dict[str, Any]:
        now = time.time()
        token = secrets.token_urlsafe(32)
        with self.connection:
            row = self.connection.execute(
                "SELECT epoch FROM supervisor_state WHERE singleton = 1"
            ).fetchone()
            epoch = int(row["epoch"] if row else 0) + 1
            self.connection.execute(
                """
                UPDATE review_jobs
                SET status = 'superseded', error = 'analyzer lease superseded'
                WHERE status = 'prepared' AND lease_epoch IS NOT NULL
                """
            )
            self.connection.execute(
                """
                UPDATE supervisor_state SET
                    epoch = ?, lease_token = ?, provider = ?, model = ?,
                    session_id = ?, state = 'attached', acquired_at = ?,
                    heartbeat_at = ?, current_job_id = NULL, stop_reason = NULL,
                    allow_cross_provider = ?
                WHERE singleton = 1
                """,
                (
                    epoch,
                    token,
                    provider,
                    model,
                    session_id,
                    now,
                    now,
                    int(allow_cross_provider),
                ),
            )
        return {**self.supervisor_status(), "lease_token": token}

    def supervisor_status(self, *, now: float | None = None) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM supervisor_state WHERE singleton = 1"
        ).fetchone()
        if not row:
            return {"state": "detached", "epoch": 0}
        result = dict(row)
        result.pop("lease_token", None)
        result["lease_epoch"] = result["epoch"]
        result["allow_cross_provider"] = bool(result["allow_cross_provider"])
        heartbeat = result.get("heartbeat_at")
        if (
            result.get("state") in {"attached", "waiting", "analyzing"}
            and heartbeat is not None
            and (now or time.time()) - float(heartbeat) > SUPERVISOR_STALE_SECONDS
        ):
            result["state"] = "detached"
            result["stop_reason"] = "analyzer heartbeat expired"
        queued = self.connection.execute(
            "SELECT COUNT(*) FROM review_jobs WHERE status = 'prepared'"
        ).fetchone()
        result["queued_jobs"] = int(queued[0] if queued else 0)
        eligible = self.connection.execute(
            """
            SELECT COUNT(*) FROM sources AS source
            LEFT JOIN analysis_cursors AS cursor
              ON cursor.source_id = source.source_id
            WHERE source.monitoring = 1
              AND source.current_project_id IS NOT NULL
              AND (
                cursor.source_id IS NULL
                OR cursor.generation != source.generation
                OR cursor.committed_offset != source.committed_offset
              )
            """
        ).fetchone()
        result["queued_sessions"] = int(eligible[0] if eligible else 0)
        coalesced = self.connection.execute(
            """
            SELECT COUNT(*) FROM review_jobs AS job
            JOIN sources AS source ON source.source_id = job.source_id
            WHERE job.job_id = ?
              AND job.status = 'prepared'
              AND (
                source.generation != job.source_generation
                OR source.committed_offset != job.source_offset
              )
            """,
            (result.get("current_job_id"),),
        ).fetchone()
        result["coalesced_sessions"] = int(coalesced[0] if coalesced else 0)
        latest = self.connection.execute(
            "SELECT MAX(submitted_at) FROM review_jobs WHERE status = 'current'"
        ).fetchone()
        result["last_accepted_review_at"] = latest[0] if latest else None
        return result

    def _current_supervisor(self, lease_token: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supervisor_state WHERE singleton = 1"
        ).fetchone()
        if not row or not row["lease_token"] or not secrets.compare_digest(
            str(row["lease_token"]), lease_token
        ):
            raise ValueError("analyzer lease was superseded or stopped")
        return row

    def heartbeat_supervisor(
        self,
        lease_token: str,
        *,
        state: str,
        current_job_id: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"attached", "waiting", "analyzing"}:
            raise ValueError("invalid analyzer state")
        with self.connection:
            row = self._current_supervisor(lease_token)
            self.connection.execute(
                """
                UPDATE supervisor_state
                SET state = ?, heartbeat_at = ?, current_job_id = ?, stop_reason = NULL
                WHERE singleton = 1 AND epoch = ?
                """,
                (state, time.time(), current_job_id, int(row["epoch"])),
            )
        return self.supervisor_status()

    def revoke_supervisor(self, reason: str = "stopped by user") -> dict[str, Any]:
        with self.connection:
            row = self.connection.execute(
                "SELECT epoch FROM supervisor_state WHERE singleton = 1"
            ).fetchone()
            epoch = int(row["epoch"] if row else 0) + 1
            self.connection.execute(
                """
                UPDATE review_jobs SET status = 'superseded', error = ?
                WHERE status = 'prepared' AND lease_epoch IS NOT NULL
                """,
                (reason,),
            )
            self.connection.execute(
                """
                UPDATE supervisor_state SET
                    epoch = ?, lease_token = NULL, state = 'detached',
                    heartbeat_at = ?, current_job_id = NULL, stop_reason = ?
                WHERE singleton = 1
                """,
                (epoch, time.time(), reason),
            )
        return self.supervisor_status()

    def supervisor_lease(self, lease_token: str) -> dict[str, Any]:
        return dict(self._current_supervisor(lease_token))

    def analysis_cursor(self, source_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM analysis_cursors WHERE source_id = ?", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    def advance_analysis_cursor(
        self,
        *,
        source_id: str,
        generation: int,
        committed_offset: int,
        input_hash: str,
        last_message_ref: str | None,
        job_id: str | None,
        accepted_at: float | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO analysis_cursors(
                    source_id, generation, committed_offset, input_hash,
                    last_message_ref, job_id, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    generation = excluded.generation,
                    committed_offset = excluded.committed_offset,
                    input_hash = excluded.input_hash,
                    last_message_ref = excluded.last_message_ref,
                    job_id = excluded.job_id,
                    accepted_at = excluded.accepted_at
                """,
                (
                    source_id,
                    generation,
                    committed_offset,
                    input_hash,
                    last_message_ref,
                    job_id,
                    accepted_at or time.time(),
                ),
            )

    def create_review_job(
        self,
        *,
        job_id: str,
        project_id: str,
        analyzer_provider: str,
        analyzer_model: str | None,
        created_at: float,
        packet_meta: dict[str, Any],
        source_id: str | None = None,
        source_generation: int | None = None,
        source_offset: int | None = None,
        input_hash: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO review_jobs(
                    job_id, project_id, analyzer_provider, analyzer_model,
                    status, created_at, packet_meta_json, source_id,
                    source_generation, source_offset, input_hash, lease_epoch
                ) VALUES (?, ?, ?, ?, 'prepared', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    analyzer_provider = excluded.analyzer_provider,
                    analyzer_model = excluded.analyzer_model,
                    status = 'prepared',
                    created_at = excluded.created_at,
                    submitted_at = NULL,
                    packet_meta_json = excluded.packet_meta_json,
                    summary = NULL,
                    items_json = '[]',
                    limitations_json = '[]',
                    error = NULL,
                    source_id = excluded.source_id,
                    source_generation = excluded.source_generation,
                    source_offset = excluded.source_offset,
                    input_hash = excluded.input_hash,
                    lease_epoch = excluded.lease_epoch
                """,
                (
                    job_id,
                    project_id,
                    analyzer_provider,
                    analyzer_model,
                    created_at,
                    json.dumps(packet_meta, separators=(",", ":"), sort_keys=True),
                    source_id,
                    source_generation,
                    source_offset,
                    input_hash,
                    lease_epoch,
                ),
            )

    def review_job(self, job_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"review job not found: {job_id}")
        return dict(row)

    def update_review_packet_meta(
        self, job_id: str, packet_meta: dict[str, Any]
    ) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE review_jobs SET packet_meta_json = ?
                WHERE job_id = ? AND status = 'prepared'
                """,
                (
                    json.dumps(packet_meta, separators=(",", ":"), sort_keys=True),
                    job_id,
                ),
            )
        if not cursor.rowcount:
            raise ValueError("review job is not awaiting a packet update")

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

    def submit_supervised_review_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        submitted_at: float,
        summary: str,
        items: list[dict[str, Any]],
        limitations: list[str],
        last_message_ref: str | None,
    ) -> None:
        with self.connection:
            lease = self._current_supervisor(lease_token)
            job = self.connection.execute(
                "SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not job or job["status"] != "prepared":
                raise ValueError("review job is not awaiting a submission")
            if int(job["lease_epoch"] or -1) != int(lease["epoch"]):
                raise ValueError("review job belongs to a superseded analyzer lease")
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
            self.connection.execute(
                """
                INSERT INTO analysis_cursors(
                    source_id, generation, committed_offset, input_hash,
                    last_message_ref, job_id, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    generation = excluded.generation,
                    committed_offset = excluded.committed_offset,
                    input_hash = excluded.input_hash,
                    last_message_ref = excluded.last_message_ref,
                    job_id = excluded.job_id,
                    accepted_at = excluded.accepted_at
                """,
                (
                    str(job["source_id"]),
                    int(job["source_generation"]),
                    int(job["source_offset"]),
                    str(job["input_hash"]),
                    last_message_ref,
                    job_id,
                    submitted_at,
                ),
            )
            self.connection.execute(
                """
                UPDATE supervisor_state
                SET state = 'waiting', heartbeat_at = ?, current_job_id = NULL
                WHERE singleton = 1 AND epoch = ?
                """,
                (submitted_at, int(lease["epoch"])),
            )

    def latest_reviews(self) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM review_jobs
            WHERE status = 'current'
            ORDER BY project_id, created_at DESC, job_id DESC
            """
        ).fetchall()
        newest: dict[str, dict[str, Any]] = {}
        with_items: dict[str, dict[str, Any]] = {}
        seen_sources: dict[str, set[str]] = {}
        for row in rows:
            project_id = str(row["project_id"])
            source_key = str(row["source_id"] or "__manual__")
            project_sources = seen_sources.setdefault(project_id, set())
            if source_key in project_sources:
                continue
            project_sources.add(source_key)
            review = dict(row)
            review["packet_meta"] = json.loads(review.pop("packet_meta_json"))
            review["items"] = json.loads(review.pop("items_json"))
            review["limitations"] = json.loads(review.pop("limitations_json"))
            newest.setdefault(project_id, review)
            if review["items"]:
                with_items.setdefault(project_id, review)
        return {
            project_id: with_items.get(project_id, review)
            for project_id, review in newest.items()
        }

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
