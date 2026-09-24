# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

from typing import List, Dict, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from models import CropDefinition, MutationDefinition


# 8-way adjacency offsets (used for all mutations)
ADJACENCY_OFFSETS = [
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1),          (0, 1),
    (1, -1),  (1, 0), (1, 1)
]


def get_crop_cells(top_left: tuple, size: int) -> List[tuple]:
    """Get all cells occupied by a crop given its top-left corner and size."""
    r, c = top_left
    return [(r + dr, c + dc) for dr in range(size) for dc in range(size)]


def can_place_crop(top_left: tuple, size: int, cell_set: set) -> bool:
    """Check if a crop can be placed at the given position."""
    for cell in get_crop_cells(top_left, size):
        if cell not in cell_set:
            return False
    return True


def get_valid_placements(cells: List[tuple], size: int) -> List[tuple]:
    """Get all valid top-left positions where a crop of given size can be placed."""
    cell_set = set(cells)
    valid = []
    for cell in cells:
        if can_place_crop(cell, size, cell_set):
            valid.append(cell)
    return valid


def get_adjacent_cells_for_mutation(mut_pos: tuple, mut_size: int, cell_set: set) -> set:
    """Get all cells adjacent (8-way) to a mutation's occupied cells."""
    mut_cells = set(get_crop_cells(mut_pos, mut_size))
    adjacent_cells = set()
    for mc in mut_cells:
        for dr, dc in ADJACENCY_OFFSETS:
            adj_cell = (mc[0] + dr, mc[1] + dc)
            if adj_cell not in mut_cells and adj_cell in cell_set:
                adjacent_cells.add(adj_cell)
    return adjacent_cells


def can_mutation_be_satisfied_at_position(
    mut,
    mut_pos: tuple,
    cell_set: set,
    crop_occupied_by_pos: Dict[str, Dict[tuple, set]]
) -> bool:
    """Check if a mutation can be satisfied at a given position."""
    adjacent_cells = get_adjacent_cells_for_mutation(mut_pos, mut.size, cell_set)

    if mut.requires_zero_adjacent:
        return True

    for req_crop in mut.requirements:
        crop_name = req_crop.crop

        if crop_name not in crop_occupied_by_pos:
            return False  # Required crop doesn't exist

        max_adjacent = 0
        for crop_pos, crop_cells in crop_occupied_by_pos[crop_name].items():
            overlap = len(crop_cells & adjacent_cells)
            max_adjacent = max(max_adjacent, overlap)

        if max_adjacent == 0:
            return False

    return True


def compute_crop_and_mutation_positions(
    req,
    cells: List[tuple],
    cell_set: set,
    crop_defs: Dict,
    locked_cells: set = None,
    locked_crop_positions: Dict[str, List[tuple]] = None,
    extra_crop_names: set = None,
) -> tuple:
    """Pre-compute valid positions and occupied cells for crops and mutations."""
    if locked_cells is None:
        locked_cells = set()
    if locked_crop_positions is None:
        locked_crop_positions = {}
    if extra_crop_names is None:
        extra_crop_names = set()

    required_crop_names = set()
    for mut in req.mutations:
        for r in mut.requirements:
            required_crop_names.add(r.crop)

    filtered_crops = [
        c for c in req.crops
        if c.name in required_crop_names or c.name in extra_crop_names
    ]

    crop_valid_positions: Dict[str, List[tuple]] = {}
    for crop in filtered_crops:
        all_valid = get_valid_placements(cells, crop.size)
        if locked_cells:
            non_overlapping = []
            for pos in all_valid:
                occupied = set(get_crop_cells(pos, crop.size))
                if not (occupied & locked_cells):
                    non_overlapping.append(pos)
            crop_valid_positions[crop.name] = non_overlapping
        else:
            crop_valid_positions[crop.name] = all_valid

    crop_occupied_by_pos: Dict[str, Dict[tuple, set]] = {}
    for crop in filtered_crops:
        crop_occupied_by_pos[crop.name] = {
            pos: set(get_crop_cells(pos, crop.size))
            for pos in crop_valid_positions[crop.name]
        }
        if crop.name in locked_crop_positions:
            for locked_pos in locked_crop_positions[crop.name]:
                crop_occupied_by_pos[crop.name][locked_pos] = set(
                    get_crop_cells(locked_pos, crop.size)
                )

    crop_coverable_cells: Dict[str, set] = {}
    for crop_name, occupied_by_pos in crop_occupied_by_pos.items():
        covered = set()
        for occ in occupied_by_pos.values():
            covered |= occ
        crop_coverable_cells[crop_name] = covered

    mutation_feasible_positions: Dict[str, List[tuple]] = {}
    all_mutation_adjacent_cells: Dict[str, set] = {}

    for mut in req.mutations:
        all_valid = get_valid_placements(cells, mut.size)
        if locked_cells:
            valid_positions = []
            for pos in all_valid:
                occupied = set(get_crop_cells(pos, mut.size))
                if not (occupied & locked_cells):
                    valid_positions.append(pos)
        else:
            valid_positions = all_valid

        req_by_crop: Dict[str, int] = {}
        all_required_coverable: set = set()
        for req_crop in mut.requirements:
            req_by_crop[req_crop.crop] = max(req_by_crop.get(req_crop.crop, 0), req_crop.count)
            all_required_coverable |= crop_coverable_cells.get(req_crop.crop, set())
        total_required = sum(req_by_crop.values())

        feasible_positions = []
        adjacent_cells = set()
        for pos in valid_positions:
            pos_adjacent = get_adjacent_cells_for_mutation(pos, mut.size, cell_set)
            if not mut.requires_zero_adjacent:
                satisfiable = True
                for req_crop in mut.requirements:
                    coverable = crop_coverable_cells.get(req_crop.crop)
                    if not coverable or len(pos_adjacent & coverable) < req_crop.count:
                        satisfiable = False
                        break
                if not satisfiable:
                    continue
                if len(pos_adjacent & all_required_coverable) < total_required:
                    continue
            feasible_positions.append(pos)
            adjacent_cells |= pos_adjacent

        mutation_feasible_positions[mut.name] = feasible_positions
        all_mutation_adjacent_cells[mut.name] = adjacent_cells

    return (filtered_crops, crop_valid_positions, crop_occupied_by_pos,
            mutation_feasible_positions, all_mutation_adjacent_cells)
