# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

"""Solver request/response models."""

from pydantic import BaseModel, Field
from typing import Annotated, List, Dict, Optional

from .crops import CropDefinition, MutationDefinition

GRID_SIZE = 10
MAX_CELLS = GRID_SIZE * GRID_SIZE
MAX_NAME_LENGTH = 64
MAX_TARGETS = 50
MAX_LOCKS = MAX_CELLS
MAX_NAMED_ENTRIES = 200

Coord = Annotated[int, Field(ge=0, le=GRID_SIZE - 1)]
Cell = Annotated[List[Coord], Field(min_length=2, max_length=2)]
Name = Annotated[str, Field(min_length=1, max_length=MAX_NAME_LENGTH)]


class LockedPlacement(BaseModel):
    """A locked crop/mutation placement that must be included in the solution."""
    name: Name
    size: int = Field(..., ge=1, le=3)
    position: List[Coord] = Field(..., min_length=2, max_length=2)


class MutationGoal(BaseModel):
    """Goal for a specific mutation - either maximize or achieve a target count."""
    mutation: Name
    maximize: bool = False
    count: Optional[int] = Field(None, ge=1, le=MAX_CELLS)
    effect_weights: Optional[Dict[Name, float]] = Field(None, max_length=MAX_NAMED_ENTRIES)


class GenericSolveRequest(BaseModel):
    """Request for the generic solver."""
    cells: List[Cell] = Field(..., max_length=MAX_CELLS)
    priorities: Optional[Dict[Name, int]] = Field(None, max_length=MAX_NAMED_ENTRIES)
    targets: List[MutationGoal] = Field(..., min_length=1, max_length=MAX_TARGETS)
    effect_weights: Optional[Dict[Name, float]] = Field(None, max_length=MAX_NAMED_ENTRIES)
    buff_crops: Optional[List[Name]] = Field(None, max_length=MAX_NAMED_ENTRIES)
    remove_unused_crops: bool = False
    locks: Optional[List[LockedPlacement]] = Field(None, max_length=MAX_LOCKS)
    unique_crops: Optional[int] = Field(None, ge=0, le=12)
    crops: Optional[List[CropDefinition]] = Field(None, exclude=True)
    mutations: Optional[List[MutationDefinition]] = Field(None, exclude=True)


class CropPlacement(BaseModel):
    """Placement of a crop."""
    crop: str
    position: List[int]
    size: int
    locked: Optional[bool] = None


class MutationResult(BaseModel):
    """Result for a specific mutation."""
    mutation: str
    position: List[int]
    size: int
    effects: Optional[List[str]] = None
    value: Optional[float] = None


class GenericSolveResponse(BaseModel):
    """Response from the generic solver."""
    status: str
    total_cells_used: int
    placements: List[CropPlacement]
    mutations: List[MutationResult]
    cache_hit: Optional[str] = None
    time_limit: Optional[float] = None
    score: Optional[float] = None
    effect_value: Optional[float] = None
    expected_spawns_per_tick: Optional[float] = None
    effect_weights: Optional[Dict[str, Dict[str, float]]] = None
    buff_crops: Optional[List[str]] = None
    # UNIQUE_CROPS
    unique_crops: Optional[Dict] = None
