# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from math import gcd
from typing import List, Dict, Optional, Tuple, TYPE_CHECKING
from ortools.sat.python import cp_model

from .helpers import get_crop_cells, get_adjacent_cells_for_mutation
from .spawn import marginal_rates, solver_multiplicity_cap, weight_of
from .effects import ZERO_WEIGHT_SLOT_UNIT
from .effect_model import ObjectiveScales, common_gcd

if TYPE_CHECKING:
    from models import GenericSolveRequest


def create_decision_variables(
    model: cp_model.CpModel,
    req: "GenericSolveRequest",
    filtered_crops: List,
    crop_valid_positions: Dict[str, List[tuple]],
    crop_occupied_by_pos: Dict[str, Dict[tuple, set]],
    mutation_feasible_positions: Dict[str, List[tuple]],
    all_mutation_adjacent_cells: Dict[str, set],
    unrestricted_crops: Optional[set] = None,
    unrestricted_cells: Optional[set] = None,
) -> tuple:
    """Create decision variables for crops and mutations."""
    unrestricted_crops = unrestricted_crops or set()
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]] = {}

    for crop in filtered_crops:
        mutations_needing_crop = [
            mut for mut in req.mutations
            for r in mut.requirements if r.crop == crop.name
        ]
        useful_positions: List[tuple] = []
        if mutations_needing_crop:
            relevant_adjacent_cells = set()
            for mut in mutations_needing_crop:
                relevant_adjacent_cells.update(all_mutation_adjacent_cells.get(mut.name, set()))
            for pos in crop_valid_positions[crop.name]:
                if crop_occupied_by_pos[crop.name][pos] & relevant_adjacent_cells:
                    useful_positions.append(pos)

        if crop.name in unrestricted_crops:
            allowed = set(useful_positions)
            for pos in crop_valid_positions[crop.name]:
                if unrestricted_cells is None or (crop_occupied_by_pos[crop.name][pos] & unrestricted_cells):
                    allowed.add(pos)
            useful_positions = sorted(allowed)

        crop_vars[crop.name] = {
            pos: model.NewBoolVar(f"{crop.name}_{pos}")
            for pos in useful_positions
        }

    # Create mutation variables
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]] = {}
    for mut in req.mutations:
        mutation_vars[mut.name] = {
            pos: model.NewBoolVar(f"{mut.name}_{pos}")
            for pos in mutation_feasible_positions[mut.name]
        }

    return crop_vars, mutation_vars


def create_multiplicity_chains(
    model: cp_model.CpModel,
    req: "GenericSolveRequest",
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    cell_set: set,
) -> Dict[str, Dict[tuple, List[cp_model.IntVar]]]:
    """Represent each position's spawn multiplicity `k` as a chain of booleans."""
    chains: Dict[str, Dict[tuple, List[cp_model.IntVar]]] = {}

    for mut in req.mutations:
        if mut.requires_zero_adjacent:
            # Lonelily spawns at most once at an empty location.
            continue
        if weight_of(mut) <= 0:
            continue

        per_position: Dict[tuple, List[cp_model.IntVar]] = {}
        for pos, e_var in mutation_vars[mut.name].items():
            ring = len(get_adjacent_cells_for_mutation(pos, mut.size, cell_set))
            k_max = solver_multiplicity_cap(mut, ring)
            if k_max <= 1:
                continue

            chain = [e_var]
            for j in range(2, k_max + 1):
                chain.append(model.NewBoolVar(f"{mut.name}_{pos}_k{j}"))
            for lower, higher in zip(chain, chain[1:]):
                model.Add(higher <= lower)

            per_position[pos] = chain

        if per_position:
            chains[mut.name] = per_position

    return chains


def add_cell_usage_constraints(
    model: cp_model.CpModel,
    cells: List[tuple],
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_occupied_cells: Dict[str, Dict[tuple, set]],
    mutation_occupied_cells: Dict[str, Dict[tuple, set]],
    locked_cells: set = None
) -> Dict[tuple, List[cp_model.IntVar]]:
    """Add constraints ensuring each cell is used by at most one crop or mutation."""
    if locked_cells is None:
        locked_cells = set()

    # Exclude locked cells from the constraint tracking
    effective_cells = [c for c in cells if c not in locked_cells]
    cell_usage: Dict[tuple, List[cp_model.IntVar]] = {c: [] for c in effective_cells}

    for crop_name, positions in crop_vars.items():
        for pos, var in positions.items():
            for occupied_cell in crop_occupied_cells[crop_name][pos]:
                if occupied_cell in cell_usage:
                    cell_usage[occupied_cell].append(var)

    for mut_name, positions in mutation_vars.items():
        for pos, var in positions.items():
            for occupied_cell in mutation_occupied_cells[mut_name][pos]:
                if occupied_cell in cell_usage:
                    cell_usage[occupied_cell].append(var)

    for c, vars_list in cell_usage.items():
        if len(vars_list) > 1:
            model.AddAtMostOne(vars_list)

    return cell_usage


def add_mutation_eligibility_constraints(
    model: cp_model.CpModel,
    req: "GenericSolveRequest",
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_adjacent_cells: Dict[str, Dict[tuple, set]],
    crop_occupied_cells: Dict[str, Dict[tuple, set]],
    locked_crop_info: Optional[Dict[str, List[tuple]]] = None,
    mutation_chain_vars: Optional[Dict[str, Dict[tuple, List[cp_model.IntVar]]]] = None,
) -> None:
    """Add constraints for mutation eligibility based on adjacent crops."""
    if locked_crop_info is None:
        locked_crop_info = {}
    if mutation_chain_vars is None:
        mutation_chain_vars = {}

    crop_cell_index: Dict[str, Dict[tuple, List[tuple]]] = {}
    for crop_name, positions in crop_vars.items():
        index: Dict[tuple, List[tuple]] = {}
        occupied = crop_occupied_cells[crop_name]
        for crop_pos in positions:
            for cell in occupied[crop_pos]:
                index.setdefault(cell, []).append(crop_pos)
        crop_cell_index[crop_name] = index

    for mut in req.mutations:
        mut_chains = mutation_chain_vars.get(mut.name, {})
        for pos, e_var in mutation_vars[mut.name].items():
            mut_adjacent = mutation_adjacent_cells[mut.name][pos]
            chain = mut_chains.get(pos)

            # Handle requires_zero_adjacent for lonelily
            if mut.requires_zero_adjacent:
                for crop_name, positions in crop_vars.items():
                    index = crop_cell_index[crop_name]
                    adjacent_positions = set()
                    for cell in mut_adjacent:
                        adjacent_positions.update(index.get(cell, ()))
                    for crop_pos in adjacent_positions:
                        model.AddAtMostOne([e_var, positions[crop_pos]])
                # Also check locked placements
                for crop_name, locked_list in locked_crop_info.items():
                    for locked_pos, locked_size in locked_list:
                        locked_cells = set(get_crop_cells(locked_pos, locked_size))
                        if locked_cells & mut_adjacent:
                            model.Add(e_var == 0)
                            break
                continue

            for req_crop in mut.requirements:
                crop_name = req_crop.crop
                required_count = req_crop.count

                # Calculate fixed contribution from locked placements
                fixed_contribution = 0
                if crop_name in locked_crop_info:
                    for locked_pos, locked_size in locked_crop_info[crop_name]:
                        locked_cells = set(get_crop_cells(locked_pos, locked_size))
                        fixed_contribution += len(locked_cells & mut_adjacent)

                overlap_by_pos: Dict[tuple, int] = {}
                index = crop_cell_index.get(crop_name, {})
                for cell in sorted(mut_adjacent):
                    for crop_pos in index.get(cell, ()):
                        overlap_by_pos[crop_pos] = overlap_by_pos.get(crop_pos, 0) + 1

                if chain is not None:
                    crop_positions = crop_vars.get(crop_name, {})
                    variable_part = sum(
                        crop_positions[crop_pos] * count
                        for crop_pos, count in overlap_by_pos.items()
                    )
                    model.Add(
                        variable_part + fixed_contribution >= required_count * sum(chain)
                    )
                    continue

                # Build constraint based on fixed + variable contributions
                if fixed_contribution >= required_count:
                    pass
                elif not overlap_by_pos:
                    # No variable contributions and fixed is insufficient
                    model.Add(e_var == 0)
                else:
                    # Combine fixed and variable contributions
                    crop_positions = crop_vars[crop_name]
                    model.Add(
                        sum(crop_positions[crop_pos] * count for crop_pos, count in overlap_by_pos.items())
                        + fixed_contribution
                        >= required_count
                    ).OnlyEnforceIf(e_var)


def add_symmetry_breaking_constraints(
    model: cp_model.CpModel,
    req: "GenericSolveRequest",
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    target_mutations: Dict[str, int],
    maximize_mutations: List[str],
    cells: List[tuple],
    cell_set: set
) -> None:
    """Add symmetry breaking constraints for single-target mode."""
    return


def create_cell_used_vars(
    model: cp_model.CpModel,
    cells: List[tuple],
    cell_usage: Dict[tuple, List[cp_model.IntVar]]
) -> Dict[tuple, cp_model.IntVar]:
    """Create variables tracking which cells are used."""
    cell_used_vars = {}
    for c in cells:
        if c not in cell_usage:
            continue  # Skip locked cells
        usage_vars = cell_usage[c]
        if not usage_vars:
            continue
        elif len(usage_vars) == 1:
            cell_used_vars[c] = usage_vars[0]
        else:
            cell_used = model.NewBoolVar(f"used_{c}")
            model.AddMaxEquality(cell_used, usage_vars)
            cell_used_vars[c] = cell_used
    return cell_used_vars


def collect_base_terms(
    maximize_mutations: List[str],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_chain_vars: Optional[Dict[str, Dict[tuple, List[cp_model.IntVar]]]] = None,
    mutation_defs: Optional[Dict] = None,
) -> Tuple[List[Tuple[int, cp_model.IntVar]], bool]:
    """Raw (un-normalised) spawn-rate objective terms for the maximize targets:"""
    defs = mutation_defs or {}
    chains = mutation_chain_vars or {}
    per_position: List[Tuple[List[cp_model.IntVar], List[int]]] = []

    for mut_name in maximize_mutations:
        mut = defs.get(mut_name)
        mut_chains = chains.get(mut_name, {})
        for pos, e_var in mutation_vars[mut_name].items():
            chain = mut_chains.get(pos) or [e_var]
            if mut is None or weight_of(mut) <= 0:
                rates = [ZERO_WEIGHT_SLOT_UNIT] + [0] * (len(chain) - 1)
            else:
                rates = marginal_rates(mut, len(chain))
            per_position.append((chain, rates))

    terms: List[Tuple[int, cp_model.IntVar]] = []
    distinct_rates = set()
    for chain, rates in per_position:
        for var, rate in zip(chain, rates):
            if rate:
                terms.append((rate, var))
                distinct_rates.add(rate)
    plain_shape = all(len(chain) == 1 for chain, _ in per_position) and len(distinct_rates) <= 1
    return terms, plain_shape


def build_maximize_expr(
    maximize_mutations: List[str],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_chain_vars: Optional[Dict[str, Dict[tuple, List[cp_model.IntVar]]]] = None,
    mutation_defs: Optional[Dict] = None,
) -> Tuple[object, bool]:
    """GCD-normalised spawn-rate expression (see collect_base_terms)."""
    terms, plain_shape = collect_base_terms(
        maximize_mutations, mutation_vars, mutation_chain_vars, mutation_defs
    )
    if not terms:
        all_max_vars: List[cp_model.IntVar] = []
        for mut_name in maximize_mutations:
            all_max_vars.extend(mutation_vars[mut_name].values())
        return sum(all_max_vars), True
    divisor = common_gcd(c for c, _ in terms)
    return sum((c // divisor) * var for c, var in terms), plain_shape


def build_objective(
    model: cp_model.CpModel,
    has_maximize: bool,
    maximize_mutations: List[str],
    target_mutations: Dict[str, int],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    cell_used_vars: Dict[tuple, cp_model.IntVar],
    filtered_crops: List,
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    num_cells: int,
    mutation_chain_vars: Optional[Dict[str, Dict[tuple, List[cp_model.IntVar]]]] = None,
    mutation_defs: Optional[Dict] = None,
    effect_terms: Optional[List[Tuple[int, cp_model.IntVar]]] = None,
    score_only: bool = False,
) -> ObjectiveScales:
    """Build the single optimisation objective:"""
    for mut_name, target_count in target_mutations.items():
        model.Add(sum(mutation_vars[mut_name].values()) == target_count)

    base_terms: List[Tuple[int, cp_model.IntVar]] = []
    plain_shape = True
    if has_maximize:
        base_terms, plain_shape = collect_base_terms(
            maximize_mutations, mutation_vars, mutation_chain_vars, mutation_defs
        )
    effect_terms = list(effect_terms or [])
    score_terms = base_terms + effect_terms
    divisor = common_gcd(c for c, _ in score_terms)
    score_expr = sum((c // divisor) * lit for c, lit in score_terms) if score_terms else 0

    priority_terms = []
    for crop in filtered_crops:
        if crop.priority > 0:
            crop_placement_vars = list(crop_vars.get(crop.name, {}).values())
            if crop_placement_vars:
                priority_terms.append(crop.priority * sum(crop_placement_vars))
    priority_expr = sum(priority_terms) if priority_terms else 0

    cells_expr = sum(cell_used_vars.values()) if cell_used_vars else 0

    if score_only and score_terms:
        model.Maximize(score_expr)
        return ObjectiveScales(
            score_scale=1,
            priority_scale=1,
            gcd=divisor,
            is_plain_count=plain_shape and not effect_terms,
            objective_is_cells_only=False,
            has_score_terms=True,
        )

    PRIORITY_SCALE = num_cells + 1
    if priority_terms:
        max_priority = max(c.priority for c in filtered_crops if c.priority > 0)
        priority_max = PRIORITY_SCALE * max_priority * num_cells
    else:
        priority_max = 0
    SCORE_SCALE = priority_max + num_cells + 1

    model.Maximize(SCORE_SCALE * score_expr - PRIORITY_SCALE * priority_expr - cells_expr)

    return ObjectiveScales(
        score_scale=SCORE_SCALE,
        priority_scale=PRIORITY_SCALE,
        gcd=divisor,
        is_plain_count=bool(score_terms) and plain_shape and not effect_terms,
        objective_is_cells_only=not score_terms and not priority_terms,
        has_score_terms=bool(score_terms),
    )


def add_decision_strategy(
    model: cp_model.CpModel,
    has_maximize: bool,
    cells: List[tuple],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    effects_active: bool = False,
) -> None:
    """Add a centre-out decision strategy for pure fixed-count mode. Skipped for"""
    if has_maximize or effects_active or not cells:
        return

    center_r = sum(c[0] for c in cells) / len(cells)
    center_c = sum(c[1] for c in cells) / len(cells)

    for mut_name, positions in mutation_vars.items():
        if positions:
            center_sorted = sorted(
                positions.keys(),
                key=lambda p: abs(p[0] - center_r) + abs(p[1] - center_c)
            )
            model.AddDecisionStrategy(
                [positions[p] for p in center_sorted],
                cp_model.CHOOSE_FIRST,
                cp_model.SELECT_MAX_VALUE
            )


def apply_locked_placements(
    model: cp_model.CpModel,
    locks: List[Dict],
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_occupied_cells: Dict[str, Dict[tuple, set]],
    crop_defs: Dict,
    mutation_defs: Dict
) -> Dict[str, int]:
    """Apply constraints for locked placements and compute their contributions."""
    locked_contributions: Dict[str, int] = {}

    if not locks:
        return locked_contributions

    for lock in locks:
        name = lock["name"]
        pos = tuple(lock["position"])
        size = lock["size"]

        # Check if this is a crop that has decision variables
        if name in crop_vars and pos in crop_vars[name]:
            # Force this crop placement
            model.Add(crop_vars[name][pos] == 1)

    return locked_contributions


def extract_results(
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_defs: Dict,
    req: "GenericSolveRequest",
    use_iterative: bool,
    best_solver_values: Dict,
    solver: cp_model.CpSolver = None
) -> tuple:
    """Extract results from solved model."""
    def get_var_value(var_type, name, pos):
        if use_iterative and best_solver_values:
            return best_solver_values[var_type].get(name, {}).get(pos, 0)
        return solver.Value(crop_vars[name][pos] if var_type == 'crop_vars' else mutation_vars[name][pos])

    placements = []
    for crop_name, positions in crop_vars.items():
        crop_size = crop_defs[crop_name].size
        crop_placements = []
        for pos in positions.keys():
            if get_var_value('crop_vars', crop_name, pos):
                crop_placements.append({
                    "crop": crop_name,
                    "position": list(pos),
                    "size": crop_size
                })
        placements.extend(crop_placements)

    mutations_result = []
    for mut in req.mutations:
        mutation_placements = []
        for pos in mutation_vars[mut.name].keys():
            if get_var_value('mutation_vars', mut.name, pos):
                mutation_placements.append({
                    "mutation": mut.name,
                    "position": list(pos),
                    "size": mut.size
                })
        if mutation_placements:
            mutations_result.extend(mutation_placements)

    # Count total cells used
    used_cells = set()
    for crop_name, positions in crop_vars.items():
        crop_size = crop_defs[crop_name].size
        for pos in positions.keys():
            if get_var_value('crop_vars', crop_name, pos):
                for cell in get_crop_cells(pos, crop_size):
                    used_cells.add(cell)
    for mut in req.mutations:
        for pos in mutation_vars[mut.name].keys():
            if get_var_value('mutation_vars', mut.name, pos):
                for cell in get_crop_cells(pos, mut.size):
                    used_cells.add(cell)

    return placements, mutations_result, used_cells
