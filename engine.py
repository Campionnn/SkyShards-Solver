# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import json
import math
import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from fastapi import HTTPException
from ortools.sat.python import cp_model
import time

try:
    import solvecache
except ImportError:
    solvecache = None

from models import (
    CropDefinition, MutationRequirement, MutationDefinition,
    LockedPlacement, MutationGoal, GenericSolveRequest, GenericSolveResponse,
)
from solver import (
    get_crop_cells,
    get_adjacent_cells_for_mutation,
    compute_crop_and_mutation_positions,
    create_decision_variables,
    create_multiplicity_chains,
    add_cell_usage_constraints,
    add_mutation_eligibility_constraints,
    add_symmetry_breaking_constraints,
    create_cell_used_vars,
    build_objective,
    score_layout,
    add_decision_strategy,
    apply_locked_placements,
    extract_results,
    EFFECT_NAMES,
    RELAY_EFFECT,
    build_buff_table,
    default_buff_crops,
    effects_active,
    needed_effects,
    prune_free_buff_set,
    prune_valueless_placements,
    resolve_effect_weights,
    score_layout_with_effects,
    special_required_effects,
    build_effect_model,
    add_effect_score_terms,
    add_special_eligibility_constraints,
)

from gamedata import DEFAULT_DATA, DEFAULT_EFFECT_WEIGHTS


# Load default priorities from JSON file
def load_default_priorities() -> Dict[str, int]:
    """Load the default priorities from default_priorities.json."""
    default_priorities_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_priorities.json")
    try:
        with open(default_priorities_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        # If file is malformed, return empty dict
        return {}


DEFAULT_PRIORITIES = load_default_priorities()

BUFF_TABLE = build_buff_table(
    [CropDefinition(**c) for c in DEFAULT_DATA['crops']],
    DEFAULT_DATA['mutations'],
)
MAX_EFFECT_WEIGHT = 100.0

SOLVER_THREADS = int(os.environ.get("SKYSHARDS_SOLVER_WORKERS", "8"))


def with_default_effect_weights(params: Dict) -> Dict:
    """Fill in the default effect weights when a request names none."""
    if params.get("effect_weights") is None:
        return {**params, "effect_weights": dict(DEFAULT_EFFECT_WEIGHTS)}
    return params


def clamp_client_time_limit(time_limit: Optional[float], ceiling: Optional[float] = None) -> Optional[float]:
    """Sanitise a time limit that came from a client: None passes through"""
    if time_limit is None:
        return None
    try:
        t = float(time_limit)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(t) or t <= 0:
        return None
    return min(t, ceiling) if ceiling is not None else t


@dataclass
class EffectContext:
    """Everything the solve needs to know about effect scoring for a request."""
    weights: Dict[str, Dict[str, float]] = field(default_factory=dict)  # per target
    active: bool = False
    free_names: List[str] = field(default_factory=list)                  # free buff sources
    needed: frozenset = frozenset()
    required: frozenset = frozenset()   # effects a godseed-style target must hold


def validate_effect_request(req: GenericSolveRequest, crop_names: Set[str], mutation_names: Set[str]) -> None:
    """Semantic validation of effect_weights / buff_crops (types are pydantic's job)."""
    valid_effects = set(EFFECT_NAMES)
    target_names = {t.mutation for t in req.targets}

    def check_weights(weights: Optional[Dict[str, float]], where: str):
        for name, w in (weights or {}).items():
            if name not in valid_effects:
                raise HTTPException(
                    400, f"Unknown effect '{name}' in {where}. Valid effects: {', '.join(sorted(valid_effects))}"
                )
            try:
                w = float(w)
            except (TypeError, ValueError):
                raise HTTPException(400, f"Weight for '{name}' in {where} must be a number")
            if not math.isfinite(w) or abs(w) > MAX_EFFECT_WEIGHT:
                raise HTTPException(
                    400, f"Weight for '{name}' in {where} must be finite and within +/-{MAX_EFFECT_WEIGHT:g}"
                )

    check_weights(req.effect_weights, "effect_weights")
    for t in req.targets:
        if t.effect_weights:
            if t.mutation not in target_names:
                raise HTTPException(400, f"effect_weights override for '{t.mutation}' which is not a target")
            check_weights(t.effect_weights, f"targets[{t.mutation}].effect_weights")

    for name in req.buff_crops or []:
        if name not in crop_names:
            if name in mutation_names:
                raise HTTPException(
                    400, f"buff_crops: '{name}' is a mutation; only base crops can be placed freely for now"
                )
            raise HTTPException(400, f"buff_crops: unknown crop '{name}'")


def resolve_buff_sources(
    req: GenericSolveRequest,
    default_crops: List[CropDefinition],
    effect_weights: Dict[str, Dict[str, float]],
    required_effects: frozenset = frozenset(),
) -> List[str]:
    """Names of the plants the solver may place freely as effect sources."""
    names = list(req.buff_crops) if req.buff_crops is not None else default_buff_crops(default_crops)
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    mutation_data = {m['name']: m for m in DEFAULT_DATA['mutations']}
    others: Set[str] = set()
    for t in req.targets:
        for r in mutation_data.get(t.mutation, {}).get('requirements', []):
            others.add(r['crop'])
    for l in req.locks or []:
        others.add(l.name)
    return prune_free_buff_set(
        unique, BUFF_TABLE, effect_weights, other_plant_names=others, required_effects=required_effects
    )


def get_required_mutations_and_crops(
    targets: List[MutationGoal],
    all_mutations_data: List[dict],
    all_crops: List[CropDefinition],
    priorities: Optional[Dict[str, int]] = None,
    extra_crop_names: Optional[List[str]] = None,
) -> tuple:
    """Compute the set of mutations and crops needed for the given targets."""
    # Build mutation lookup from raw data
    mutation_data_dict = {m['name']: m for m in all_mutations_data}

    # Get base crop names (crops that aren't mutations)
    crop_names = {c.name for c in all_crops}

    # Only include target mutations
    target_mutation_names = [t.mutation for t in targets]

    # Validate targets exist
    for mut_name in target_mutation_names:
        if mut_name not in mutation_data_dict:
            raise HTTPException(400, f"Unknown mutation '{mut_name}' in targets")

    # Create MutationDefinition objects only for target mutations
    filtered_mutations = [
        MutationDefinition(
            name=m['name'],
            size=m['size'],
            requirements=[MutationRequirement(**r) for r in m.get('requirements', [])],
            requires_zero_adjacent=m.get('requires_zero_adjacent', False),
            ground=m.get('ground', 'farmland'),
            rarity=m.get('rarity', 'common'),
            growth_stages=m.get('growth_stages', 0),
            decay=m.get('decay', 0),
            positive_buffs=m.get('positive_buffs', []),
            negative_buffs=m.get('negative_buffs', []),
            drops=m.get('drops', {}),
            special=m.get('special'),
            harvest_info=m.get('harvest_info'),
            growing_info=m.get('growing_info'),
            mutation_chance=m.get('mutation_chance', 0.10),
            spawn_weight=m.get('spawn_weight', 0),
        )
        for m in all_mutations_data
        if m['name'] in target_mutation_names
    ]

    # Collect crops needed by target mutations
    required_crops = set()
    mutations_used_as_crops = set()

    # For each target mutation, get its immediate requirements
    for target_mut_name in target_mutation_names:
        mut_data = mutation_data_dict[target_mut_name]
        for req in mut_data.get('requirements', []):
            crop = req['crop']
            if crop in crop_names:
                required_crops.add(crop)
            else:
                mutations_used_as_crops.add(crop)

    for name in extra_crop_names or []:
        if name in crop_names:
            required_crops.add(name)
        elif name in mutation_data_dict:
            mutations_used_as_crops.add(name)

    # Filter crops to only required ones
    filtered_crops = [c for c in all_crops if c.name in required_crops]

    for mut_name in sorted(mutations_used_as_crops):
        mut_data = mutation_data_dict[mut_name]
        priority = 0
        if priorities and mut_name in priorities:
            priority = priorities[mut_name]
        filtered_crops.append(CropDefinition(
            name=mut_data['name'],
            size=mut_data['size'],
            priority=priority,
            ground=mut_data.get('ground', 'farmland'),
            positive_buffs=mut_data.get('positive_buffs', []),
            negative_buffs=mut_data.get('negative_buffs', []),
        ))

    return filtered_mutations, filtered_crops


def validate_request(req: GenericSolveRequest) -> tuple:
    """Validate the solver request and build lookup dictionaries."""
    crop_defs = {c.name: c for c in req.crops}
    mutation_defs = {m.name: m for m in req.mutations}

    # Validate crop references in mutations
    for mut in req.mutations:
        for r in mut.requirements:
            if r.crop not in crop_defs:
                raise HTTPException(400, f"Unknown crop '{r.crop}' in mutation '{mut.name}'")

    # Validate targets reference valid mutations
    for t in req.targets:
        if t.mutation not in mutation_defs:
            raise HTTPException(400, f"Unknown mutation '{t.mutation}' in targets")
        if not t.maximize and t.count is None:
            raise HTTPException(400, f"Target for '{t.mutation}' must have count or maximize=true")

    # Separate targets into maximize and count-based
    maximize_mutations = [t.mutation for t in req.targets if t.maximize]
    target_mutations = {t.mutation: t.count for t in req.targets if not t.maximize and t.count is not None}

    cells = [tuple(c) for c in req.cells]
    cell_set = set(cells)

    return crop_defs, mutation_defs, maximize_mutations, target_mutations, cells, cell_set


def validate_locked_placements(
    locks: Optional[List[LockedPlacement]],
    cell_set: set,
    all_crops: List[CropDefinition],
    all_mutations_data: List[dict]
) -> set:
    """Validate locked placements and return the set of locked cells."""
    if not locks:
        return set()

    # Build lookup dicts for validation
    crop_lookup = {c.name: c.size for c in all_crops}
    mutation_lookup = {m["name"]: m["size"] for m in all_mutations_data}

    locked_cells_total = set()
    locked_cells_by_placement = []

    for i, lock in enumerate(locks):
        name = lock.name
        provided_size = lock.size
        pos = tuple(lock.position)

        # Check if crop/mutation exists
        expected_size = None
        if name in crop_lookup:
            expected_size = crop_lookup[name]
        elif name in mutation_lookup:
            expected_size = mutation_lookup[name]
        else:
            raise HTTPException(400, f"Lock #{i+1}: Unknown crop/mutation '{name}'")

        # Check if provided size matches definition
        if expected_size != provided_size:
            raise HTTPException(
                400,
                f"Lock #{i+1}: '{name}' has size {expected_size} but lock specifies size {provided_size}"
            )

        # Get cells occupied by this lock
        occupied = set(get_crop_cells(pos, provided_size))

        # Check all cells are in available cells
        invalid_cells = occupied - cell_set
        if invalid_cells:
            raise HTTPException(
                400,
                f"Lock #{i+1} ('{name}' at {list(pos)}) uses cells not in available cells: {sorted([list(c) for c in invalid_cells])}"
            )

        # Check for overlaps with previous locks
        for j, prev_occupied in enumerate(locked_cells_by_placement):
            overlap = occupied & prev_occupied
            if overlap:
                raise HTTPException(
                    400,
                    f"Lock #{i+1} overlaps with lock #{j+1} at cells: {sorted([list(c) for c in overlap])}"
                )

        locked_cells_by_placement.append(occupied)
        locked_cells_total.update(occupied)

    return locked_cells_total


def calculate_time_limit(
    num_cells: int,
    time_limit: Optional[float],
    target_mutations: Dict[str, int],
    maximize_mutations: List[str],
    mutation_upper_bounds: Optional[Dict],
    effects_on: bool = False,
) -> float:
    """Calculate the actual time limit to use for the solver."""
    if time_limit is not None:
        return time_limit

    if num_cells <= 50:
        base_time = 30.0
    elif num_cells <= 75:
        base_time = 45.0
    else:
        base_time = 60.0

    num_targets = len(target_mutations) + len(maximize_mutations)
    if num_targets > 1:
        base_time = max(base_time, 90.0)
    if effects_on:
        base_time = max(base_time, 60.0)
    elif mutation_upper_bounds and num_targets <= 1:
        base_time = min(base_time, 30.0)

    return base_time


def _make_solver(time_budget: float) -> cp_model.CpSolver:
    """Create a CP-SAT solver with the standard parameters."""
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_budget
    solver.parameters.num_search_workers = SOLVER_THREADS
    solver.parameters.linearization_level = int(os.environ.get("SKYSHARDS_LINEARIZATION_LEVEL", "2"))
    solver.parameters.random_seed = 42  # For reproducibility
    return solver


def solve_standard(
    model: cp_model.CpModel,
    time_budget: float,
    callback: Optional[cp_model.CpSolverSolutionCallback] = None
) -> tuple:
    """Single solve for the full budget."""
    solver = _make_solver(time_budget)

    if callback:
        callback.start_background_updater()
        try:
            status = solver.Solve(model, callback)
        finally:
            callback.stop_background_updater()
    else:
        status = solver.Solve(model)
    return solver, status


def expected_spawns_per_tick(
    cells: List[tuple],
    placements: List[Dict],
    mutations_result: List[Dict],
    targets: Optional[List[MutationGoal]] = None,
) -> float:
    """Exact expected mutation spawns per growth tick for a finished layout."""
    rates = score_layout(cells, placements, mutations_result, DEFAULT_DATA['mutations'])
    if targets:
        wanted = {t.mutation for t in targets if t.maximize}
        if wanted:
            return sum(v for k, v in rates.items() if k in wanted)
    return sum(rates.values())


def annotate_result(result: Dict, cells: List[tuple], req: GenericSolveRequest, ctx: EffectContext) -> Dict:
    """Attach the exact score, per-spot effects and rate figures to a result"""
    placements = result.get("placements", [])
    mutations = result.get("mutations", [])
    bd = score_layout_with_effects(
        cells, placements, mutations, DEFAULT_DATA['mutations'], req.targets, ctx.weights, BUFF_TABLE
    )
    for m, slot in zip(mutations, bd.per_slot):
        m["effects"] = slot["effects"]
        m["value"] = slot["value"]
    result["score"] = round(bd.score, 4)
    result["effect_value"] = round(bd.effect_value, 4)
    result["expected_spawns_per_tick"] = round(bd.total_rate, 4)
    result["effect_weights"] = ctx.weights
    result["buff_crops"] = list(ctx.free_names)
    return result


@dataclass
class PreparedProblem:
    """A solve request normalised the way the solver sees it:"""
    req: GenericSolveRequest
    default_crops: List[CropDefinition]   # every base crop, priorities applied
    ctx: EffectContext
    weights: Dict[str, Dict[str, float]]

    @property
    def has_locks(self) -> bool:
        return self.req.locks is not None and len(self.req.locks) > 0


def prepare_problem(req: GenericSolveRequest) -> PreparedProblem:
    """Normalise a request into a PreparedProblem (see class doc). Raises"""
    default_crops = [CropDefinition(**c) for c in DEFAULT_DATA['crops']]

    for crop in default_crops:
        if crop.name in DEFAULT_PRIORITIES:
            crop.priority = DEFAULT_PRIORITIES[crop.name]

    if req.priorities:
        for crop in default_crops:
            if crop.name in req.priorities:
                priority_value = req.priorities[crop.name]
                if 0 <= priority_value <= 100:
                    crop.priority = priority_value

    # Effect weights and free buff sources
    validate_effect_request(
        req, {c.name for c in default_crops}, {m['name'] for m in DEFAULT_DATA['mutations']}
    )
    weights = resolve_effect_weights(req.targets, req.effect_weights)
    required = special_required_effects(t.mutation for t in req.targets)
    ctx = EffectContext(weights=weights, active=effects_active(weights) or bool(required), required=required)
    if ctx.active:
        ctx.free_names = resolve_buff_sources(req, default_crops, weights, required)

    filtered_mutations, filtered_crops = get_required_mutations_and_crops(
        req.targets,
        DEFAULT_DATA['mutations'],
        default_crops,
        req.priorities if req.priorities else DEFAULT_PRIORITIES,
        extra_crop_names=ctx.free_names,
    )

    # Create a new request with populated crops/mutations
    prepared_req = GenericSolveRequest(
        cells=req.cells,
        priorities=req.priorities,
        crops=filtered_crops,
        mutations=filtered_mutations,
        targets=req.targets,
        effect_weights=req.effect_weights,
        buff_crops=ctx.free_names if ctx.active else req.buff_crops,
        remove_unused_crops=req.remove_unused_crops,
        locks=req.locks,
    )
    return PreparedProblem(
        req=prepared_req,
        default_crops=default_crops,
        ctx=ctx,
        weights=weights,
    )


def _best_bound(callback, solver, use_captured_solution) -> Optional[float]:
    """CP-SAT's best objective bound, or None if it cannot be read."""
    if callback is not None and callback.best_bound is not None:
        return callback.best_bound
    if not use_captured_solution and solver is not None:
        try:
            return solver.BestObjectiveBound()
        except Exception:
            return None
    return None


def solve_generic(
    req: GenericSolveRequest,
    maximize_only: bool = False,
    time_limit: Optional[float] = None,
    job_context: Optional[Dict] = None,
    resume_effort_floor: Optional[float] = None
):
    """Generic solver supporting multiple crop types, sizes, and mutation types."""
    prep = prepare_problem(req)
    req, default_crops, ctx, weights = prep.req, prep.default_crops, prep.ctx, prep.weights
    assert req.crops is not None
    assert req.mutations is not None

    # Check if locks are present
    has_locks = req.locks is not None and len(req.locks) > 0

    # Validate request and build lookups
    crop_defs, mutation_defs, maximize_mutations, target_mutations, cells, cell_set = validate_request(req)
    has_maximize = len(maximize_mutations) > 0

    # Validate locked placements if present
    locked_cells = set()
    locked_crop_positions: Dict[str, List[tuple]] = {}
    locks_as_dicts: List[Dict] = []

    if has_locks:
        locked_cells = validate_locked_placements(
            req.locks, cell_set, default_crops, DEFAULT_DATA['mutations']
        )

        for lock in req.locks:
            name = lock.name
            pos = tuple(lock.position)
            if name not in locked_crop_positions:
                locked_crop_positions[name] = []
            locked_crop_positions[name].append(pos)
        locks_as_dicts = [{"name": l.name, "size": l.size, "position": list(l.position)} for l in req.locks]

    hints = None
    if solvecache is not None and not has_locks:
        hints = solvecache.lookup(prep, cells, resume_effort_floor=resume_effort_floor)
        if hints.response is not None:
            return annotate_result(hints.response, cells, req, ctx)

    num_cells = len(cells)
    unified_budget = calculate_time_limit(
        num_cells, time_limit, target_mutations, maximize_mutations,
        hints.mutation_upper_bounds if hints else None,
        effects_on=ctx.active,
    )

    (filtered_crops, crop_valid_positions, crop_occupied_by_pos,
     mutation_feasible_positions, all_mutation_adjacent_cells) = compute_crop_and_mutation_positions(
        req, cells, cell_set, crop_defs,
        locked_cells=locked_cells if has_locks else None,
        locked_crop_positions=locked_crop_positions if has_locks else None,
        extra_crop_names=set(ctx.free_names),
    )

    unrestricted_cells: Optional[set] = None
    spread_possible = False
    if ctx.active:
        # Slots never relay, so only crops and locked plants can.
        plant_names = {c.name for c in filtered_crops}
        plant_names |= {l["name"] for l in locks_as_dicts}
        spread_possible = any(BUFF_TABLE[n].spreads for n in plant_names if n in BUFF_TABLE)
        if not spread_possible:
            unrestricted_cells = set()
            for mut in req.mutations:
                for pos in mutation_feasible_positions[mut.name]:
                    for cell in get_crop_cells(pos, mut.size):
                        unrestricted_cells.add(cell)
                        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                            q = (cell[0] + dr, cell[1] + dc)
                            if q in cell_set:
                                unrestricted_cells.add(q)

    # Create CP model
    model = cp_model.CpModel()

    # Create decision variables
    crop_vars, mutation_vars = create_decision_variables(
        model, req, filtered_crops, crop_valid_positions, crop_occupied_by_pos,
        mutation_feasible_positions, all_mutation_adjacent_cells,
        unrestricted_crops=set(ctx.free_names) if ctx.active else None,
        unrestricted_cells=unrestricted_cells,
    )

    mutation_chain_vars = create_multiplicity_chains(model, req, mutation_vars, cell_set)

    if has_locks:
        apply_locked_placements(
            model, locks_as_dicts, crop_vars, crop_occupied_by_pos, crop_defs, mutation_defs
        )

    # Build pre-computed data structures for constraints
    crop_occupied_cells: Dict[str, Dict[tuple, set]] = {
        crop_name: {pos: occ_cells for pos, occ_cells in pos_cells.items() if pos in crop_vars.get(crop_name, {})}
        for crop_name, pos_cells in crop_occupied_by_pos.items()
    }

    mutation_occupied_cells: Dict[str, Dict[tuple, set]] = {}
    mutation_adjacent_cells: Dict[str, Dict[tuple, set]] = {}
    for mut in req.mutations:
        mutation_occupied_cells[mut.name] = {}
        mutation_adjacent_cells[mut.name] = {}
        for pos in mutation_vars[mut.name]:
            occupied = set(get_crop_cells(pos, mut.size))
            mutation_occupied_cells[mut.name][pos] = occupied
            mutation_adjacent_cells[mut.name][pos] = get_adjacent_cells_for_mutation(pos, mut.size, cell_set)

    cell_usage = add_cell_usage_constraints(
        model, cells, crop_vars, mutation_vars, crop_occupied_cells, mutation_occupied_cells,
        locked_cells=locked_cells if has_locks else None
    )

    locked_crop_info = {}
    if has_locks:
        for lock in req.locks:
            crop_name = lock.name
            if crop_name not in locked_crop_info:
                locked_crop_info[crop_name] = []
            locked_crop_info[crop_name].append((tuple(lock.position), lock.size))

    add_mutation_eligibility_constraints(
        model, req, mutation_vars, crop_vars, mutation_adjacent_cells, crop_occupied_cells,
        locked_crop_info=locked_crop_info if has_locks else None,
        mutation_chain_vars=mutation_chain_vars,
    )

    add_symmetry_breaking_constraints(
        model, req, mutation_vars, target_mutations, maximize_mutations, cells, cell_set
    )

    cell_used_vars = create_cell_used_vars(model, cells, cell_usage)

    # Effect propagation circuit + weighted score terms
    effect_terms = []
    effect_stats: Dict = {}
    if ctx.active:
        present = {n: BUFF_TABLE[n] for n in
                   ({c.name for c in filtered_crops} | {l["name"] for l in locks_as_dicts})
                   if n in BUFF_TABLE}
        ctx.needed = needed_effects(weights, present, required=ctx.required)
        effect_model = build_effect_model(
            model, cells, cell_set, crop_vars, mutation_vars,
            crop_occupied_cells, mutation_occupied_cells,
            BUFF_TABLE, locks_as_dicts, ctx.needed,
        )
        effect_terms = add_effect_score_terms(
            effect_model, mutation_vars, mutation_occupied_cells, weights, mutation_defs
        )
        if ctx.required:
            forced_off = add_special_eligibility_constraints(
                model, effect_model, mutation_vars, mutation_occupied_cells
            )
            effect_model.stats["special_positions_forced_off"] = forced_off
        effect_stats = dict(effect_model.stats)

    scales = build_objective(
        model, has_maximize, maximize_mutations, target_mutations,
        mutation_vars, cell_used_vars, filtered_crops, crop_vars, num_cells,
        mutation_chain_vars=mutation_chain_vars,
        mutation_defs=mutation_defs,
        effect_terms=effect_terms,
        score_only=maximize_only,
    )

    if hints is not None:
        solvecache.apply(
            model, hints, req, crop_vars, mutation_vars, cell_used_vars,
            maximize_mutations, has_maximize, scales,
        )
    add_decision_strategy(model, has_maximize, cells, mutation_vars, effects_active=ctx.active)

    best_solver_values = {}
    use_captured_solution = False

    standard_callback = None
    if job_context:
        from solver_callbacks import SolverProgressCallback, update_job_phase
        job_manager = job_context.get('job_manager')
        job_id = job_context.get('job_id')
        start_time = job_context.get('start_time', time.time())
        if job_manager and job_id:
            update_job_phase(job_manager, job_id, "Solving", "Running optimization", start_time)
            crop_sizes = {crop_name: crop_defs[crop_name].size for crop_name in crop_vars.keys()}
            mutation_sizes = {mut.name: mut.size for mut in req.mutations}

            locked_for_callback = []
            if has_locks:
                for lock in req.locks:
                    locked_for_callback.append({
                        "name": lock.name,
                        "size": lock.size,
                        "position": tuple(lock.position)
                    })

            standard_callback = SolverProgressCallback(
                job_manager=job_manager,
                job_id=job_id,
                is_maximizing=True,
                crop_vars=crop_vars,
                mutation_vars=mutation_vars,
                cell_used_vars=cell_used_vars,
                crop_sizes=crop_sizes,
                mutation_sizes=mutation_sizes,
                locked_placements=locked_for_callback,
                time_limit=unified_budget,
                objective_scales=scales,
            )

    solver, status = solve_standard(model, unified_budget, callback=standard_callback)

    # Check if solver was cancelled but has a partial solution
    if standard_callback and standard_callback.was_cancelled and standard_callback.best_solution_values:
        use_captured_solution = True
        best_solver_values = standard_callback.best_solution_values
        status = cp_model.FEASIBLE

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise HTTPException(503, "No solution found")

    # Extract results
    placements, mutations_result, used_cells = extract_results(
        crop_vars, mutation_vars, crop_defs, req, use_captured_solution, best_solver_values,
        solver if not use_captured_solution else None
    )

    for placement in placements:
        placement["locked"] = False

    if has_locks:
        for lock in req.locks:
            placements.append({
                "crop": lock.name,
                "position": list(lock.position),
                "size": lock.size,
                "locked": True
            })

    placements, breakdown = prune_valueless_placements(
        cells, placements, mutations_result, DEFAULT_DATA['mutations'], req.targets,
        weights, BUFF_TABLE, mutation_defs, free_names=ctx.free_names,
    )
    used_cells = set()
    for p in placements:
        used_cells.update(get_crop_cells(tuple(p["position"]), p["size"]))
    for m in mutations_result:
        used_cells.update(get_crop_cells(tuple(m["position"]), m["size"]))

    if use_captured_solution:
        final_status = "OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE"
    else:
        final_status = solver.StatusName(status)

    result = {
        "status": final_status,
        "total_cells_used": len(used_cells),
        "placements": placements,
        "mutations": mutations_result,
        "cache_hit": hints.cache_hit if hints else None,
    }
    annotate_result(result, cells, req, ctx)
    if effect_stats:
        result["effect_model_stats"] = effect_stats

    if solvecache is not None and not maximize_only and not has_locks:
        solvecache.store(prep, cells, result, solvecache.Outcome(
            status=final_status,
            has_maximize=has_maximize,
            mutations=mutations_result,
            targets=req.targets,
            scales=scales,
            score=breakdown.score,
            cells_used=len(used_cells),
            best_bound=_best_bound(standard_callback, solver, use_captured_solution),
            solve_duration=(time.time() - job_context["start_time"]) if job_context else 0.0,
        ))

    return result


def run_job(
    request_params: Dict,
    job_id: str,
    job_manager,
    allow_client_time_limit: bool = False,
) -> Dict:
    """Greenhouse solve for the job worker (worker.py), reporting progress"""
    from solver_callbacks import update_job_phase

    start_time = time.time()
    request_type = request_params.get("_request_type", "greenhouse")

    # Remove internal fields before processing
    params = {k: v for k, v in request_params.items() if not k.startswith("_")}
    client_time_limit = params.pop("time_limit", None)
    time_limit = clamp_client_time_limit(client_time_limit) if allow_client_time_limit else None

    job_context = {
        'job_manager': job_manager,
        'job_id': job_id,
        'start_time': start_time
    }

    if request_type == "greenhouse":
        req = GenericSolveRequest(**params)
        update_job_phase(job_manager, job_id, "Initializing",
                         "Building constraint model", start_time)
        result = solve_generic(req, time_limit=time_limit, job_context=job_context)
        if hasattr(result, 'model_dump'):
            result = result.model_dump()
        elif not isinstance(result, dict):
            result = dict(result)
        return result

    raise ValueError(f"Unknown request type: {request_type}")
