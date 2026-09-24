# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from dataclasses import dataclass, field
from functools import reduce
from math import gcd
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ortools.sat.python import cp_model

from .effects import (
    IMPROVED_OF,
    NEGATIVE_EFFECTS,
    RELAY_EFFECT,
    SPECIAL_EFFECT_SETS,
    PlantBuffs,
    relay_regimes,
    relay_turn_key,
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
    """Encode direct effects plus the one-turn-per-spreader relay for the effects
    in `needed`, and return the final held-set literals per cell."""
    b = _Builder(model)
    effects = tuple(sorted(needed))
    if not effects:
        return EffectModel(effects=(), has_final={}, spread_possible=False, stats={}, _builder=b)
    relayed = tuple(e for e in effects if e != RELAY_EFFECT)
    tracked = tuple(sorted(set(effects) | {RELAY_EFFECT}))
    order = sorted(cell_set)

    placements: List[Tuple[Literal, frozenset, tuple, frozenset]] = []
    for name, positions in crop_vars.items():
        occ_map = crop_occupied_cells.get(name, {})
        buffs = buff_table.get(name)
        listed = frozenset(buffs.intrinsic) if buffs is not None else frozenset()
        for pos, var in positions.items():
            occ = frozenset(c for c in occ_map.get(pos, ()) if c in cell_set)
            if occ:
                placements.append((var, occ, tuple(pos), listed))
    for lock in locked_placements or []:
        name = lock.get("name") or lock.get("crop")
        buffs = buff_table.get(name)
        listed = frozenset(buffs.intrinsic) if buffs is not None else frozenset()
        pos = tuple(lock["position"])
        occ = frozenset(c for c in get_crop_cells(pos, int(lock.get("size", 1))) if c in cell_set)
        if occ:
            placements.append((True, occ, pos, listed))

    covering: Dict[tuple, List[int]] = {c: [] for c in cell_set}
    for i, (_, occ, _, _) in enumerate(placements):
        for c in occ:
            covering[c].append(i)
    single_at: Dict[tuple, List[int]] = {
        c: [i for i in covering[c] if len(placements[i][1]) == 1] for c in cell_set
    }
    multis = [i for i, p in enumerate(placements) if len(p[1]) > 1]

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

    direct: Dict[tuple, Dict[str, Literal]] = {
        c: {e: b.or_([gives_to(q, c, e) for q in nbrs(c)], f"direct_{c}_{e}") for e in tracked}
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

    def finish(has_final, regimes_used):
        stats = {
            "num_bools": b.num_bools, "num_constraints": b.num_constraints, "effects": len(effects),
            "relay_tables": regimes_used, "possible_spreaders": len(live),
            "multi_cell_units": len(multis),
        }
        return EffectModel(effects=effects, has_final=has_final, spread_possible=spread_possible,
                           stats=stats, _builder=b)

    if not spread_possible:
        base = entity_or_tile({c: {e: direct[c][e] for e in effects} for c in order}) if multis else \
            {c: {e: direct[c][e] for e in effects} for c in order}
        return finish(base, [])

    # ---- which table sizes can the spreader count reach?
    forced = sum(1 for l in unit_lits if l is True)
    regimes = [(t, lo, hi) for t, lo, hi in relay_regimes(order)
               if lo <= len(live) and (hi is None or hi >= forced)]

    def relay_circuit(table):
        tile_key = {c: relay_turn_key(c, table) for c in order}
        multi_key = {i: relay_turn_key(placements[i][2], table) for i in multis}
        units = [("tile", c, tile_key[c]) for c in order] + [("multi", i, multi_key[i]) for i in multis]
        units.sort(key=lambda u: u[2])
        tile_push: Dict[tuple, Dict[str, Literal]] = {}
        multi_push: Dict[int, Dict[str, Literal]] = {}

        def pushes_into(target_cells, own, key_limit):
            """Push literals landing on `target_cells` from neighbouring units
            (not `own`, not overlapping it) whose turn key is below key_limit
            (None: every neighbour)."""
            own_occ = placements[own][1] if own is not None else frozenset()
            lits_by_e = {e: [] for e in relayed}
            seen_tiles, seen_multis = set(), set()
            for x in target_cells:
                for q in nbrs(x):
                    if q in target_cells:
                        continue
                    if q not in seen_tiles and (key_limit is None or tile_key[q] < key_limit):
                        seen_tiles.add(q)
                        for e in relayed:
                            lits_by_e[e].append(tile_push[q][e])
                    for w in covering[q]:
                        if w not in multi_held or w == own or w in seen_multis:
                            continue
                        if own_occ & placements[w][1] or any(t in placements[w][1] for t in target_cells):
                            continue
                        if key_limit is not None and not multi_key[w] < key_limit:
                            continue
                        seen_multis.add(w)
                        for e in relayed:
                            lits_by_e[e].append(multi_push[w][e])
            return lits_by_e

        for kind, u, key in units:
            if kind == "tile":
                c = u
                tile_push[c] = {}
                earlier = pushes_into((c,), None, key) if tile_spreader[c] is not False else None
                for e in relayed:
                    if earlier is None:
                        tile_push[c][e] = False
                        continue
                    before = b.or_([direct[c][e]] + earlier[e], f"before{table}_{c}_{e}")
                    tile_push[c][e] = b.and_([tile_spreader[c], before], f"push{table}_{c}_{e}")
            else:
                i = u
                multi_push[i] = {}
                occ = tuple(sorted(placements[i][1]))
                earlier = pushes_into(occ, i, key) if multi_spreader[i] is not False else None
                for e in relayed:
                    if earlier is None:
                        multi_push[i][e] = False
                        continue
                    before = b.or_([multi_held[i][e]] + earlier[e], f"mbefore{table}_{i}_{e}")
                    multi_push[i][e] = b.and_([multi_spreader[i], before], f"mpush{table}_{i}_{e}")

        final = {}
        for c in order:
            into = pushes_into((c,), None, None)
            final[c] = {e: b.or_([direct[c][e]] + into[e], f"final{table}_{c}_{e}") for e in relayed}
        return final

    circuits = [(t, relay_circuit(t)) for t, _, _ in regimes]
    if len(regimes) == 1:
        chosen = circuits[0][1]
    else:
        count = sum(1 if l is True else l for l in live)
        indicators = []
        for t, lo, hi in regimes:
            ind = b.new_bool(f"relay_table_{t}")
            model.Add(count >= lo).OnlyEnforceIf(ind)
            if hi is not None:
                model.Add(count <= hi).OnlyEnforceIf(ind)
            indicators.append(ind)
        model.AddExactlyOne(indicators)
        b.num_constraints += 2 * len(regimes) + 1
        chosen = {
            c: {e: b.or_([b.and_([ind, circ[c][e]], f"sel{t}_{c}_{e}")
                          for ind, (t, circ) in zip(indicators, circuits)], f"has_{c}_{e}")
                for e in relayed}
            for c in order
        }

    per_cell = {c: {e: (direct[c][e] if e == RELAY_EFFECT else chosen[c][e]) for e in effects} for c in order}
    has_final = entity_or_tile(per_cell) if multis else per_cell
    return finish(has_final, [t for t, _, _ in regimes])


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
