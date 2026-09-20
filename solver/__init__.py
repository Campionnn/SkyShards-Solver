# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

# Solver module for SkyShards optimization

from .helpers import (
    ADJACENCY_OFFSETS,
    get_crop_cells,
    can_place_crop,
    get_valid_placements,
    get_adjacent_cells_for_mutation,
    can_mutation_be_satisfied_at_position,
    compute_crop_and_mutation_positions,
)

from .spawn import (
    BLANK_FILL_TO,
    RATE_SCALE,
    marginal_rates,
    max_multiplicity,
    multiplicity,
    score_layout,
    spawn_probability,
)

from .effects import (
    EFFECT_NAMES,
    EFFECT_META,
    NEGATIVE_EFFECTS,
    IMPROVED_OF,
    RELAY_EFFECT,
    SPECIAL_EFFECT_SETS,
    special_required_effects,
    PlantBuffs,
    ScoreBreakdown,
    build_buff_table,
    default_buff_crops,
    effective_effects,
    effects_active,
    needed_effects,
    prune_free_buff_set,
    prune_valueless_placements,
    resolve_effect_weights,
    score_layout_with_effects,
    simulate_effects,
    slot_unit,
    slot_unit_int,
)

from .effect_model import (
    EffectModel,
    ObjectiveScales,
    build_effect_model,
    add_effect_score_terms,
    add_special_eligibility_constraints,
)

from .constraints import (
    create_decision_variables,
    create_multiplicity_chains,
    add_cell_usage_constraints,
    add_mutation_eligibility_constraints,
    add_symmetry_breaking_constraints,
    create_cell_used_vars,
    build_maximize_expr,
    build_objective,
    add_decision_strategy,
    apply_locked_placements,
    extract_results,
)

__all__ = [
    # helpers
    "ADJACENCY_OFFSETS",
    "get_crop_cells",
    "can_place_crop",
    "get_valid_placements",
    "get_adjacent_cells_for_mutation",
    "can_mutation_be_satisfied_at_position",
    "compute_crop_and_mutation_positions",
    # spawn math
    "BLANK_FILL_TO",
    "RATE_SCALE",
    "marginal_rates",
    "max_multiplicity",
    "multiplicity",
    "score_layout",
    "spawn_probability",
    # effects
    "EFFECT_NAMES",
    "EFFECT_META",
    "NEGATIVE_EFFECTS",
    "IMPROVED_OF",
    "RELAY_EFFECT",
    "SPECIAL_EFFECT_SETS",
    "special_required_effects",
    "PlantBuffs",
    "ScoreBreakdown",
    "build_buff_table",
    "default_buff_crops",
    "effective_effects",
    "effects_active",
    "needed_effects",
    "prune_free_buff_set",
    "prune_valueless_placements",
    "resolve_effect_weights",
    "score_layout_with_effects",
    "simulate_effects",
    "slot_unit",
    "slot_unit_int",
    # effect model
    "EffectModel",
    "ObjectiveScales",
    "build_effect_model",
    "add_effect_score_terms",
    "add_special_eligibility_constraints",
    # constraints
    "create_decision_variables",
    "create_multiplicity_chains",
    "add_cell_usage_constraints",
    "add_mutation_eligibility_constraints",
    "add_symmetry_breaking_constraints",
    "create_cell_used_vars",
    "build_maximize_expr",
    "build_objective",
    "add_decision_strategy",
    "apply_locked_placements",
    "extract_results",
]
