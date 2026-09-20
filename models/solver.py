# SPDX-License-Identifier: AGPL-3.0-only
# SkyShards local solver. See LICENSE.

"""Solver request/response models."""

from pydantic import BaseModel, Field
from typing import List, Dict, Optional

from .crops import CropDefinition, MutationDefinition


class LockedPlacement(BaseModel):
    """A locked crop/mutation placement that must be included in the solution."""
    name: str
    size: int = Field(..., ge=1, le=3)
    position: List[int] = Field(..., min_length=2, max_length=2)


class MutationGoal(BaseModel):
    """Goal for a specific mutation - either maximize or achieve a target count."""
    mutation: str
    maximize: bool = False
    count: Optional[int] = Field(None, ge=1)
    effect_weights: Optional[Dict[str, float]] = None


class GenericSolveRequest(BaseModel):
    """Request for the generic solver."""
    cells: List[List[int]]
    priorities: Optional[Dict[str, int]] = None
    targets: List[MutationGoal] = Field(..., min_length=1)
    effect_weights: Optional[Dict[str, float]] = None
    buff_crops: Optional[List[str]] = None
    remove_unused_crops: bool = False
    locks: Optional[List[LockedPlacement]] = None
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
    score: Optional[float] = None
    effect_value: Optional[float] = None
    expected_spawns_per_tick: Optional[float] = None
    effect_weights: Optional[Dict[str, Dict[str, float]]] = None
    buff_crops: Optional[List[str]] = None
