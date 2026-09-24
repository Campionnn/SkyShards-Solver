# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import time
import threading
from typing import Optional, Callable, Dict, Any, List
from ortools.sat.python import cp_model

from jobs import JobManager, JobProgress


class SolverProgressCallback(cp_model.CpSolverSolutionCallback):
    """Callback for tracking solver progress and reporting to job manager."""

    def __init__(
        self,
        job_manager: JobManager,
        job_id: str,
        is_maximizing: bool = False,
        update_interval: float = 1.0,
        crop_vars: Optional[Dict[str, Dict[tuple, Any]]] = None,
        mutation_vars: Optional[Dict[str, Dict[tuple, Any]]] = None,
        cell_used_vars: Optional[Dict[tuple, Any]] = None,
        crop_sizes: Optional[Dict[str, int]] = None,
        mutation_sizes: Optional[Dict[str, int]] = None,
        locked_placements: Optional[List[Dict]] = None,
        time_limit: Optional[float] = None,
        objective_scales=None,
    ):
        """Initialize the progress callback."""
        super().__init__()
        self.job_manager = job_manager
        self.job_id = job_id
        self.is_maximizing = is_maximizing
        self.update_interval = update_interval
        self.time_limit = time_limit
        self.objective_scales = objective_scales

        # Variable references for solution capture
        self.crop_vars = crop_vars
        self.mutation_vars = mutation_vars
        self.cell_used_vars = cell_used_vars

        # Sizes for preview extraction
        self.crop_sizes = crop_sizes or {}
        self.mutation_sizes = mutation_sizes or {}

        # Locked placements for preview
        self.locked_placements = locked_placements or []

        self.solutions_found = 0
        self.start_time = time.time()
        self._initial_bound: Optional[float] = None

        self.best_solution_values: Optional[Dict[str, Any]] = None
        self.best_objective: Optional[float] = None
        self.best_bound: Optional[float] = None
        self.was_cancelled = False

        # Thread-safe state for background updater
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._has_new_solution = threading.Event()
        self._updater_thread: Optional[threading.Thread] = None
        self._last_sent_solution_count = 0

    def start_background_updater(self):
        """Start the background thread that sends periodic updates."""
        self._stop_event.clear()
        self._updater_thread = threading.Thread(target=self._background_updater, daemon=True)
        self._updater_thread.start()

    def stop_background_updater(self):
        """Stop the background thread."""
        self._stop_event.set()
        if self._updater_thread and self._updater_thread.is_alive():
            self._updater_thread.join(timeout=2.0)

    def _background_updater(self):
        """Background thread that periodically sends updates and checks cancellation."""
        while not self._stop_event.is_set():
            if self.job_manager.is_cancellation_requested(self.job_id):
                self.was_cancelled = True
                self.StopSearch()
                break

            with self._lock:
                if self.best_solution_values is not None:
                    self._send_progress_update()

            self._stop_event.wait(timeout=self.update_interval)

    def _send_progress_update(self):
        """Send a progress update to the job manager (must hold lock)."""
        if self.best_solution_values is None:
            return

        current_time = time.time()

        preview_placements, preview_mutations, preview_cells_used = self._extract_preview()

        n_muts = len(preview_mutations) if preview_mutations else 0
        activity = f"Found {self.solutions_found} solutions, best has {n_muts} mutations"
        if self.objective_scales is not None and self.best_objective is not None:
            try:
                score_int, priority, _cells = self.objective_scales.decode(self.best_objective)
                score = self.objective_scales.score_from_int(score_int)
                activity = (
                    f"Found {self.solutions_found} solutions, best score {score:.3f} "
                    f"({n_muts} mutations, {preview_cells_used or 0} cells"
                    + (f", priority {priority}" if priority else "")
                    + ")"
                )
            except Exception:
                pass
        elif not self.is_maximizing:
            activity = f"Found {self.solutions_found} solutions, best uses {preview_cells_used or 0} cells"

        percentage = self._calculate_percentage()

        progress = JobProgress(
            phase="solving",
            percentage=percentage,
            solutions_found=self.solutions_found,
            best_objective=self.best_objective,
            best_bound=self.best_bound,
            current_activity=activity,
            elapsed_seconds=current_time - self.start_time,
            preview_placements=preview_placements,
            preview_mutations=preview_mutations,
            preview_cells_used=preview_cells_used
        )

        self.job_manager.update_progress(self.job_id, progress)

    def on_solution_callback(self):
        """Called when the solver finds a new solution."""
        self.solutions_found += 1

        with self._lock:
            self._capture_solution()
            if self.solutions_found == 1:
                try:
                    self._send_progress_update()
                except Exception:
                    pass

        self._has_new_solution.set()

        if self.job_manager.is_cancellation_requested(self.job_id):
            self.was_cancelled = True
            self.StopSearch()
            return

    def _capture_solution(self) -> None:
        """Capture the current solution's variable values for extraction (must hold lock)."""
        if not self.crop_vars or not self.mutation_vars:
            return

        self.best_objective = self.ObjectiveValue()
        self.best_bound = self.BestObjectiveBound()

        solution = self.Response().solution

        crop_values = {}
        for crop_name, positions in self.crop_vars.items():
            crop_values[crop_name] = {
                pos: 1 for pos, var in positions.items() if solution[var.Index()]
            }

        mutation_values = {}
        for mut_name, positions in self.mutation_vars.items():
            mutation_values[mut_name] = {
                pos: 1 for pos, var in positions.items() if solution[var.Index()]
            }

        self.best_solution_values = {
            'crop_vars': crop_values,
            'mutation_vars': mutation_values,
            'cell_used_vars': {}
        }

    def _extract_preview(self) -> tuple:
        """Extract preview data (placements, mutations, cells_used) from captured solution."""
        if not self.best_solution_values:
            return None, None, None

        crop_values = self.best_solution_values.get('crop_vars', {})
        mutation_values = self.best_solution_values.get('mutation_vars', {})

        placements = []
        used_cells: set = set()
        for crop_name, positions in crop_values.items():
            size = self.crop_sizes.get(crop_name, 1)
            for pos, val in positions.items():
                if val == 1:
                    placements.append({
                        "crop": crop_name,
                        "position": [pos[0], pos[1]],
                        "size": size,
                        "locked": False
                    })
                    for dr in range(size):
                        for dc in range(size):
                            used_cells.add((pos[0] + dr, pos[1] + dc))

        for lock in self.locked_placements:
            lock_pos = lock["position"]
            lock_size = lock["size"]
            pos_tuple = tuple(lock_pos) if not isinstance(lock_pos, tuple) else lock_pos
            placements.append({
                "crop": lock["name"],
                "position": list(lock_pos),
                "size": lock_size,
                "locked": True
            })
            for dr in range(lock_size):
                for dc in range(lock_size):
                    used_cells.add((pos_tuple[0] + dr, pos_tuple[1] + dc))

        mutations = []
        for mut_name, positions in mutation_values.items():
            size = self.mutation_sizes.get(mut_name, 1)
            for pos, val in positions.items():
                if val == 1:
                    mutations.append({
                        "mutation": mut_name,
                        "position": [pos[0], pos[1]],
                        "size": size
                    })
                    for dr in range(size):
                        for dc in range(size):
                            used_cells.add((pos[0] + dr, pos[1] + dc))

        cells_used = len(used_cells)

        return placements, mutations, cells_used

    def _calculate_percentage(self) -> Optional[float]:
        """Calculate progress percentage based on the maximum of:
        1."""
        percentages = []

        try:
            if self.best_objective is not None and self.best_bound is not None:
                objective = self.best_objective
                bound = self.best_bound

                if self._initial_bound is None:
                    self._initial_bound = bound

                if self.is_maximizing:
                    # For maximization, we're going from 0 towards the bound
                    if bound > 0:
                        progress = min(100.0, max(0.0, (objective / bound) * 100))
                        percentages.append(progress)
                else:
                    if self._initial_bound is not None and self._initial_bound != bound:
                        range_size = abs(self._initial_bound - bound)
                        if range_size > 0:
                            progress = min(100.0, max(0.0, (1 - (objective - bound) / range_size) * 100))
                            percentages.append(progress)
                        elif objective <= bound:
                            percentages.append(100.0)
        except Exception:
            pass

        if self.time_limit is not None and self.time_limit > 0:
            elapsed = time.time() - self.start_time
            time_progress = min(100.0, max(0.0, (elapsed / self.time_limit) * 100))
            percentages.append(time_progress)

        if percentages:
            return round(max(percentages), 1)

        return None


def update_job_phase(
    job_manager: JobManager,
    job_id: str,
    phase: str,
    activity: str,
    start_time: float
) -> None:
    """Update job progress with a phase change."""
    progress = JobProgress(
        phase=phase,
        percentage=None,
        solutions_found=0,
        best_objective=None,
        best_bound=None,
        current_activity=activity,
        elapsed_seconds=time.time() - start_time,
        preview_placements=None,
        preview_mutations=None,
        preview_cells_used=None
    )
    job_manager.update_progress(job_id, progress)
