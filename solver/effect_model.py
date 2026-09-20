# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from dataclasses import dataclass, field
from functools import reduce
from math import gcd
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ortools.sat.python import cp_model

from .effects import IMPROVED_OF, NEGATIVE_EFFECTS, RELAY_EFFECT, SPECIAL_EFFECT_SETS, PlantBuffs, slot_unit_int
from .helpers import get_crop_cells

Literal = Union[bool, cp_model.IntVar, object]

CARDINAL_ORDER = {
    "N": (-1, 0),
    "W": (0, -1),
    "E": (0, 1),
    "S": (1, 0),
}


class _Builder:
    """Small helper that creates exact OR/AND literals with constant folding."""

    def __init__(self, model: cp_model.CpModel):
        self.model = model
        self.num_bools = 0
        self.num_constraints = 0

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

    def or_(self, lits: Iterable, name: str):
        kept = []
        seen = set()
        for l in lits:
            if l is True:
                return True
            if l is False:
                continue
            key = id(l)
            if key in seen:
                continue
            seen.add(key)
            kept.append(l)
        if not kept:
            return False
        if len(kept) == 1:
            return kept[0]
        y = self.new_bool(name)
        self.model.AddBoolOr(kept).OnlyEnforceIf(y)
        for l in kept:
            self.model.AddImplication(l, y)
        self.num_constraints += 1 + len(kept)
        return y

    def and_(self, lits: Iterable, name: str):
        kept = []
        seen = set()
        for l in lits:
            if l is False:
                return False
            if l is True:
                continue
            key = id(l)
            if key in seen:
                continue
            seen.add(key)
            kept.append(l)
        if not kept:
            return True
        if len(kept) == 1:
            return kept[0]
        y = self.new_bool(name)
        self.model.AddBoolAnd(kept).OnlyEnforceIf(y)
        self.model.AddBoolOr([self.neg(l) for l in kept] + [y])
        self.num_constraints += 2
        return y

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
        y = self.new_bool(name)
        self.model.Add(y == sum(vars_list))
        self.num_constraints += 1
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
        """Effective (scored) literal for one effect of a plant at `plant_cells`:"""
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
    passes: int = 2,
) -> EffectModel:
    """Unroll the propagation loop for the effects in `needed` and return the"""
    b = _Builder(model)
    effects = tuple(sorted(needed))
    if not effects:
        return EffectModel(effects=(), has_final={}, spread_possible=False, stats={}, _builder=b)

    sources: Dict[tuple, Dict[str, List]] = {c: {e: [] for e in effects} for c in cell_set}
    spread_sources: Dict[tuple, List] = {c: [] for c in cell_set}
    locked_intrinsic: Dict[tuple, set] = {}
    locked_spread: set = set()

    def register(name, pos, var, occupied):
        buffs = buff_table.get(name)
        if buffs is None:
            return
        for cell in occupied:
            if cell not in sources:
                continue
            for e in buffs.intrinsic:
                if e in sources[cell]:
                    sources[cell][e].append(var)
            if buffs.spreads:
                spread_sources[cell].append(var)

    for name, positions in crop_vars.items():
        occ = crop_occupied_cells.get(name, {})
        for pos, var in positions.items():
            register(name, pos, var, occ.get(pos, ()))
    for lock in locked_placements or []:
        name = lock.get("name") or lock.get("crop")
        buffs = buff_table.get(name)
        if buffs is None:
            continue
        for cell in get_crop_cells(tuple(lock["position"]), int(lock.get("size", 1))):
            if cell in cell_set:
                locked_intrinsic.setdefault(cell, set()).update(buffs.intrinsic)
                if buffs.spreads:
                    locked_spread.add(cell)

    def intrinsic_lit(cell, e):
        if e in locked_intrinsic.get(cell, ()):
            return True
        return b.exactly_one_of(sources[cell][e], f"intr_{cell}_{e}")

    def spread_lit(cell):
        if cell in locked_spread:
            return True
        return b.exactly_one_of(spread_sources[cell], f"spread_{cell}")

    intrinsic: Dict[tuple, Dict[str, Literal]] = {
        c: {e: intrinsic_lit(c, e) for e in effects} for c in cell_set
    }
    spread: Dict[tuple, Literal] = {c: spread_lit(c) for c in cell_set}
    spread_possible = any(s is not False for s in spread.values())

    def nb(cell, d):
        dr, dc = CARDINAL_ORDER[d]
        q = (cell[0] + dr, cell[1] + dc)
        return q if q in cell_set else None

    order = sorted(cell_set)
    has_final: Dict[tuple, Dict[str, Literal]] = {}

    if not spread_possible:
        for c in order:
            has_final[c] = {}
            for e in effects:
                lits = []
                for d in ("N", "W", "E", "S"):
                    q = nb(c, d)
                    if q is not None:
                        lits.append(intrinsic[q][e])
                has_final[c][e] = b.or_(lits, f"has_{c}_{e}")
        stats = {"num_bools": b.num_bools, "num_constraints": b.num_constraints, "effects": len(effects)}
        return EffectModel(effects=effects, has_final=has_final, spread_possible=False, stats=stats, _builder=b)

    state: Dict[tuple, Dict[str, Literal]] = {c: {e: False for e in effects} for c in order}
    for p in range(1, passes + 1):
        has_visit: Dict[tuple, Dict[str, Literal]] = {}
        push: Dict[tuple, Dict[str, Literal]] = {}
        for c in order:
            has_visit[c] = {}
            push[c] = {}
            n, w = nb(c, "N"), nb(c, "W")
            for e in effects:
                lits = [state[c][e]]
                if n is not None:
                    lits.append(push[n][e])
                if w is not None:
                    lits.append(push[w][e])
                hv = b.or_(lits, f"hv{p}_{c}_{e}")
                has_visit[c][e] = hv
                if e == RELAY_EFFECT:
                    push[c][e] = intrinsic[c][e]
                else:
                    relay = b.and_([spread[c], hv], f"relay{p}_{c}_{e}")
                    push[c][e] = b.or_([intrinsic[c][e], relay], f"push{p}_{c}_{e}")
        new_state: Dict[tuple, Dict[str, Literal]] = {}
        for c in order:
            new_state[c] = {}
            e_, s_ = nb(c, "E"), nb(c, "S")
            for e in effects:
                lits = [has_visit[c][e]]
                if e_ is not None:
                    lits.append(push[e_][e])
                if s_ is not None:
                    lits.append(push[s_][e])
                new_state[c][e] = b.or_(lits, f"end{p}_{c}_{e}")
        state = new_state

    has_final = state
    stats = {"num_bools": b.num_bools, "num_constraints": b.num_constraints, "effects": len(effects)}
    return EffectModel(effects=effects, has_final=has_final, spread_possible=True, stats=stats, _builder=b)


def add_effect_score_terms(
    effect_model: EffectModel,
    mutation_vars: Dict[str, Dict[tuple, cp_model.IntVar]],
    mutation_occupied_cells: Dict[str, Dict[tuple, set]],
    weights_by_target: Dict[str, Dict[str, float]],
    mutation_defs: Dict,
) -> List[Tuple[int, cp_model.IntVar]]:
    """Objective terms `(coefficient, literal)` for the weighted effects each"""
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
    """Godseed-style eligibility: a slot variable may only be 1 if the slot's"""
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
