# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import logging
import os
import time
import threading
from typing import Dict, Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor

from jobs import JobManager, JobStatus, JobProgress, Job
from solver_callbacks import SolverProgressCallback, update_job_phase

logger = logging.getLogger(__name__)

_current = threading.local()


def current_job_cancelled() -> bool:
    """True when the job running on this thread was cancelled or the worker is shutting down."""
    event = getattr(_current, "cancel_event", None)
    return event is not None and event.is_set()


GENERIC_JOB_ERROR = "SolverError: The solver hit an unexpected error. Please try again."


def public_job_error(e: BaseException) -> str:
    """The one line stored in a failed job's `error` (shown publicly by
    GET /greenhouse/jobs/{id})."""
    status = getattr(e, "status_code", None)
    detail = getattr(e, "detail", None)
    if isinstance(status, int) and 400 <= status < 600 and isinstance(detail, str):
        return f"HTTPException: {status}: {detail.splitlines()[0][:300] if detail else ''}"
    if type(e).__name__ == "ValidationError" and hasattr(e, "errors"):
        try:
            err = e.errors()[0]
            loc = ".".join(str(p) for p in err.get("loc", ()))
            return f"ValidationError: {loc + ': ' if loc else ''}{err.get('msg', 'invalid value')}"[:300]
        except Exception:
            return "ValidationError: invalid solve request"
    return GENERIC_JOB_ERROR

CLEANUP_INTERVAL_S = 60.0
YIELD_MAX_S = float(os.environ.get("SKYSHARDS_YIELD_MAX_S", "600"))

_yield_check: Optional[Callable[[], bool]] = None


def set_yield_check(check: Optional[Callable[[], bool]]) -> None:
    """Install a predicate that, while it returns True, stops the worker from
    starting new solves (running ones run to completion)."""
    global _yield_check
    _yield_check = check


class JobWorker:
    """Background worker that processes jobs from the queue."""

    def __init__(
        self,
        job_manager: JobManager,
        solver_func: Callable[[Dict, str, JobManager], Dict],
        num_workers: int = 1,
        poll_interval: float = 0.5
    ):
        """Initialize the job worker."""
        self.job_manager = job_manager
        self.solver_func = solver_func
        self.num_workers = num_workers
        self.poll_interval = poll_interval

        self._executor: Optional[ThreadPoolExecutor] = None
        self._shutdown = threading.Event()
        self._active_jobs: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the worker pool and polling thread."""
        if self._executor is not None:
            return  # Already running

        self._shutdown.clear()
        self._executor = ThreadPoolExecutor(max_workers=self.num_workers)
        self._poll_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._poll_thread.start()

    def stop(self, wait: bool = False, timeout: float = 10.0) -> None:
        """Stop the worker pool."""
        self._shutdown.set()

        with self._lock:
            active = dict(self._active_jobs)
        for job_id, event in active.items():
            event.set()
            try:
                self.job_manager.cancel_job(job_id)
            except Exception as e:
                logger.warning("Could not flag job %s for cancellation: %s", job_id, e)

        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
            self._executor = None

        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=timeout)
            self._poll_thread = None

        try:
            failed = self.job_manager.mark_interrupted_as_failed()
            if failed:
                logger.info("Marked %d running job(s) as interrupted on shutdown", failed)
        except Exception as e:
            logger.warning("Could not mark interrupted jobs on shutdown: %s", e)

    def _run_loop(self) -> None:
        """Main polling loop that checks for new jobs."""
        last_cleanup = 0.0
        yield_start: Optional[float] = None
        while not self._shutdown.is_set():
            try:
                now = time.time()

                if now - last_cleanup >= CLEANUP_INTERVAL_S:
                    last_cleanup = now
                    self.job_manager.cleanup_expired()

                with self._lock:
                    active_count = len(self._active_jobs)
                    active = list(self._active_jobs.items())

                for job_id, event in active:
                    if not event.is_set() and self.job_manager.is_cancellation_requested(job_id):
                        event.set()

                busy = False
                if _yield_check is not None and active_count < self.num_workers:
                    try:
                        busy = bool(_yield_check())
                    except Exception:
                        busy = False
                if busy:
                    if yield_start is None:
                        yield_start = now
                        print("Job worker: yielding CPU to leaderboard verification")
                    elif now - yield_start > YIELD_MAX_S:
                        print(f"Job worker: yield capped at {YIELD_MAX_S:.0f}s, resuming")
                        busy = False
                        yield_start = None
                elif yield_start is not None:
                    print(f"Job worker: resumed after yielding {now - yield_start:.0f}s")
                    yield_start = None

                if busy:
                    self._shutdown.wait(timeout=self.poll_interval)
                    continue

                if active_count < self.num_workers:
                    job = self.job_manager.claim_next_queued_job()
                    if job is not None and self._executor is not None:
                        cancel_event = threading.Event()
                        with self._lock:
                            self._active_jobs[job.id] = cancel_event
                        self._executor.submit(self._process_job, job, cancel_event)

            except Exception as e:
                print(f"Worker loop error: {e}")

            self._shutdown.wait(timeout=self.poll_interval)

    def _process_job(self, job: Job, cancel_event: threading.Event) -> None:
        """Process a single job."""
        start_time = time.time()
        _current.cancel_event = cancel_event

        try:
            update_job_phase(
                self.job_manager,
                job.id,
                phase="preparing",
                activity="Initializing solver...",
                start_time=start_time
            )

            if self.job_manager.is_cancelled(job.id):
                return

            result = self.solver_func(job.request_params, job.id, self.job_manager)

            if self.job_manager.is_cancellation_requested(job.id):
                print(f"[DEBUG] Worker: cancellation requested, result={result is not None}, has_placements={result.get('placements') if result else None}")
                if result and result.get("placements"):
                    # We have a partial result - mark it as cancelled and save it
                    result["status"] = "CANCELLED"
                    print(f"[DEBUG] Worker: saving cancelled job with {len(result.get('placements', []))} placements")
                    self.job_manager.complete_cancelled_job(job.id, result)
                else:
                    # No useful result - just mark as cancelled
                    print(f"[DEBUG] Worker: no placements, marking as cancelled without result")
                    self.job_manager.complete_cancelled_job(job.id, None)
                return

            self.job_manager.complete_job(job.id, result)

        except Exception as e:
            error_msg = public_job_error(e)
            if error_msg == GENERIC_JOB_ERROR:
                logger.exception("Job %s failed", job.id)
            else:
                logger.info("Job %s failed: %s", job.id, error_msg)
            self.job_manager.fail_job(job.id, error_msg)

        finally:
            _current.cancel_event = None
            with self._lock:
                self._active_jobs.pop(job.id, None)


# Global worker instance
_worker: Optional[JobWorker] = None


def init_worker(
    job_manager: JobManager,
    solver_func: Callable[[Dict, str, JobManager], Dict],
    num_workers: int = 1
) -> JobWorker:
    """Initialize and start the global worker."""
    global _worker

    if _worker is not None:
        _worker.stop()

    _worker = JobWorker(
        job_manager=job_manager,
        solver_func=solver_func,
        num_workers=num_workers
    )
    _worker.start()

    return _worker


def stop_worker() -> None:
    """Stop the global worker."""
    global _worker

    if _worker is not None:
        _worker.stop()
        _worker = None
