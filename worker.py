# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import time
import threading
import traceback
from typing import Dict, Optional, Callable, Any
from concurrent.futures import ThreadPoolExecutor

from jobs import JobManager, JobStatus, JobProgress, Job
from solver_callbacks import SolverProgressCallback, update_job_phase


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

    def stop(self, wait: bool = True, timeout: float = 30.0) -> None:
        """Stop the worker pool."""
        self._shutdown.set()

        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None

        if self._poll_thread is not None and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=timeout)
            self._poll_thread = None

    def _run_loop(self) -> None:
        """Main polling loop that checks for new jobs."""
        while not self._shutdown.is_set():
            try:
                # Check how many workers are available
                with self._lock:
                    active_count = len(self._active_jobs)

                if active_count < self.num_workers:
                    # Try to get next job
                    job = self.job_manager.get_next_queued_job()

                    if job is not None:
                        # Mark as running before submitting
                        self.job_manager.update_status(job.id, JobStatus.RUNNING)

                        # Submit to thread pool
                        if self._executor is not None:
                            cancel_event = threading.Event()
                            with self._lock:
                                self._active_jobs[job.id] = cancel_event

                            self._executor.submit(self._process_job, job, cancel_event)

                # Also run cleanup periodically
                self.job_manager.cleanup_expired()

            except Exception as e:
                # Log but don't crash the worker loop
                print(f"Worker loop error: {e}")

            # Wait before next poll
            self._shutdown.wait(timeout=self.poll_interval)

    def _process_job(self, job: Job, cancel_event: threading.Event) -> None:
        """Process a single job."""
        start_time = time.time()

        try:
            # Update progress to show we're starting
            update_job_phase(
                self.job_manager,
                job.id,
                phase="preparing",
                activity="Initializing solver...",
                start_time=start_time
            )

            if self.job_manager.is_cancelled(job.id):
                return

            # Run the solver
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

            # Mark as completed
            self.job_manager.complete_job(job.id, result)

        except Exception as e:
            # Mark as failed with error details
            error_msg = f"{type(e).__name__}: {str(e)}"
            if hasattr(e, '__traceback__'):
                tb = traceback.format_exception(type(e), e, e.__traceback__)
                error_msg += "\n" + "".join(tb[-3:])  # Last 3 lines of traceback

            self.job_manager.fail_job(job.id, error_msg)

        finally:
            # Remove from active jobs
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
