# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import sqlite3
import json
import time
import uuid
import threading
from typing import Dict, List, Optional, Any
from enum import Enum
from datetime import datetime
from pydantic import BaseModel, Field
from contextlib import contextmanager


class JobStatus(str, Enum):
    """Status of a job in the queue."""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobProgress(BaseModel):
    """Progress information for a running job."""
    phase: str
    percentage: Optional[float] = None
    solutions_found: int = 0
    best_objective: Optional[float] = None
    best_bound: Optional[float] = None
    current_activity: str = ''
    elapsed_seconds: float = 0
    # Live preview of best solution found so far
    preview_placements: Optional[List[Dict]] = None
    preview_mutations: Optional[List[Dict]] = None
    preview_cells_used: Optional[int] = None


class Job(BaseModel):
    """A job in the queue."""
    id: str
    status: JobStatus
    created_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    # Request info
    request_type: str
    request_params: Dict

    # Progress (updated during execution)
    progress: Optional[JobProgress] = None

    # Result (when completed)
    result: Optional[Dict] = None
    error: Optional[str] = None


class JobManager:
    """Manages job queue with SQLite persistence."""

    def __init__(self, db_path: str = "job_queue.db", expiration_hours: float = 24.0):
        """Initialize the job manager."""
        self.db_path = db_path
        self.expiration_hours = expiration_hours
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """Get a thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        yield self._local.conn

    def _init_db(self):
        """Initialize database schema."""
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        request_type TEXT NOT NULL,
                        request_params TEXT NOT NULL,

                        created_at REAL NOT NULL,
                        started_at REAL,
                        completed_at REAL,

                        progress_json TEXT,
                        result_json TEXT,
                        error TEXT,

                        cancellation_requested INTEGER DEFAULT 0
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")

                # Migration: add cancellation_requested column if missing
                cursor = conn.execute("PRAGMA table_info(jobs)")
                columns = [row[1] for row in cursor.fetchall()]
                if "cancellation_requested" not in columns:
                    conn.execute("ALTER TABLE jobs ADD COLUMN cancellation_requested INTEGER DEFAULT 0")

                conn.commit()

    def create_job(self, request_type: str, request_params: Dict) -> str:
        """Create a new job and add it to the queue."""
        job_id = str(uuid.uuid4())
        created_at = time.time()

        with self._lock:
            with self._get_connection() as conn:
                conn.execute("""
                    INSERT INTO jobs (id, status, request_type, request_params, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    job_id,
                    JobStatus.QUEUED.value,
                    request_type,
                    json.dumps(request_params),
                    created_at
                ))
                conn.commit()

        return job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job by its ID."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM jobs WHERE id = ?",
                    (job_id,)
                )
                row = cursor.fetchone()

                if not row:
                    return None

                return self._row_to_job(row)

    def get_next_queued_job(self) -> Optional[Job]:
        """Get the next job in the queue (FIFO order)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                    (JobStatus.QUEUED.value,)
                )
                row = cursor.fetchone()

                if not row:
                    return None

                return self._row_to_job(row)

    def get_queue_position(self, job_id: str) -> Optional[int]:
        """Get the position of a job in the queue."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT created_at, status FROM jobs WHERE id = ?",
                    (job_id,)
                )
                row = cursor.fetchone()

                if not row or row["status"] != JobStatus.QUEUED.value:
                    return None

                job_created_at = row["created_at"]

                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM jobs WHERE status = ? AND created_at < ?",
                    (JobStatus.QUEUED.value, job_created_at)
                )
                count_row = cursor.fetchone()

                return (count_row["count"] if count_row else 0) + 1

    def update_status(self, job_id: str, status: JobStatus) -> bool:
        """Update a job's status."""
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                if status == JobStatus.RUNNING:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                        (status.value, now, job_id)
                    )
                elif status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, completed_at = ? WHERE id = ?",
                        (status.value, now, job_id)
                    )
                else:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ? WHERE id = ?",
                        (status.value, job_id)
                    )
                conn.commit()
                return cursor.rowcount > 0

    def update_progress(self, job_id: str, progress: JobProgress) -> bool:
        """Update a job's progress information."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET progress_json = ? WHERE id = ?",
                    (progress.model_dump_json(), job_id)
                )
                conn.commit()
                return cursor.rowcount > 0

    def complete_job(self, job_id: str, result: Dict) -> bool:
        """Mark a job as completed with its result."""
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET status = ?, completed_at = ?, result_json = ? WHERE id = ?",
                    (JobStatus.COMPLETED.value, now, json.dumps(result), job_id)
                )
                conn.commit()
                return cursor.rowcount > 0

    def fail_job(self, job_id: str, error: str) -> bool:
        """Mark a job as failed with an error message."""
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET status = ?, completed_at = ?, error = ? WHERE id = ?",
                    (JobStatus.FAILED.value, now, error, job_id)
                )
                conn.commit()
                return cursor.rowcount > 0

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation of a job."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT status FROM jobs WHERE id = ?",
                    (job_id,)
                )
                row = cursor.fetchone()

                if not row:
                    return False

                current_status = row["status"]

                # Can't cancel already completed/failed/cancelled jobs
                if current_status in (JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
                    return False

                now = time.time()

                if current_status == JobStatus.RUNNING.value:
                    cursor = conn.execute(
                        "UPDATE jobs SET cancellation_requested = 1 WHERE id = ?",
                        (job_id,)
                    )
                else:
                    # For queued jobs, immediately cancel
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, completed_at = ? WHERE id = ?",
                        (JobStatus.CANCELLED.value, now, job_id)
                    )

                conn.commit()
                return cursor.rowcount > 0


    def is_cancelled(self, job_id: str) -> bool:
        """Check if a job has been cancelled (final status)."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT status FROM jobs WHERE id = ?",
                    (job_id,)
                )
                row = cursor.fetchone()

                if not row:
                    return False

                return row["status"] == JobStatus.CANCELLED.value

    def is_cancellation_requested(self, job_id: str) -> bool:
        """Check if cancellation has been requested for a job."""
        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT cancellation_requested FROM jobs WHERE id = ?",
                    (job_id,)
                )
                row = cursor.fetchone()

                if not row:
                    return False

                return bool(row["cancellation_requested"])

    def complete_cancelled_job(self, job_id: str, result: Optional[Dict] = None) -> bool:
        """Finalize a cancelled job, optionally storing a partial result."""
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                if result is not None:
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, completed_at = ?, result_json = ?, cancellation_requested = 0 WHERE id = ?",
                        (JobStatus.COMPLETED.value, now, json.dumps(result), job_id)
                    )
                else:
                    # No result - mark as cancelled
                    cursor = conn.execute(
                        "UPDATE jobs SET status = ?, completed_at = ?, cancellation_requested = 0 WHERE id = ?",
                        (JobStatus.CANCELLED.value, now, job_id)
                    )
                conn.commit()
                return cursor.rowcount > 0

    def cleanup_expired(self) -> int:
        """Remove jobs older than expiration time."""
        cutoff_time = time.time() - (self.expiration_hours * 3600)

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM jobs WHERE completed_at IS NOT NULL AND completed_at < ?",
                    (cutoff_time,)
                )
                conn.commit()
                return cursor.rowcount

    def mark_interrupted_as_failed(self) -> int:
        """Mark any running jobs as failed (called on startup)."""
        now = time.time()

        with self._lock:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "UPDATE jobs SET status = ?, completed_at = ?, error = ? WHERE status = ?",
                    (
                        JobStatus.FAILED.value,
                        now,
                        "Server restarted while job was running",
                        JobStatus.RUNNING.value
                    )
                )
                conn.commit()
                return cursor.rowcount


    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """Convert a database row to a Job object."""
        progress = None
        if row["progress_json"]:
            progress = JobProgress.model_validate_json(row["progress_json"])

        result = None
        if row["result_json"]:
            result = json.loads(row["result_json"])

        return Job(
            id=row["id"],
            status=JobStatus(row["status"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            request_type=row["request_type"],
            request_params=json.loads(row["request_params"]),
            progress=progress,
            result=result,
            error=row["error"]
        )


# Global job manager instance
job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    """Get the global job manager instance, creating it if needed."""
    global job_manager
    if job_manager is None:
        job_manager = JobManager()
    return job_manager


def init_job_manager(db_path: str = "job_queue.db", expiration_hours: float = 24.0) -> JobManager:
    """Initialize the global job manager with custom settings."""
    global job_manager
    job_manager = JobManager(db_path=db_path, expiration_hours=expiration_hours)
    return job_manager
