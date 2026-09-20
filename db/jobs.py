"""SQLite job registry. Public operations are synchronous.

Call from async code with ``run_blocking(store.method, ...)``. Each operation
uses its own connection and transaction, so connections never cross threads.
This module manages records only; it never deletes download workspaces.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_DATABASE_PATH = Path("/app/config/dndeezer/jobs.sqlite3")
JobState = Literal[
    "queued", "downloading", "completed", "failed", "cancelled", "interrupted", "cleaned"
]
_STATES = frozenset(JobState.__args__)


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_name: str
    task_id: str
    backend_id: str
    payload: str
    mount_root: Path
    workspace_path: Path
    state: JobState
    file_paths: tuple[Path, ...]
    error: str | None
    created_at: float
    updated_at: float
    cleaned_at: float | None


class JobStore:
    """Durable records keyed by the exact host handle's job name.

    Call initialize before use. Record creation must precede starting a job.
    Only mark_cleaned after the caller has verified workspace removal.
    Cleaned records remain as tombstones until explicitly pruned.
    """

    def __init__(self, path: Path = DEFAULT_DATABASE_PATH) -> None:
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported job database version: {version}")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_name TEXT PRIMARY KEY NOT NULL,
                    task_id TEXT NOT NULL,
                    backend_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    mount_root TEXT NOT NULL,
                    workspace_path TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN (
                        'queued', 'downloading', 'completed', 'failed',
                        'cancelled', 'interrupted', 'cleaned'
                    )),
                    file_paths_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    cleaned_at REAL,
                    CHECK ((state = 'cleaned') = (cleaned_at IS NOT NULL))
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_cleaned_at ON jobs(cleaned_at)"
            )
            connection.execute("PRAGMA user_version = 1")

    def create(
        self, *, job_name: str, task_id: str, backend_id: str, payload: str,
        mount_root: Path, workspace_path: Path,
    ) -> JobRecord:
        if not all((job_name, task_id, backend_id, payload)):
            raise ValueError("Job identity and payload must not be empty")
        root, workspace = Path(mount_root), Path(workspace_path)
        if not root.is_absolute() or not workspace.is_absolute():
            raise ValueError("Job paths must be absolute")
        if (
            root == Path(root.anchor) or workspace == root
            or not workspace.is_relative_to(root)
            or ".." in root.parts or ".." in workspace.parts
        ):
            raise ValueError("Workspace must be strictly inside the downloads root")
        now = time.time()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO jobs (
                    job_name, task_id, backend_id, payload, mount_root,
                    workspace_path, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (job_name, task_id, backend_id, payload, str(root), str(workspace), now, now),
            )
            return self._require(connection, job_name)

    def get(self, job_name: str) -> JobRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_name = ?", (job_name,)
            ).fetchone()
            return self._decode(row) if row else None

    def list_jobs(self, *, state: JobState | None = None) -> list[JobRecord]:
        if state is not None and state not in _STATES:
            raise ValueError(f"Unknown job state: {state}")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE (? IS NULL OR state = ?) ORDER BY created_at, job_name",
                (state, state),
            ).fetchall()
            return [self._decode(row) for row in rows]

    def update(
        self, job_name: str, *, state: JobState,
        file_paths: tuple[Path, ...] | None = None, error: str | None = None,
    ) -> JobRecord:
        """Update lifecycle evidence; cleaned records cannot be resurrected."""
        if state not in _STATES or state == "cleaned":
            raise ValueError("Use a lifecycle state; use mark_cleaned for cleanup")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, job_name)
            if current.state == "cleaned":
                raise ValueError("Cannot update a cleaned job")
            paths = current.file_paths if file_paths is None else tuple(map(Path, file_paths))
            for path in paths:
                if (
                    not path.is_absolute() or ".." in path.parts
                    or path == current.workspace_path
                    or not path.is_relative_to(current.workspace_path)
                ):
                    raise ValueError("Completed file must be inside the owned workspace")
            connection.execute(
                """UPDATE jobs SET state = ?, file_paths_json = ?, error = ?,
                    updated_at = ? WHERE job_name = ?""",
                (state, json.dumps([str(p) for p in paths]), error, time.time(), job_name),
            )
            return self._require(connection, job_name)

    def recover_interrupted(self) -> int:
        """Call once at startup, before new workers run, not during live reloads.

        This assumes no other plugin instance is still executing these jobs.
        Completed evidence and cleaned tombstones are preserved.
        """
        with self._connection() as connection:
            return connection.execute(
                """UPDATE jobs SET state = 'interrupted',
                    error = 'Download interrupted by plugin restart', updated_at = ?
                    WHERE state IN ('queued', 'downloading')""",
                (time.time(),),
            ).rowcount

    def mark_cleaned(self, job_name: str) -> JobRecord:
        """Acknowledge caller-verified removal; repeated calls are idempotent."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require(connection, job_name)
            if current.state in ("queued", "downloading"):
                raise ValueError("Stop active work before marking it cleaned")
            now = time.time()
            connection.execute(
                """UPDATE jobs SET state = 'cleaned', cleaned_at = ?, updated_at = ?,
                    error = NULL WHERE job_name = ? AND state != 'cleaned'""",
                (now, now, job_name),
            )
            return self._require(connection, job_name)

    def prune_cleaned(self, *, before: float) -> int:
        """Delete only tombstones older than an explicit Unix timestamp.

        Callers must choose retention to cover host retries. Unknown and
        unfinished jobs are never removed. This does not touch any files.
        """
        with self._connection() as connection:
            return connection.execute(
                "DELETE FROM jobs WHERE state = 'cleaned' AND cleaned_at < ?", (before,)
            ).rowcount

    def maintain(self) -> None:
        """Check integrity and reclaim database space; run while idle."""
        with self._connection() as connection:
            results = [row[0] for row in connection.execute("PRAGMA quick_check")]
            if results != ["ok"]:
                raise RuntimeError(f"Job database integrity check failed: {results}")
            connection.execute("VACUUM")
            connection.execute("PRAGMA optimize")

    @classmethod
    def _require(cls, connection: sqlite3.Connection, job_name: str) -> JobRecord:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_name = ?", (job_name,)
        ).fetchone()
        if row is None:
            raise KeyError(job_name)
        return cls._decode(row)

    @staticmethod
    def _decode(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_name=row["job_name"], task_id=row["task_id"],
            backend_id=row["backend_id"], payload=row["payload"],
            mount_root=Path(row["mount_root"]), workspace_path=Path(row["workspace_path"]),
            state=row["state"], file_paths=tuple(map(Path, json.loads(row["file_paths_json"]))),
            error=row["error"], created_at=row["created_at"],
            updated_at=row["updated_at"], cleaned_at=row["cleaned_at"],
        )
