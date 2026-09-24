# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

import json
import os

from solver.effects import EFFECT_META, default_buff_crops

data_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
with open(data_file, "r") as f:
    _RAW_DATA = json.load(f)

_weights_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_effect_weights.json")
try:
    with open(_weights_file, "r") as f:
        DEFAULT_EFFECT_WEIGHTS = {k: float(v) for k, v in json.load(f).items() if float(v) != 0.0}
except (OSError, ValueError):
    DEFAULT_EFFECT_WEIGHTS = {}


# Full data preserving the object structure for data lookups
FULL_DATA = _RAW_DATA

_crops_list = []
for crop_id, crop_data in _RAW_DATA["crops"].items():
    _crops_list.append({
        "name": crop_id,
        "size": crop_data.get("size", 1),
        "ground": crop_data.get("ground", "farmland"),
        "growth_stages": crop_data.get("growth_stages"),
        "positive_buffs": crop_data.get("positive_buffs", []),
        "negative_buffs": crop_data.get("negative_buffs", []),
        "sell_price": crop_data.get("sell_price"),
    })

_mutations_list = []
for mut_id, mut_data in _RAW_DATA["mutations"].items():
    entry = {
        "name": mut_id,
        "size": mut_data.get("size", 1),
        "ground": mut_data.get("ground", "farmland"),
        "requirements": mut_data.get("requirements", []),
        "rarity": mut_data.get("rarity", "common"),
        "growth_stages": mut_data.get("growth_stages", 0),
        "decay": mut_data.get("decay", 0),
        "positive_buffs": mut_data.get("positive_buffs", []),
        "negative_buffs": mut_data.get("negative_buffs", []),
        "drops": mut_data.get("drops", {}),
        "mutation_chance": mut_data.get("mutation_chance", 0.10),
        "spawn_weight": mut_data.get("spawn_weight", 0),
    }
    special = mut_data.get("special")
    if special:
        entry["special"] = special
        if special == "requires_zero_adjacent":
            entry["requires_zero_adjacent"] = True
        else:
            entry["requires_zero_adjacent"] = False
    else:
        entry["requires_zero_adjacent"] = False

    if "harvest_info" in mut_data:
        entry["harvest_info"] = mut_data["harvest_info"]
    if "growing_info" in mut_data:
        entry["growing_info"] = mut_data["growing_info"]

    _mutations_list.append(entry)


DEFAULT_DATA = {
    "crops": _crops_list,
    "mutations": _mutations_list,
    "effects": _RAW_DATA.get("effects", {}),
    "effect_meta": EFFECT_META,
    "buff_crops": default_buff_crops(_crops_list),
    "default_effect_weights": DEFAULT_EFFECT_WEIGHTS,
}
