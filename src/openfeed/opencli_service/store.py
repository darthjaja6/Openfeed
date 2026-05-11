from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .models import JobRequest


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    site TEXT NOT NULL,
                    command TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    timeout_seconds INTEGER NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    lane_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    returncode INTEGER,
                    stdout TEXT,
                    stderr TEXT,
                    result_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs(status, profile, site, priority, created_at)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, updated_at)"
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS project_state (
                    profile TEXT NOT NULL,
                    site TEXT NOT NULL,
                    project TEXT NOT NULL,
                    last_started_at REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (profile, site, project)
                )
                """
            )
            self._conn.commit()

    def recover_interrupted_jobs(self) -> int:
        """Fail jobs left running by a previous service process.

        We do not automatically re-run interrupted jobs because OpenCLI commands
        can include write operations. Producers can submit idempotent read jobs
        again using their own idempotency keys.
        """
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = 'opencli service restarted while job was running',
                    updated_at = ?,
                    finished_at = ?
                WHERE status = 'running'
                """,
                (now, now),
            )
            self._conn.commit()
            return int(cur.rowcount)

    def enqueue(self, request: JobRequest) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if request.idempotency_key:
                existing = self._conn.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
            job_id = f"job_{uuid.uuid4().hex}"
            self._conn.execute(
                """
                INSERT INTO jobs (
                    id, project, profile, site, command, args_json, status,
                    priority, timeout_seconds, idempotency_key, created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request.project,
                    request.profile,
                    request.site,
                    request.command,
                    json.dumps(request.args),
                    request.priority,
                    request.timeout_seconds,
                    request.idempotency_key,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO project_state (profile, site, project, last_started_at)
                VALUES (?, ?, ?, 0)
                """,
                (request.profile, request.site, request.project),
            )
            self._conn.commit()
            return self.get(job_id) or {"id": job_id, "status": "queued"}

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def acquire_next(self, *, profile: str, site: str, lane_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT jobs.*
                FROM jobs
                LEFT JOIN project_state
                  ON project_state.profile = jobs.profile
                 AND project_state.site = jobs.site
                 AND project_state.project = jobs.project
                WHERE jobs.status = 'queued' AND jobs.profile = ? AND jobs.site = ?
                ORDER BY COALESCE(project_state.last_started_at, 0) ASC,
                         jobs.priority DESC,
                         jobs.created_at ASC
                LIMIT 1
                """,
                (profile, site),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["id"])
            self._conn.execute(
                """
                UPDATE jobs
                SET status = 'running',
                    lane_id = ?,
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (lane_id, now, now, job_id),
            )
            self._conn.execute(
                """
                INSERT INTO project_state (profile, site, project, last_started_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile, site, project)
                DO UPDATE SET last_started_at = excluded.last_started_at
                """,
                (profile, site, str(row["project"]), now),
            )
            self._conn.commit()
            return self.get(job_id)

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        returncode: int | None = None,
        stdout: str | None = None,
        stderr: str | None = None,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        now = time.time()
        result_json = json.dumps(result) if result is not None else None
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?,
                    returncode = ?,
                    stdout = ?,
                    stderr = ?,
                    result_json = ?,
                    error = ?,
                    updated_at = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (status, returncode, stdout, stderr, result_json, error, now, now, job_id),
            )
            self._conn.commit()

    def cancel(self, job_id: str) -> bool:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, now, job_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def queued_sites(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT profile, site FROM jobs WHERE status = 'queued'"
            ).fetchall()
        return [(str(row["profile"]), str(row["site"])) for row in rows]
