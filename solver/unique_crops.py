# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from ortools.sat.python import cp_model

from .helpers import ADJACENCY_OFFSETS, get_crop_cells

UNIQUE_CROP_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("wheat",),
    ("potato",),
    ("carrot",),
    ("pumpkin",),
    ("melon",),
    ("cocoa_beans",),
    ("sugar_cane",),
    ("cactus",),
    ("nether_wart",),
    ("wild_rose",),
    ("red_mushroom", "brown_mushroom"),
    ("sunflower", "moonflower"),
)
MAX_UNIQUE_CROPS = len(UNIQUE_CROP_GROUPS)

_GROUP_OF: Dict[str, int] = {name: i for i, g in enumerate(UNIQUE_CROP_GROUPS) for name in g}


def group_of(name: str) -> Optional[int]:
    """Index of the buff slot `name` fills, or None if it fills none."""
    return _GROUP_OF.get(name)


@dataclass(frozen=True)
class UniqueCropsPlan:
    requested: int = 0
    target: int = 0                                  # requested, clamped to what can fit
    locked_groups: FrozenSet[int] = frozenset()      # already covered by locks
    open_groups: Tuple[int, ...] = ()                # the solver may fill these
    candidate_crops: Tuple[str, ...] = field(default=())

    @property
    def enabled(self) -> bool:
        return self.target > 0

    @property
    def needs_model(self) -> bool:
        """True when the locks alone do not reach the target."""
        return self.target > len(self.locked_groups)


NO_PLAN = UniqueCropsPlan()


def plan_unique_crops(requested: Optional[int], locks, num_cells: int) -> UniqueCropsPlan:
    """Work out what the solve has to add."""
    if not requested or requested <= 0:
        return NO_PLAN
    locks = locks or []
    locked = frozenset(g for g in (group_of(l.name) for l in locks) if g is not None)
    open_groups = tuple(i for i in range(MAX_UNIQUE_CROPS) if i not in locked)
    free_cells = max(0, num_cells - sum(l.size * l.size for l in locks))
    target = min(requested, MAX_UNIQUE_CROPS, len(locked) + min(len(open_groups), free_cells))
    candidates = tuple(n for i in open_groups for n in UNIQUE_CROP_GROUPS[i]) if target > len(locked) else ()
    return UniqueCropsPlan(
        requested=requested,
        target=target,
        locked_groups=locked,
        open_groups=open_groups,
        candidate_crops=candidates,
    )


def add_filler_positions(
    model: cp_model.CpModel,
    plan: UniqueCropsPlan,
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_valid_positions: Dict[str, List[tuple]],
) -> None:
    """Give every candidate crop a variable at every valid (lock-free) position."""
    if not plan.needs_model:
        return
    for name in plan.candidate_crops:
        positions = crop_vars.setdefault(name, {})
        for pos in crop_valid_positions.get(name, ()):
            if pos not in positions:
                positions[pos] = model.NewBoolVar(f"{name}_{pos}")


def add_unique_crops_constraint(
    model: cp_model.CpModel,
    plan: UniqueCropsPlan,
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
) -> bool:
    """Require at least `plan.target` groups on the grid."""
    if not plan.needs_model:
        return True
    need = plan.target - len(plan.locked_groups)
    have = []
    for g in plan.open_groups:
        lits = [v for name in UNIQUE_CROP_GROUPS[g] for v in crop_vars.get(name, {}).values()]
        if not lits:
            continue
        h = model.NewBoolVar(f"unique_group_{g}")
        model.AddBoolOr(lits).OnlyEnforceIf(h)
        have.append(h)
    if len(have) < need:
        return False
    model.Add(sum(have) >= need)
    return True


def covered_groups(placements: Sequence[Dict]) -> Set[int]:
    """Groups some placement (locked or not) fills."""
    out = set()
    for p in placements:
        g = group_of(p.get("crop") or p.get("name"))
        if g is not None:
            out.add(g)
    return out


def keeps_coverage(plan: UniqueCropsPlan) -> Optional[Callable[[List[Dict]], bool]]:
    """Guard for the pruning pass: a trial layout must still reach the target."""
    if not plan.enabled:
        return None
    return lambda placements: len(covered_groups(placements)) >= plan.target


def relocate_fillers(
    cells: Sequence[tuple],
    placements: List[Dict],
    mutation_slots: Sequence[Dict],
    score_fn: Callable[[List[Dict]], Tuple[float, float]],
    eligible_fn: Callable[[List[Dict]], bool],
    max_passes: int = 3,
) -> List[Dict]:
    """Local improvement for fillers the model mis-values."""
    cell_set = {tuple(c) for c in cells}
    slot_cells = set()
    for m in mutation_slots:
        slot_cells.update(get_crop_cells(tuple(m["position"]), m["size"]))
    slot_ring = {
        (r + dr, c + dc) for (r, c) in slot_cells for dr, dc in ADJACENCY_OFFSETS
    } - slot_cells

    def occupied(pl):
        used = set(slot_cells)
        for p in pl:
            used.update(get_crop_cells(tuple(p["position"]), p["size"]))
        return used

    current = list(placements)
    best_score, best_rate = score_fn(current)
    eps = 1e-9
    for _ in range(max_passes):
        moved = False
        movable = [
            p for p in current
            if not p.get("locked") and p["size"] == 1 and group_of(p["crop"]) is not None
            and tuple(p["position"]) in slot_ring
        ]
        for p in movable:
            empty = sorted(cell_set - occupied(current))
            best_move = None
            for cell in empty:
                q = {**p, "position": list(cell)}
                trial = [q if x is p else x for x in current]
                if not eligible_fn(trial):
                    continue
                s, r = score_fn(trial)
                better = (s > best_score + eps and r >= best_rate - eps) or (s >= best_score - eps and r > best_rate + eps)
                if better:
                    best_score, best_rate, best_move = s, r, trial
            if best_move is not None:
                current = best_move
                moved = True
        if not moved:
            break
    return current


def summarize(plan: UniqueCropsPlan, placements: Sequence[Dict]) -> Optional[Dict]:
    """The `unique_crops` block of a solve response."""
    if not plan.enabled:
        return None
    groups = covered_groups(placements)
    names = sorted({
        p.get("crop") or p.get("name") for p in placements
        if group_of(p.get("crop") or p.get("name")) is not None
    })
    return {
        "requested": plan.requested,
        "target": plan.target,
        "achieved": len(groups),
        "crops": names,
    }
