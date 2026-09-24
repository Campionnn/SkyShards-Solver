# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .helpers import get_crop_cells, get_adjacent_cells_for_mutation
from .spawn import (
    RATE_SCALE,
    adjacent_crop_counts,
    build_cell_owner,
    is_all_positive_special,
    marginal_rates,
    multiplicity,
    name_of,
    score_layout,
    weight_of,
    _get,
)


def _load_raw_data() -> Dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
    with open(path, "r") as f:
        return json.load(f)


_RAW = _load_raw_data()

RELAY_EFFECT = "effect_spread"

EFFECT_NAMES: Tuple[str, ...] = tuple(_RAW.get("effects", {}).keys())

PROPAGATABLE_EFFECTS: Tuple[str, ...] = tuple(EFFECT_NAMES)

SPECIAL_EFFECT_SETS: Dict[str, FrozenSet[str]] = {
    name: frozenset(entry.get("positive_buffs") or [])
    for name, entry in _RAW.get("mutations", {}).items()
    if entry.get("special") == "all_positive_crop_effects"
}

# Negatives are whatever any plant lists under negative_buffs.
NEGATIVE_EFFECTS: FrozenSet[str] = frozenset(
    e
    for table in (_RAW.get("crops", {}), _RAW.get("mutations", {}))
    for entry in table.values()
    for e in (entry.get("negative_buffs") or [])
)

IMPROVED_OF: Dict[str, str] = {
    e: "improved_" + e for e in EFFECT_NAMES if "improved_" + e in EFFECT_NAMES
}

ZERO_WEIGHT_SLOT_UNIT = RATE_SCALE // 4

EFFECT_META = {
    "relay": RELAY_EFFECT,
    "negative": sorted(NEGATIVE_EFFECTS),
    "improved_of": dict(IMPROVED_OF),
    "special_effect_sets": {k: sorted(v) for k, v in SPECIAL_EFFECT_SETS.items()},
}


@dataclass(frozen=True)
class PlantBuffs:
    """What one plant kind contributes to propagation (what it pushes; it"""
    name: str
    size: int
    intrinsic: FrozenSet[str]
    spreads: bool


def plant_buffs_from_def(d) -> PlantBuffs:
    """Build a PlantBuffs from a CropDefinition, MutationDefinition or raw dict."""
    positive = list(_get(d, "positive_buffs", []) or [])
    negative = list(_get(d, "negative_buffs", []) or [])
    listed = set(positive) | set(negative)
    return PlantBuffs(
        name=name_of(d),
        size=int(_get(d, "size", 1) or 1),
        intrinsic=frozenset(listed),
        spreads=RELAY_EFFECT in listed,
    )


def build_buff_table(crop_defs: Iterable = (), mutation_defs: Iterable = ()) -> Dict[str, PlantBuffs]:
    """name -> PlantBuffs for every crop and mutation definition (first"""
    table: Dict[str, PlantBuffs] = {}
    for group in (crop_defs, mutation_defs):
        for d in group or []:
            n = name_of(d)
            if n not in table:
                table[n] = plant_buffs_from_def(d)
    return table


def default_buff_crops(all_crops: Iterable) -> List[str]:
    """Base crops that carry any buff at all - the default free placement set."""
    out = []
    for c in all_crops:
        if (_get(c, "positive_buffs", None) or _get(c, "negative_buffs", None)):
            out.append(name_of(c))
    return out


CARDINAL = ((-1, 0), (1, 0), (0, -1), (0, 1))

RELAY_HASH_COLUMN_FACTOR = 13
RELAY_TABLE_INITIAL = 16
RELAY_TABLE_LOAD_FACTOR = 0.75


def relay_table_size(spreader_count: int) -> int:
    """HashMap capacity holding `spreader_count` entries: 16, doubled while"""
    table = RELAY_TABLE_INITIAL
    while spreader_count > RELAY_TABLE_LOAD_FACTOR * table:
        table *= 2
    return table


def relay_turn_key(position, table: int) -> Tuple[int, int, int]:
    """Sort key for a spreader's turn: its hash slot, then row, then column."""
    row, col = position
    return ((RELAY_HASH_COLUMN_FACTOR * col + row) % table, row, col)


def relay_regimes(cells: Iterable) -> List[Tuple[int, int, Optional[int]]]:
    """Every (table, min_count, max_count) whose turn order can differ on"""
    max_key = max((RELAY_HASH_COLUMN_FACTOR * c + r for r, c in cells), default=0)
    out: List[Tuple[int, int, Optional[int]]] = []
    table, lo = RELAY_TABLE_INITIAL, 0
    while True:
        hi = int(RELAY_TABLE_LOAD_FACTOR * table)
        if table > max_key:
            out.append((table, lo, None))
            return out
        out.append((table, lo, hi))
        table, lo = table * 2, hi + 1


@dataclass
class SimPlant:
    name: str
    position: tuple
    size: int
    cells: Tuple[tuple, ...]
    buffs: PlantBuffs
    has: Set[str] = field(default_factory=set)
    is_mutation_slot: bool = False


@dataclass
class EffectSimulation:
    cell_to_plant: Dict[tuple, SimPlant]
    plants: List[SimPlant]
    relay_order: List[SimPlant] = field(default_factory=list)   # spreaders, in turn order
    relay_table: int = RELAY_TABLE_INITIAL


    def raw_at(self, cell) -> FrozenSet[str]:
        p = self.cell_to_plant.get(tuple(cell))
        return frozenset(p.has) if p is not None else frozenset()

    def effective_at(self, cell) -> FrozenSet[str]:
        return effective_effects(self.raw_at(cell))

    def special_eligible(self, name: str, position, size: int = 1) -> bool:
        """Godseed rule: the slot holds every effect in its required set."""
        required = SPECIAL_EFFECT_SETS.get(name)
        if required is None:
            return False
        return required <= self.raw_at(tuple(position))


def effective_effects(raw: Iterable[str]) -> FrozenSet[str]:
    """The set that matters for scoring: immunity strips negatives, improved"""
    eff = set(raw)
    if "immunity" in eff:
        eff -= NEGATIVE_EFFECTS
    for base, improved in IMPROVED_OF.items():
        if improved in eff:
            eff.discard(base)
    return frozenset(eff)


def _placement_fields(p) -> Tuple[str, tuple, int]:
    if isinstance(p, dict):
        name = p.get("crop") or p.get("mutation") or p.get("name")
        return name, tuple(p["position"]), int(p.get("size", 1))
    name = getattr(p, "crop", None) or getattr(p, "mutation", None) or getattr(p, "name")
    return name, tuple(p.position), int(getattr(p, "size", 1))


def simulate_effects(
    cells: Sequence,
    placements: Sequence,
    mutation_slots: Sequence,
    buff_table: Dict[str, PlantBuffs],
) -> EffectSimulation:
    """Run the game's propagation over a concrete layout (module docstring)."""
    cell_set = {tuple(c) for c in cells}
    cell_to_plant: Dict[tuple, SimPlant] = {}
    plants: List[SimPlant] = []

    def add(name, pos, size, is_slot):
        buffs = buff_table.get(name)
        if buffs is None:
            buffs = PlantBuffs(name=name, size=size, intrinsic=frozenset(), spreads=False)
        plant_cells = tuple(c for c in get_crop_cells(pos, size) if c in cell_set)
        plant = SimPlant(
            name=name, position=pos, size=size, cells=plant_cells, buffs=buffs,
            has=set(), is_mutation_slot=is_slot,
        )
        plants.append(plant)
        for c in plant_cells:
            cell_to_plant[c] = plant

    for p in placements or []:
        name, pos, size = _placement_fields(p)
        add(name, pos, size, False)
    for s in mutation_slots or []:
        name, pos, size = _placement_fields(s)
        add(name, pos, size, True)

    def neighbours(plant: SimPlant) -> List[SimPlant]:
        seen: List[SimPlant] = []
        for r, c in plant.cells:
            for dr, dc in CARDINAL:
                q = cell_to_plant.get((r + dr, c + dc))
                if q is not None and q is not plant and all(q is not x for x in seen):
                    seen.append(q)
        return seen

    for plant in plants:
        if plant.is_mutation_slot or not plant.buffs.intrinsic:
            continue
        for q in neighbours(plant):
            q.has |= plant.buffs.intrinsic

    # 2-3. One relay turn per spreader, in HashMap order.
    spreaders = [p for p in plants if not p.is_mutation_slot and RELAY_EFFECT in p.has]
    table = relay_table_size(len(spreaders))
    spreaders.sort(key=lambda p: relay_turn_key(p.position, table))
    for plant in spreaders:
        payload = plant.has - {RELAY_EFFECT}
        if not payload:
            continue
        for q in neighbours(plant):
            q.has |= payload

    return EffectSimulation(
        cell_to_plant=cell_to_plant, plants=plants, relay_order=spreaders, relay_table=table,
    )


def resolve_effect_weights(targets: Sequence, request_weights: Optional[Dict[str, float]]) -> Dict[str, Dict[str, float]]:
    """Per-target effect weights: the request-level dict, overridden key-by-key"""
    base = {k: float(v) for k, v in (request_weights or {}).items()}
    out: Dict[str, Dict[str, float]] = {}
    for t in targets or []:
        name = _get(t, "mutation")
        merged = dict(base)
        override = _get(t, "effect_weights", None)
        if override:
            merged.update({k: float(v) for k, v in override.items()})
        out[name] = {k: v for k, v in merged.items() if v != 0.0 and math.isfinite(v)}
    return out


def effects_active(weights_by_target: Dict[str, Dict[str, float]]) -> bool:
    return any(w for w in (weights_by_target or {}).values())


def special_required_effects(target_names: Iterable[str]) -> FrozenSet[str]:
    """Union of the effect sets godseed-style targets need to hold."""
    out: Set[str] = set()
    for n in target_names:
        out |= SPECIAL_EFFECT_SETS.get(n, frozenset())
    return frozenset(out)


def needed_effects(
    weights_by_target: Dict[str, Dict[str, float]],
    buff_table: Optional[Dict[str, PlantBuffs]] = None,
    required: Iterable[str] = (),
) -> FrozenSet[str]:
    """Closure of effects the model must track: every weighted effect and every"""
    carried: Optional[Set[str]] = None
    if buff_table is not None:
        carried = set()
        for b in buff_table.values():
            carried |= b.intrinsic

    weighted: Set[str] = set(required)
    for w in (weights_by_target or {}).values():
        weighted |= set(w)
    if carried is not None:
        weighted &= carried

    needed: Set[str] = set(weighted)
    for e in weighted:
        if e in NEGATIVE_EFFECTS:
            needed.add("immunity")
        if e in IMPROVED_OF:
            needed.add(IMPROVED_OF[e])
    if carried is not None:
        needed &= carried
    return frozenset(needed)


def prune_free_buff_set(
    names: Iterable[str],
    buff_table: Dict[str, PlantBuffs],
    weights_by_target: Dict[str, Dict[str, float]],
    other_plant_names: Iterable[str] = (),
    required_effects: Iterable[str] = (),
) -> List[str]:
    """Keep only free crops that can possibly raise the score: relays, carriers"""
    positive: Set[str] = set(required_effects)
    negative: Set[str] = set()
    for w in (weights_by_target or {}).values():
        for e, v in w.items():
            if v > 0:
                positive.add(e)
            elif v < 0:
                negative.add(e)

    kept = []
    for n in names:
        b = buff_table.get(n)
        if b is None:
            continue
        if b.spreads or (b.intrinsic & positive):
            kept.append(n)

    present: Set[str] = set()
    for n in list(other_plant_names) + kept:
        b = buff_table.get(n)
        if b is not None:
            present |= b.intrinsic
    if negative & present:
        for n in names:
            b = buff_table.get(n)
            if b is not None and "immunity" in b.intrinsic and n not in kept:
                kept.append(n)
    return kept


def slot_unit(mut) -> float:
    """Value of one plain slot of this mutation, in expected spawns per tick."""
    w = weight_of(mut)
    if w <= 0:
        return ZERO_WEIGHT_SLOT_UNIT / RATE_SCALE
    return min(1.0, w / 100.0)


def slot_unit_int(mut) -> int:
    """Integer twin of slot_unit on the RATE_SCALE used by the CP-SAT objective."""
    if weight_of(mut) <= 0:
        return ZERO_WEIGHT_SLOT_UNIT
    return marginal_rates(mut, 1)[0]


@dataclass
class ScoreBreakdown:
    score: float
    base_rate: float
    effect_value: float
    per_slot: List[Dict]
    total_rate: float = 0.0  # every slot, incl. fixed-count targets


def score_layout_with_effects(
    cells: Sequence,
    placements: Sequence,
    mutation_slots: Sequence,
    all_mutations: Sequence,
    targets: Sequence,
    weights_by_target: Dict[str, Dict[str, float]],
    buff_table: Dict[str, PlantBuffs],
    detailed: bool = True,
) -> ScoreBreakdown:
    """Exact layout score: competition-aware spawn rate over maximize targets"""
    by_name = {name_of(m): m for m in all_mutations}
    maximize = {_get(t, "mutation") for t in targets or [] if _get(t, "maximize", False)}
    target_names = {_get(t, "mutation") for t in targets or []}

    sim = simulate_effects(cells, placements, mutation_slots, buff_table)
    rates = score_layout(cells, placements, mutation_slots, all_mutations, special_eligible_fn=sim.special_eligible)
    base_rate = sum(v for k, v in rates.items() if k in maximize)
    total_rate = sum(rates.values())

    per_slot_rate = (
        _per_slot_rates(cells, placements, mutation_slots, all_mutations, sim) if detailed else None
    )

    effect_value = 0.0
    per_slot: List[Dict] = []
    for i, slot in enumerate(mutation_slots or []):
        name, pos, size = _placement_fields(slot)
        eff = sorted(sim.effective_at(pos))
        weights = (weights_by_target or {}).get(name, {})
        unit = slot_unit(by_name[name]) if name in by_name else ZERO_WEIGHT_SLOT_UNIT / RATE_SCALE
        value = 0.0
        if name in target_names:
            value = sum(weights.get(e, 0.0) for e in eff) * unit
            effect_value += value
        if not detailed:
            continue
        rate = per_slot_rate[i] if name in maximize else 0.0
        per_slot.append({
            "mutation": name,
            "position": list(pos),
            "size": size,
            "rate": round(rate, 6),
            "effects": eff,
            "value": round(rate + value, 6),
        })

    return ScoreBreakdown(
        score=base_rate + effect_value,
        base_rate=base_rate,
        effect_value=effect_value,
        per_slot=per_slot,
        total_rate=total_rate,
    )


def _per_slot_rates(cells, placements, mutation_slots, all_mutations, sim: EffectSimulation) -> List[float]:
    """score_layout, one slot at a time (same math, per-slot resolution)."""
    out = []
    for slot in mutation_slots or []:
        rates = score_layout(cells, placements, [slot], all_mutations, special_eligible_fn=sim.special_eligible)
        name, _, _ = _placement_fields(slot)
        out.append(rates.get(name, 0.0))
    return out


def slots_all_eligible(
    cells, placements, mutation_slots, mutation_defs: Dict,
    buff_table: Optional[Dict[str, PlantBuffs]] = None,
) -> bool:
    """Every mutation slot still has its requirements met by the placements"""
    cell_set = {tuple(c) for c in cells}
    owner = build_cell_owner(placements)
    sim = None
    for slot in mutation_slots or []:
        name, pos, size = _placement_fields(slot)
        mut = mutation_defs.get(name)
        if mut is None:
            return False
        special = None
        if is_all_positive_special(mut):
            if sim is None:
                sim = simulate_effects(cells, placements, mutation_slots, buff_table or {})
            special = sim.special_eligible(name, pos, size)
        counts = adjacent_crop_counts(pos, size, cell_set, owner)
        if multiplicity(mut, counts, special_eligible=special) < 1:
            return False
    return True


def prune_valueless_placements(
    cells: Sequence,
    placements: List[Dict],
    mutation_slots: Sequence,
    all_mutations: Sequence,
    targets: Sequence,
    weights_by_target: Dict[str, Dict[str, float]],
    buff_table: Dict[str, PlantBuffs],
    mutation_defs: Dict,
    free_names: Optional[Iterable[str]] = None,
) -> Tuple[List[Dict], ScoreBreakdown]:
    """Drop every non-locked placement whose removal does not lower the exact"""
    free = set(free_names or ())

    def sort_key(p):
        return (0 if p.get("crop") in free else 1, tuple(p["position"]))

    def score(pl):
        bd = score_layout_with_effects(
            cells, pl, mutation_slots, all_mutations, targets, weights_by_target, buff_table,
            detailed=False,
        )
        return bd.score, bd.total_rate

    current = list(placements)
    best_score, best_rate = score(current)
    changed = True
    while changed:
        changed = False
        candidates = sorted((p for p in current if not p.get("locked")), key=sort_key)
        for p in candidates:
            trial = [q for q in current if q is not p]
            if not slots_all_eligible(cells, trial, mutation_slots, mutation_defs, buff_table):
                continue
            trial_score, trial_rate = score(trial)
            if trial_score >= best_score - 1e-9 and trial_rate >= best_rate - 1e-9:
                current = trial
                best_score, best_rate = trial_score, trial_rate
                changed = True
    final = score_layout_with_effects(
        cells, current, mutation_slots, all_mutations, targets, weights_by_target, buff_table
    )
    return current, final
