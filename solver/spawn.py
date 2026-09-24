# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import os
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .helpers import get_adjacent_cells_for_mutation, get_crop_cells

BLANK_FILL_TO = 100

RATE_SCALE = 10_000

MULTIPLICITY_SEARCH_CAP = int(os.environ.get("SKYSHARDS_MULTIPLICITY_CAP", "3"))


def _get(mut, key, default=None):
    if isinstance(mut, dict):
        return mut.get(key, default)
    return getattr(mut, key, default)


def requirements_of(mut) -> List[Tuple[str, int]]:
    """Normalize requirements to a list of (crop_name, count)."""
    out = []
    for r in _get(mut, "requirements", []) or []:
        if isinstance(r, dict):
            out.append((r["crop"], int(r["count"])))
        else:
            out.append((r.crop, int(r.count)))
    return out


def weight_of(mut) -> int:
    return int(_get(mut, "spawn_weight", 0) or 0)


def name_of(mut) -> str:
    return _get(mut, "name")


def requires_zero_adjacent(mut) -> bool:
    if bool(_get(mut, "requires_zero_adjacent", False)):
        return True
    return _get(mut, "special", None) == "requires_zero_adjacent"


def special_of(mut) -> Optional[str]:
    return _get(mut, "special", None)


SPECIAL_ALL_POSITIVE = "all_positive_crop_effects"


def is_all_positive_special(mut) -> bool:
    return special_of(mut) == SPECIAL_ALL_POSITIVE


SCALING_QUIRKS: Dict[str, Dict] = {
    "ashwreath": {
        "scaling_crop": "nether_wart",
        "full_weight_count": 4,
    },
}


def scaling_requirement(mut) -> Optional[Tuple[str, int]]:
    """The (crop, base requirement count) that drives this mutation's weight past
    the minimum eligible value, or None for the common case where eligibility
    alone already means the listed spawn_weight (see SCALING_QUIRKS)."""
    quirk = SCALING_QUIRKS.get(name_of(mut))
    if not quirk:
        return None
    target_crop = quirk["scaling_crop"]
    for crop, count in requirements_of(mut):
        if crop == target_crop:
            return crop, count
    return None


def full_weight_multiplicity(mut) -> int:
    """k at which this mutation's weight reaches its listed spawn_weight."""
    scaling = scaling_requirement(mut)
    if scaling is None:
        return 1
    _, base_count = scaling
    full_count = SCALING_QUIRKS[name_of(mut)]["full_weight_count"]
    return max(1, full_count - base_count + 1)


def max_multiplicity(mut, ring: int) -> int:
    """Upper bound on `k` for a mutation whose adjacency ring holds `ring` cells."""
    scaling = scaling_requirement(mut)
    if scaling is None:
        return 1
    _, base_count = scaling
    if base_count <= 0:
        return 1
    required = sum(count for _, count in requirements_of(mut))
    room = ring - required
    if room < 0:
        return 0
    return max(0, min(room + 1, full_weight_multiplicity(mut)))


def solver_multiplicity_cap(mut, ring: int) -> int:
    """The largest `k` the solver bothers to model at a position."""
    return min(max_multiplicity(mut, ring), MULTIPLICITY_SEARCH_CAP)


def multiplicity(mut, adjacent_counts: Dict[str, int], special_eligible: Optional[bool] = None) -> int:
    """How many multiplicity units this location has earned toward this
    mutation's weight - 1 as soon as every requirement is met at least once,
    and higher only for a mutation with a scaling quirk (see SCALING_QUIRKS),
    capped at the k that already reaches the listed spawn_weight."""
    if requires_zero_adjacent(mut):
        return 1 if not any(adjacent_counts.values()) else 0

    if is_all_positive_special(mut):
        return 1 if special_eligible else 0

    reqs = requirements_of(mut)
    if not reqs:
        return 0

    for crop, count in reqs:
        if adjacent_counts.get(crop, 0) < count:
            return 0

    scaling = scaling_requirement(mut)
    if scaling is None:
        return 1

    crop, base_count = scaling
    k = adjacent_counts.get(crop, 0) - base_count + 1
    return max(1, min(k, full_weight_multiplicity(mut)))


def pool_denominator(pool_weights: Iterable[int]) -> int:
    """max(100, sum of weights) - the blank tops the pool up to 100."""
    return max(BLANK_FILL_TO, sum(pool_weights))


def spawn_probability(target: str, pool: Dict[str, int]) -> float:
    """Chance that `target` is the pick at a location, given the pool of effective
    weights (already multiplied by multiplicity) keyed by mutation name."""
    if not pool:
        return 0.0
    return pool.get(target, 0) / pool_denominator(pool.values())


def effective_weight(mut, k: int) -> float:
    """Pool weight this mutation contributes at multiplicity `k`."""
    weight = weight_of(mut)
    if weight <= 0 or k <= 0:
        return 0.0
    scaling = scaling_requirement(mut)
    if scaling is None:
        return float(weight)
    _, base_count = scaling
    full_count = SCALING_QUIRKS[name_of(mut)]["full_weight_count"]
    if full_count <= 0:
        return float(weight)
    scaling_crops = base_count + k - 1
    return min(float(weight), weight * scaling_crops / full_count)


def build_pool(
    mutations: Sequence, adjacent_counts: Dict[str, int], special_eligible: Optional[bool] = None
) -> Dict[str, float]:
    """Effective weight per mutation (see effective_weight), dropping zeros."""
    pool = {}
    for mut in mutations:
        k = multiplicity(mut, adjacent_counts, special_eligible=special_eligible)
        if k > 0:
            w = effective_weight(mut, k)
            if w > 0:
                pool[name_of(mut)] = w
    return pool


def marginal_rates(mut, k_max: int) -> List[int]:
    """Scaled marginal gain of each successive multiplicity unit, sole-candidate case."""
    scaled = [0]
    for k in range(1, max(k_max, 0) + 1):
        scaled.append(min(RATE_SCALE, int(RATE_SCALE * effective_weight(mut, k) // BLANK_FILL_TO)))

    out = [scaled[k] - scaled[k - 1] for k in range(1, len(scaled))]

    for i in range(1, len(out)):
        assert out[i] <= out[i - 1], (
            f"marginal rates for {name_of(mut)} are not non-increasing: {out}"
        )
    return out


def candidate_mutations(placed_crop_names: Iterable[str], all_mutations: Sequence) -> List:
    """Mutations that could appear in some location's pool given the crops present."""
    placed = set(placed_crop_names)
    out = []
    for mut in all_mutations:
        if weight_of(mut) <= 0:
            continue
        special = special_of(mut)
        if special and special != "requires_zero_adjacent":
            continue
        reqs = requirements_of(mut)
        if not reqs and not requires_zero_adjacent(mut):
            continue
        if all(crop in placed for crop, _ in reqs):
            out.append(mut)
    return out


def adjacent_crop_counts(
    position: tuple,
    size: int,
    cell_set: set,
    cell_owner: Dict[tuple, str],
) -> Dict[str, int]:
    """Count adjacent cells by the crop occupying them."""
    counts: Dict[str, int] = {}
    for cell in get_adjacent_cells_for_mutation(position, size, cell_set):
        owner = cell_owner.get(cell)
        if owner is not None:
            counts[owner] = counts.get(owner, 0) + 1
    return counts


def build_cell_owner(placements: Sequence) -> Dict[tuple, str]:
    """Map each occupied cell to the name of the crop occupying it."""
    owner: Dict[tuple, str] = {}
    for p in placements:
        if isinstance(p, dict):
            name = p.get("crop") or p.get("name")
            pos = tuple(p["position"])
            size = int(p.get("size", 1))
        else:
            name = getattr(p, "crop", None) or getattr(p, "name")
            pos = tuple(p.position)
            size = int(getattr(p, "size", 1))
        if name is None:
            continue
        for cell in get_crop_cells(pos, size):
            owner[cell] = name
    return owner


def score_layout(
    cells: Sequence,
    placements: Sequence,
    mutation_slots: Sequence,
    all_mutations: Sequence,
    special_eligible_fn=None,
) -> Dict[str, float]:
    """Exact expected spawns per tick for a concrete layout, keyed by mutation name."""
    cell_set = {tuple(c) for c in cells}
    cell_owner = build_cell_owner(placements)
    by_name = {name_of(m): m for m in all_mutations}
    candidates = candidate_mutations(set(cell_owner.values()), all_mutations)

    rates: Dict[str, float] = {}
    for slot in mutation_slots:
        if isinstance(slot, dict):
            target = slot.get("mutation") or slot.get("name")
            pos = tuple(slot["position"])
            size = int(slot.get("size", 1))
        else:
            target = getattr(slot, "mutation", None) or getattr(slot, "name")
            pos = tuple(slot.position)
            size = int(getattr(slot, "size", 1))

        if target not in by_name:
            continue

        counts = adjacent_crop_counts(pos, size, cell_set, cell_owner)
        pool_muts = list(candidates)
        if by_name[target] not in pool_muts:
            pool_muts.append(by_name[target])

        special_eligible = None
        if special_eligible_fn is not None and is_all_positive_special(by_name[target]):
            special_eligible = bool(special_eligible_fn(target, pos, size))
        pool = build_pool(pool_muts, counts, special_eligible=special_eligible)
        p = spawn_probability(target, pool)
        if p > 0:
            rates[target] = rates.get(target, 0.0) + p
    return rates
