# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from dataclasses import dataclass, field
from functools import reduce
from math import gcd
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ortools.sat.python import cp_model

from .effects import (
    EFFECT_INERT_KINDS,
    IMPROVED_OF,
    NEGATIVE_EFFECTS,
    RELAY_EFFECT,
    SPECIAL_EFFECT_SETS,
    PlantBuffs,
    slot_unit_int,
)
from .helpers import get_crop_cells

Literal = Union[bool, cp_model.IntVar, object]

CARDINAL_ORDER = {
    "N": (-1, 0),
    "W": (0, -1),
    "E": (0, 1),
    "S": (1, 0),
}


class _Builder:
    """Creates exact OR/AND literals with constant folding and hash-consing."""

    def __init__(self, model: cp_model.CpModel):
        self.model = model
        self.num_bools = 0
        self.num_constraints = 0
        self._or_memo: Dict[frozenset, Literal] = {}
        self._and_memo: Dict[frozenset, Literal] = {}
        self._sum_memo: Dict[frozenset, Literal] = {}

    def new_bool(self, name: str):
        self.num_bools += 1
        return self.model.NewBoolVar(name)

    @staticmethod
    def neg(lit):
        if lit is True:
            return False
        if lit is False:
            return True
        return lit.Not()

    @staticmethod
    def _operands(lits: Iterable, absorbing: bool):
        """Distinct non-constant operands keyed by literal index, or the
        `absorbing` constant when one operand (or a complementary pair)
        decides the gate."""
        kept: Dict[int, Literal] = {}
        for l in lits:
            if l is absorbing:
                return absorbing
            if l is (not absorbing):
                continue
            k = l.Index()
            if ~k in kept:
                return absorbing
            if k not in kept:
                kept[k] = l
        return kept

    def or_(self, lits: Iterable, name: str):
        kept = self._operands(lits, True)
        if kept is True:
            return True
        if not kept:
            return False
        if len(kept) == 1:
            return next(iter(kept.values()))
        key = frozenset(kept)
        y = self._or_memo.get(key)
        if y is not None:
            return y
        ops = list(kept.values())
        y = self.new_bool(name)
        self.model.AddBoolOr(ops).OnlyEnforceIf(y)
        self.model.AddBoolAnd([self.neg(l) for l in ops]).OnlyEnforceIf(y.Not())
        self.num_constraints += 2
        self._or_memo[key] = y
        return y

    def and_(self, lits: Iterable, name: str):
        kept = self._operands(lits, False)
        if kept is False:
            return False
        if not kept:
            return True
        if len(kept) == 1:
            return next(iter(kept.values()))
        key = frozenset(kept)
        y = self._and_memo.get(key)
        if y is not None:
            return y
        y = self.new_bool(name)
        self._define_and(y, list(kept.values()))
        self._and_memo[key] = y
        return y

    def _define_and(self, y, ops: List) -> None:
        self.model.AddBoolAnd(ops).OnlyEnforceIf(y)
        self.model.AddBoolOr([self.neg(l) for l in ops] + [y])
        self.num_constraints += 2

    def clause(self, lits: Iterable) -> None:
        """Add OR(lits) as a constraint, folding constants."""
        ops = []
        for l in lits:
            if l is True:
                return
            if l is False:
                continue
            ops.append(l)
        if not ops:
            raise ValueError("empty clause: the effect circuit is contradictory")
        self.model.AddBoolOr(ops)
        self.num_constraints += 1

    def as_var(self, lit, name: str):
        """Materialize a literal as a plain BoolVar (objective terms need one)."""
        if lit is True or lit is False or isinstance(lit, cp_model.IntVar):
            return lit
        y = self.new_bool(name)
        self.model.Add(y == lit)
        self.num_constraints += 1
        return y

    def exactly_one_of(self, vars_list: List, name: str):
        """A literal equal to the sum of mutually exclusive booleans."""
        if not vars_list:
            return False
        if len(vars_list) == 1:
            return vars_list[0]
        key = frozenset(v.Index() for v in vars_list)
        y = self._sum_memo.get(key)
        if y is not None:
            return y
        y = self.new_bool(name)
        self.model.Add(y == sum(vars_list))
        self.num_constraints += 1
        self._sum_memo[key] = y
        return y


@dataclass
class EffectModel:
    effects: Tuple[str, ...]
    has_final: Dict[tuple, Dict[str, Literal]]
    spread_possible: bool
    stats: Dict[str, int] = field(default_factory=dict)
    _builder: Optional[_Builder] = None
    _slot_cache: Dict[Tuple[tuple, ...], Dict[str, Literal]] = field(default_factory=dict)
    _eff_cache: Dict[Tuple[Tuple[tuple, ...], str], Literal] = field(default_factory=dict)

    def slot_has(self, plant_cells: Sequence[tuple]) -> Dict[str, Literal]:
        """Raw held set of a plant occupying `plant_cells` (OR over its cells)."""
        key = tuple(sorted(plant_cells))
        cached = self._slot_cache.get(key)
        if cached is not None:
            return cached
        out = {}
        for e in self.effects:
            lits = [self.has_final.get(c, {}).get(e, False) for c in key]
            out[e] = self._builder.or_(lits, f"slothas_{key[0]}_{e}")
        self._slot_cache[key] = out
        return out

    def slot_effective(self, plant_cells: Sequence[tuple], effect: str) -> Literal:
        """Effective (scored) literal for one effect of a plant at `plant_cells`:
        immunity cancels negatives, improved variants hide their base."""
        key = (tuple(sorted(plant_cells)), effect)
        if key in self._eff_cache:
            return self._eff_cache[key]
        has = self.slot_has(plant_cells)
        lit = has.get(effect, False)
        if lit is False:
            self._eff_cache[key] = False
            return False
        blockers = []
        if effect in NEGATIVE_EFFECTS and "immunity" in has:
            blockers.append(self._builder.neg(has["immunity"]))
        improved = IMPROVED_OF.get(effect)
        if improved is not None and improved in has:
            blockers.append(self._builder.neg(has[improved]))
        if blockers:
            lit = self._builder.and_([lit] + blockers, f"sloteff_{key[0][0]}_{effect}")
        self._eff_cache[key] = lit
        return lit


def build_effect_model(
    model: cp_model.CpModel,
    cells: Sequence[tuple],
    cell_set: set,
    crop_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    crop_occupied_cells: Dict[str, Dict[tuple, set]],
    mutation_occupied_cells: Dict[str, Dict[tuple, set]],
    buff_table: Dict[str, PlantBuffs],
    locked_placements: Optional[Sequence[Dict]],
    needed: Iterable[str],
) -> EffectModel:
    """Encode direct effects plus the relay fixpoint for the effects
    in `needed`, and return the final held-set literals per cell."""
    b = _Builder(model)
    effects = tuple(sorted(needed))
    if not effects:
        return EffectModel(effects=(), has_final={}, spread_possible=False, stats={}, _builder=b)
    relayed = tuple(e for e in effects if e != RELAY_EFFECT)
    tracked = tuple(sorted(set(effects) | {RELAY_EFFECT}))
    order = sorted(cell_set)

    placements: List[Tuple[Literal, frozenset, tuple, frozenset]] = []
    placement_names: List[str] = []
    for name, positions in crop_vars.items():
        occ_map = crop_occupied_cells.get(name, {})
        buffs = buff_table.get(name)
        listed = frozenset(buffs.intrinsic) if buffs is not None else frozenset()
        for pos, var in positions.items():
            occ = frozenset(c for c in occ_map.get(pos, ()) if c in cell_set)
            if occ:
                placements.append((var, occ, tuple(pos), listed))
                placement_names.append(name)
    for lock in locked_placements or []:
        name = lock.get("name") or lock.get("crop")
        buffs = buff_table.get(name)
        listed = frozenset(buffs.intrinsic) if buffs is not None else frozenset()
        pos = tuple(lock["position"])
        occ = frozenset(c for c in get_crop_cells(pos, int(lock.get("size", 1))) if c in cell_set)
        if occ:
            placements.append((True, occ, pos, listed))
            placement_names.append(name)

    covering: Dict[tuple, List[int]] = {c: [] for c in cell_set}
    for i, (_, occ, _, _) in enumerate(placements):
        for c in occ:
            covering[c].append(i)
    single_at: Dict[tuple, List[int]] = {
        c: [i for i in covering[c] if len(placements[i][1]) == 1] for c in cell_set
    }
    multis = [i for i, p in enumerate(placements) if len(p[1]) > 1]
    inert_idx = {i for i, nm in enumerate(placement_names) if nm in EFFECT_INERT_KINDS}
    inert_single_at: Dict[tuple, List[int]] = {
        c: [i for i in single_at[c] if i in inert_idx] for c in cell_set
    }

    def nbrs(cell):
        r, c = cell
        out = []
        for d in ("N", "W", "E", "S"):
            dr, dc = CARDINAL_ORDER[d]
            q = (r + dr, c + dc)
            if q in cell_set:
                out.append(q)
        return out

    def any_of(indices, name) -> Literal:
        """Exact OR of mutually exclusive placement literals (they share a tile)."""
        lits = [placements[i][0] for i in indices]
        if any(l is True for l in lits):
            return True
        return b.exactly_one_of([l for l in lits if l is not False], name)

    # ---- direct effects
    gives_cache: Dict[Tuple[tuple, str], Literal] = {}

    def gives_to(q, c, e) -> Literal:
        """The plant on q lists e and is not also on c."""
        idx = [i for i in covering[q] if e in placements[i][3]]
        if not idx:
            return False
        kept = [i for i in idx if c not in placements[i][1]]
        if len(kept) == len(idx):
            key = (q, e)
            if key not in gives_cache:
                gives_cache[key] = any_of(idx, f"gives_{q}_{e}")
            return gives_cache[key]
        return any_of(kept, f"gives_{q}_{c}_{e}") if kept else False

    raw_direct: Dict[tuple, Dict[str, Literal]] = {
        c: {e: b.or_([gives_to(q, c, e) for q in nbrs(c)], f"direct_{c}_{e}") for e in tracked}
        for c in order
    }

    inert_present = {c: any_of(inert_single_at[c], f"inertpresent_{c}") for c in order}
    direct: Dict[tuple, Dict[str, Literal]] = {
        c: {e: b.and_([raw_direct[c][e], b.neg(inert_present[c])], f"directmasked_{c}_{e}") for e in tracked}
        for c in order
    }

    single_present = {c: any_of(single_at[c], f"single_{c}") for c in order}
    tile_spreader = {c: b.and_([single_present[c], direct[c][RELAY_EFFECT]], f"spreader_{c}") for c in order}
    multi_held: Dict[int, Dict[str, Literal]] = {}
    multi_spreader: Dict[int, Literal] = {}
    for i in multis:
        lit, occ, anchor, _ = placements[i]
        cs = sorted(occ)
        multi_held[i] = {e: b.or_([direct[c][e] for c in cs], f"mheld_{anchor}_{i}_{e}") for e in tracked}
        multi_spreader[i] = b.and_([lit, multi_held[i][RELAY_EFFECT]], f"mspreader_{anchor}_{i}")

    unit_lits = [tile_spreader[c] for c in order] + [multi_spreader[i] for i in multis]
    live = [l for l in unit_lits if l is not False]
    spread_possible = bool(live) and bool(relayed)

    def entity_or_tile(per_cell):
        """A multi-cell plant's tiles all hold the plant's one set."""
        out = {}
        for c in order:
            out[c] = {}
            for e in per_cell[c]:
                terms = [per_cell[c][e]]
                for i in covering[c]:
                    if i in multi_held:
                        whole = b.or_([per_cell[x][e] for x in sorted(placements[i][1])], f"ment_{i}_{e}")
                        terms.append(b.and_([placements[i][0], whole], f"mtile_{c}_{i}_{e}"))
                out[c][e] = b.or_(terms, f"hold_{c}_{e}")
        return out

    def finish(has_final, levels_used=0):
        stats = {
            "num_bools": b.num_bools, "num_constraints": b.num_constraints, "effects": len(effects),
            "possible_spreaders": len(live), "fixpoint_levels": levels_used,
            "multi_cell_units": len(multis),
        }
        return EffectModel(effects=effects, has_final=has_final, spread_possible=spread_possible,
                           stats=stats, _builder=b)

    if not spread_possible:
        base = entity_or_tile({c: {e: direct[c][e] for e in effects} for c in order}) if multis else \
            {c: {e: direct[c][e] for e in effects} for c in order}
        return finish(base)

    def neighbour_units(target_cells, own):
        """Units next to `target_cells` (not `own`, not overlapping it), as
        (kind, id) in a deterministic order."""
        own_occ = placements[own][1] if own is not None else frozenset()
        out = []
        seen_tiles, seen_multis = set(), set()
        for x in target_cells:
            for q in nbrs(x):
                if q in target_cells:
                    continue
                if q not in seen_tiles:
                    seen_tiles.add(q)
                    out.append(("tile", q))
                for w in covering[q]:
                    if w not in multi_held or w == own or w in seen_multis:
                        continue
                    if own_occ & placements[w][1] or any(t in placements[w][1] for t in target_cells):
                        continue
                    seen_multis.add(w)
                    out.append(("multi", w))
        return out

    units = [("tile", c) for c in order] + [("multi", i) for i in multis]
    unit_spreader = {("tile", c): tile_spreader[c] for c in order}
    unit_spreader.update({("multi", i): multi_spreader[i] for i in multis})
    unit_held = {("tile", c): direct[c] for c in order}
    unit_held.update({("multi", i): multi_held[i] for i in multis})
    unit_nbrs = {("tile", c): neighbour_units((c,), None) for c in order}
    unit_nbrs.update({("multi", i): neighbour_units(tuple(sorted(placements[i][1])), i) for i in multis})

    can_push = {
        u: {e: unit_spreader[u] is not False and unit_held[u][e] is not False for e in relayed}
        for u in units
    }
    changed = True
    while changed:
        changed = False
        for u in units:
            if unit_spreader[u] is False:
                continue
            for e in relayed:
                if not can_push[u][e] and any(can_push[w][e] for w in unit_nbrs[u]):
                    can_push[u][e] = True
                    changed = True

    push: Dict[tuple, Dict[str, Literal]] = {u: {} for u in units}
    levels_used = 0
    for e in relayed:
        live_units = [u for u in units if can_push[u][e]]
        feeders = {u: [w for w in unit_nbrs[u] if can_push[w][e]] for u in live_units}
        for u in units:
            if not can_push[u][e]:
                push[u][e] = False
            elif not feeders[u]:
                push[u][e] = b.and_([unit_spreader[u], unit_held[u][e]], f"push_{u}_{e}")
        cyclic = [u for u in live_units if feeders[u]]
        component: Dict[tuple, int] = {}
        sizes: List[int] = []
        for start in cyclic:
            if start in component:
                continue
            k = len(sizes)
            component[start] = k
            stack, size = [start], 0
            while stack:
                u = stack.pop()
                size += 1
                for w in feeders[u]:
                    if w not in component and feeders.get(w):
                        component[w] = k
                        stack.append(w)
            sizes.append(size)
        for u in cyclic:
            push[u][e] = b.new_bool(f"push_{u}_{e}")
        level = {}
        for u in cyclic:
            level[u] = model.NewIntVar(0, max(0, sizes[component[u]] - 1), f"level_{u}_{e}")
            levels_used += 1
        for u in cyclic:
            y, spr, held = push[u][e], unit_spreader[u], unit_held[u][e]
            b.clause([b.neg(spr), b.neg(held), y])
            for w in feeders[u]:
                b.clause([b.neg(spr), b.neg(push[w][e]), y])
            b.clause([b.neg(y), spr])
            support = []
            for w in feeders[u]:
                s = b.new_bool(f"support_{w}_{u}_{e}")
                model.AddImplication(s, push[w][e])
                if w in level:
                    model.Add(level[w] < level[u]).OnlyEnforceIf(s)
                    b.num_constraints += 1
                b.num_constraints += 1
                support.append(s)
            b.clause([b.neg(y), held] + support)

    chosen = {}
    for c in order:
        into = unit_nbrs[("tile", c)]
        chosen[c] = {
            e: b.or_([direct[c][e]] + [push[w][e] for w in into], f"final_{c}_{e}")
            for e in relayed
        }

    per_cell = {c: {e: (direct[c][e] if e == RELAY_EFFECT else chosen[c][e]) for e in effects} for c in order}
    has_final = entity_or_tile(per_cell) if multis else per_cell
    return finish(has_final, levels_used)


def add_effect_score_terms(
    effect_model: EffectModel,
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_occupied_cells: Dict[str, Dict[tuple, set]],
    weights_by_target: Dict[str, Dict[str, float]],
    mutation_defs: Dict,
) -> List[Tuple[int, cp_model.IntVar]]:
    """Objective terms `(coefficient, literal)` for the weighted effects each
    target mutation would hold at each of its candidate positions."""
    b = effect_model._builder
    terms: List[Tuple[int, cp_model.IntVar]] = []
    if not effect_model.effects:
        return terms
    for mut_name, weights in (weights_by_target or {}).items():
        positions = mutation_vars.get(mut_name)
        if not positions or not weights:
            continue
        mut_def = mutation_defs.get(mut_name)
        if mut_def is None:
            continue
        unit = slot_unit_int(mut_def)
        occ = mutation_occupied_cells.get(mut_name, {})
        for pos, mvar in positions.items():
            plant_cells = sorted(occ.get(pos, {pos}))
            for e, w in weights.items():
                if e not in effect_model.effects:
                    continue
                coef = int(round(w * unit))
                if coef == 0:
                    continue
                eff_lit = effect_model.slot_effective(plant_cells, e)
                lit = b.and_([mvar, eff_lit], f"score_{mut_name}_{pos}_{e}")
                if lit is False:
                    continue
                lit = b.as_var(lit, f"scorev_{mut_name}_{pos}_{e}")
                if lit is True:
                    lit = mvar
                terms.append((coef, lit))
    effect_model.stats["score_terms"] = len(terms)
    effect_model.stats["num_bools"] = b.num_bools
    effect_model.stats["num_constraints"] = b.num_constraints
    return terms


def add_special_eligibility_constraints(
    model: cp_model.CpModel,
    effect_model: EffectModel,
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_occupied_cells: Dict[str, Dict[tuple, set]],
) -> int:
    """Godseed-style eligibility: a slot variable may only be 1 if the slot's
    cells (which, like every slot, push nothing themselves) end up holding
    every effect in the mutation's required set."""
    b = effect_model._builder
    forced_off = 0
    for mut_name, required in SPECIAL_EFFECT_SETS.items():
        positions = mutation_vars.get(mut_name)
        if not positions:
            continue
        occ = mutation_occupied_cells.get(mut_name, {})
        for pos, var in positions.items():
            plant_cells = sorted(occ.get(pos, {pos}))
            has = effect_model.slot_has(plant_cells) if effect_model.effects else {}
            impossible = False
            for e in sorted(required):
                lit = has.get(e, False)
                if lit is True:
                    continue
                if lit is False:
                    impossible = True
                    break
                model.AddImplication(var, lit)
            if impossible:
                model.Add(var == 0)
                forced_off += 1
    return forced_off


@dataclass
class ObjectiveScales:
    """How the single CP-SAT objective is assembled, for decoding it later."""
    score_scale: int
    priority_scale: int
    gcd: int
    is_plain_count: bool
    objective_is_cells_only: bool
    has_score_terms: bool

    def decode(self, objective_value: float) -> Tuple[int, int, int]:
        """Split an objective value into (score_int, priority_penalty, cells)."""
        v = int(round(objective_value))
        score_int = -((-v) // self.score_scale)  # ceil division
        rest = score_int * self.score_scale - v
        priority = rest // self.priority_scale
        cells = rest - priority * self.priority_scale
        return score_int, priority, cells

    def score_from_int(self, score_int: int) -> float:
        """score_int * gcd is on RATE_SCALE (1.0 == one full-rate slot)."""
        from .spawn import RATE_SCALE
        return score_int * self.gcd / RATE_SCALE


def common_gcd(coefficients: Iterable[int]) -> int:
    vals = [abs(c) for c in coefficients if c]
    if not vals:
        return 1
    return reduce(gcd, vals)
